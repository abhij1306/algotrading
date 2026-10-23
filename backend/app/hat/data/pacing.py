"""One call pacer for every outside source the data layer asks (audit D8).

A `Pacer` spaces call starts by any of three rules: a minimum interval between two starts (the
NSE archive, about one request a second, HAT-DATA-092), at most `per_minute` starts in any 60
seconds and at most `per_day` in an IST day (the Fyers history budget, audit D3). `wait` blocks
until the next call may start and counts it; a call counts when it is started, whatever it
answers. `state` keeps the last minute's starts and the day counts across runs (the minute
history's `Progress`); it is saved before `wait` returns, so a crash never loses a count. The
clock and the sleep are injected (tests move a fake clock).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from ..clock import SYSTEM_CLOCK, Clock
from ..errors import HatError
from ..time import IST

WINDOW = timedelta(minutes=1)  # the per-minute budget's window
CODE_DAY_LIMIT = "pace_daily_limit"

Sleep = Callable[[float], None]


class PaceState(Protocol):
    """Where a pacer keeps its counts: the starts of the last minute and calls per IST day."""

    calls: dict[str, int]
    recent: list[datetime]

    def save(self) -> None: ...


@dataclass
class MemoryState:
    """Counts kept for one run only."""

    calls: dict[str, int] = field(default_factory=dict)
    recent: list[datetime] = field(default_factory=list)

    def save(self) -> None:
        return None


class DayLimitError(HatError):
    """The IST day's call budget is spent; nothing was started."""

    code = CODE_DAY_LIMIT


class Pacer:
    """Spaces call starts by `min_interval` seconds, `per_minute` and `per_day` (None: no
    such rule). A state seeded with an earlier run's starts paces against them too."""

    def __init__(
        self,
        *,
        min_interval: float | None = None,
        per_minute: int | None = None,
        per_day: int | None = None,
        state: PaceState | None = None,
        clock: Clock = SYSTEM_CLOCK,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._per_minute = per_minute
        self._per_day = per_day
        self._state: PaceState = state if state is not None else MemoryState()
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[datetime] = deque(sorted(self._state.recent))
        self._last: datetime | None = self._recent[-1] if self._recent else None
        self.made = 0
        self.last_day: date | None = None  # the IST day of the last call started

    def today(self) -> date:
        return self._clock.now().astimezone(IST).date()

    def wait(self) -> None:
        """Sleep until the next call may start, then count it; `DayLimitError` when the day's
        budget (after any wait: the day the call would start) is spent."""
        now = self._clock.now()
        while (pause := self._pause(now)) > 0:
            self._sleep(pause)
            now = self._clock.now()
        day = now.astimezone(IST).date()
        key = day.isoformat()
        if self._per_day is not None and self._state.calls.get(key, 0) >= self._per_day:
            raise DayLimitError(CODE_DAY_LIMIT)
        self._recent.append(now)
        self._last = now
        self._state.recent = list(self._recent)
        self._state.calls[key] = self._state.calls.get(key, 0) + 1
        self.made += 1
        self.last_day = day
        self._state.save()

    def _pause(self, now: datetime) -> float:
        """Seconds until a call may start at `now` (0 or less: it may)."""
        while self._recent and self._recent[0] <= now - WINDOW:
            self._recent.popleft()
        pause = 0.0
        if self._min_interval is not None and self._last is not None:
            pause = self._min_interval - (now - self._last).total_seconds()
        if self._per_minute is not None and len(self._recent) >= self._per_minute:
            oldest = self._recent[len(self._recent) - self._per_minute]
            pause = max(pause, (oldest + WINDOW - now).total_seconds())
        return pause
