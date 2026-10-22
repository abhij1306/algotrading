"""Feed staleness (HAT-DATA-116).

The feed is stale at an instant `t` inside market hours exactly when no tick arrived in
`(t - 30 s, t]`: a silence of 30 seconds or more. Outside market hours the feed is never stale
(it is expected to be silent). The monitor records one `feed.stale` per transition into stale and
tells its health sink when ticks resume, so the health kill clears by itself (HAT-RISK-063).

Pure and synchronous: the caller passes `now`, and calls `check(now)` on a timer and (through
`on_tick`) before each tick is applied, so a silence that ends with a tick is still seen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.hat.clock import require_aware

__all__ = ["STALE_AFTER", "FeedHealth", "StalenessMonitor"]

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
        try:
            call()
        except Exception:  # a failing sink must never stop the feed; the state is kept
            logger.exception("feed health sink failed")
