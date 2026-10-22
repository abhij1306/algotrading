"""Feed staleness (HAT-DATA-116).

The feed is stale at an instant `t` inside market hours exactly when no tick arrived in
`(t - 30 s, t]`: a silence of 30 seconds or more. Outside market hours the feed is never stale
(it is expected to be silent). The monitor records one `feed.stale` per transition into stale and
tells its health sink when ticks resume, so the health kill clears by itself (HAT-RISK-063).

Pure and synchronous: the caller passes `now`, and calls `check(now)` on a timer and (through
`on_tick`) before each tick is applied, so a silence that ends with a tick is still seen.

One symbol's silence is not the feed's (PR 39): an illiquid or band-locked name can stay quiet
for hours while the socket is fine, and a missed entry is safe where a kill would make the
account manage-only all day. `SilenceWatch` asks the provider to subscribe the symbol again
after each `silent_after` of silence and alerts the owner once a day per symbol, and never
reaches the health kill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Protocol

from app.hat.clock import require_aware

__all__ = ["STALE_AFTER", "FeedHealth", "SilenceWatch", "StalenessMonitor"]

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(seconds=30)


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
        _isolated(call, "feed health sink")


class SilenceWatch:
    """Per-symbol silence (PR 39). A watched symbol is silent at a `check` when the market is
    open, the feed is live and no tick of it arrived in the last `silent_after`, counted from
    its last tick or from the last check at which it was not yet watched, the market was closed
    or the feed stale, whichever is later (a closed market or a stale feed explains any silence,
    so either restarts the count). Each check asks the provider to `resubscribe` the symbols
    found silent in one call, and counts each one again from then, so a symbol that stays
    silent is asked again every `silent_after`. The owner is told by one `alert` per check
    listing the symbols not yet alerted since the market last opened (one message, not one per
    symbol, and not again for a symbol that resumes and falls silent again that day). It holds
    no reference to the feed's health, so it can never trip a kill."""

    def __init__(
        self,
        is_open: Callable[[datetime], bool],
        alert: Callable[[list[str], datetime], None],
        resubscribe: Callable[[list[str]], None],
        *,
        silent_after: timedelta,
    ) -> None:
        if silent_after <= timedelta(0):
            raise ValueError("silent_after must be positive")
        self._is_open = is_open
        self._alert = alert
        self._resubscribe = resubscribe
        self._silent_after = silent_after
        self._since: dict[str, datetime] = {}  # where each symbol's silence is counted from
        self._alerted: set[str] = set()  # alerted since the market last opened

    @property
    def silent_after(self) -> timedelta:
        return self._silent_after

    def on_tick(self, symbol: str, now: datetime) -> None:
        """A tick of `symbol` arrived at `now`: its silence ends (counted for a symbol already
        watched at a check; one not yet watched starts its count at its first check)."""
        require_aware(now)
        since = self._since.get(symbol)
        if since is not None and now > since:
            self._since[symbol] = now

    def check(self, now: datetime, watched: Collection[str], feed_stale: bool) -> list[str]:
        """Resubscribe the watched symbols silent at `now` and alert the new ones; returns the
        silent symbols, sorted."""
        require_aware(now)
        watched = frozenset(watched)
        for symbol in [s for s in self._since if s not in watched]:
            del self._since[symbol]
        is_open = self._is_open(now)
        if not is_open:
            self._alerted.clear()
        if feed_stale or not is_open:
            for symbol in watched:
                self._since[symbol] = now
            return []
        found = sorted(
            symbol
            for symbol in watched
            if now - self._since.setdefault(symbol, now) >= self._silent_after
        )
        if found:
            fresh = [symbol for symbol in found if symbol not in self._alerted]
            self._alerted.update(fresh)
            for symbol in found:
                self._since[symbol] = now
            _isolated(lambda: self._resubscribe(found), "symbol resubscribe")
            if fresh:
                _isolated(lambda: self._alert(fresh, now), "symbol silence alert")
        return found


def _isolated(call: Callable[[], None], what: str) -> None:
    """Run a sink; its failure is logged and never reaches the feed (the caller keeps its
    state)."""
    try:
        call()
    except Exception:
        logger.exception("%s failed", what)
