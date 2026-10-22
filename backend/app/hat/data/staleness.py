"""Feed staleness (HAT-DATA-116).

The feed is stale at an instant `t` inside market hours exactly when no tick arrived in
`(t - 30 s, t]`: a silence of 30 seconds or more. Outside market hours the feed is never stale
(it is expected to be silent). The monitor records one `feed.stale` per transition into stale and
tells its health sink when ticks resume, so the health kill clears by itself (HAT-RISK-063).

Pure and synchronous: the caller passes `now`, and calls `check(now)` on a timer and (through
`on_tick`) before each tick is applied, so a silence that ends with a tick is still seen.

One symbol's silence is not the feed's (PR 39): an illiquid or band-locked name can stay quiet
for hours while the socket is fine, and a missed entry is safe where a kill would make the
account manage-only all day. `SilenceWatch` raises an alert and asks the provider to subscribe
the symbol again once per silence of `silent_after`, and never reaches the health kill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Protocol

from app.hat.clock import require_aware

__all__ = ["STALE_AFTER", "SYMBOL_SILENT_AFTER", "FeedHealth", "SilenceWatch", "StalenessMonitor"]

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(seconds=30)
SYMBOL_SILENT_AFTER = timedelta(seconds=120)


class FeedHealth(Protocol):
    """What reacts to the feed going stale and recovering (the health kill, the event log)."""

    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None: ...

    def feed_recovered(self, now: datetime) -> None: ...


class StalenessMonitor:
    def __init__(
        self,
        health: FeedHealth,
        is_open: Callable[[datetime], bool],
        *,
        stale_after: timedelta = STALE_AFTER,
    ) -> None:
        if stale_after <= timedelta(0):
            raise ValueError("stale_after must be positive")
        self._health = health
        self._is_open = is_open
        self._stale_after = stale_after
        self._last_tick: datetime | None = None
        self._stale = False

    @property
    def last_tick(self) -> datetime | None:
        """The arrival instant of the latest tick, None before the first."""
        return self._last_tick

    @property
    def stale_after(self) -> timedelta:
        return self._stale_after

    @property
    def stale(self) -> bool:
        """The state at the last `check` or `on_tick`."""
        return self._stale

    def is_stale(self, now: datetime) -> bool:
        """Whether the feed is stale at `now` (does not change any state)."""
        require_aware(now)
        if not self._is_open(now):
            return False
        return self._last_tick is None or now - self._last_tick >= self._stale_after

    def check(self, now: datetime) -> bool:
        """Re-evaluate at `now`; report a transition to the health sink. Returns `stale`."""
        stale = self.is_stale(now)
        if stale and not self._stale:
            silent = None if self._last_tick is None else now - self._last_tick
            self._stale = True
            self._notify(lambda: self._health.feed_stale(now, silent))
        elif not stale and self._stale:
            self._stale = False
            self._notify(lambda: self._health.feed_recovered(now))
        return self._stale

    def on_tick(self, now: datetime) -> None:
        """A tick arrived at `now`: first observe any silence that just ended, then reset."""
        self.check(now)
        if self._last_tick is None or now > self._last_tick:
            self._last_tick = now
        self.check(now)

    @staticmethod
    def _notify(call: Callable[[], None]) -> None:
        try:
            call()
        except Exception:  # a failing sink must never stop the feed; the state is kept
            logger.exception("feed health sink failed")


class SilenceWatch:
    """Per-symbol silence (PR 39). A watched symbol is silent at a `check` when the market is
    open, the feed is live and no tick of it arrived in the last `silent_after`, counted only
    over checks where all three held (a closed market or a stale feed explains any silence, and
    a symbol newly watched starts its count at its first check). Each silence is reported once:
    one `alert` per symbol and one `resubscribe` of the symbols found at that check; the next
    report needs a tick of the symbol first, or the symbol unwatched or the market closed in
    between. It holds no reference to the feed's health, so it can never trip a kill."""

    def __init__(
        self,
        is_open: Callable[[datetime], bool],
        alert: Callable[[str, datetime], None],
        resubscribe: Callable[[list[str]], None],
        *,
        silent_after: timedelta = SYMBOL_SILENT_AFTER,
    ) -> None:
        if silent_after <= timedelta(0):
            raise ValueError("silent_after must be positive")
        self._is_open = is_open
        self._alert = alert
        self._resubscribe = resubscribe
        self._silent_after = silent_after
        self._since: dict[str, datetime] = {}  # where each symbol's silence is counted from
        self._reported: set[str] = set()

    @property
    def silent_after(self) -> timedelta:
        return self._silent_after

    @property
    def reported(self) -> frozenset[str]:
        """The symbols whose current silence was reported."""
        return frozenset(self._reported)

    def on_tick(self, symbol: str, now: datetime) -> None:
        """A tick of `symbol` arrived at `now`: its silence ends (counted for a symbol already
        watched at a check; one not yet watched starts its count at its first check)."""
        require_aware(now)
        since = self._since.get(symbol)
        if since is not None and now > since:
            self._since[symbol] = now
        self._reported.discard(symbol)

    def check(self, now: datetime, watched: Collection[str], feed_stale: bool) -> list[str]:
        """Report the watched symbols newly silent at `now`; returns them, sorted."""
        require_aware(now)
        watched = frozenset(watched)
        for symbol in [s for s in self._since if s not in watched]:
            del self._since[symbol]
        self._reported &= watched
        if not self._is_open(now):
            self._reported.clear()
        if feed_stale or not self._is_open(now):
            for symbol in watched:
                self._since[symbol] = now
            return []
        found = sorted(
            symbol
            for symbol in watched
            if symbol not in self._reported
            and now - self._since.setdefault(symbol, now) >= self._silent_after
        )
        if found:
            self._reported.update(found)
            _isolated(lambda: self._resubscribe(found), "symbol resubscribe")
            for symbol in found:
                _isolated(lambda s=symbol: self._alert(s, now), "symbol silence alert")
        return found


def _isolated(call: Callable[[], None], what: str) -> None:
    try:
        call()
    except Exception:  # the watch keeps its state; a failing sink never stops the feed
        logger.exception("%s failed", what)
