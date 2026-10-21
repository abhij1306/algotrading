"""NSE market sessions: the one HAT owner of the session times and the weekend rule.

The cash session is 09:15:00-15:30:00 IST, Monday to Friday, both ends inclusive: 15:30:00
exactly still counts as open, any later instant is closed. There is no DEV_MODE bypass and no
process-wide installed calendar: a `MarketHoursPolicy` is built from the calendar it is given
(`MarketCalendar.policy`, from `hat_trading_calendar`) and passed to whoever needs it. Special
sessions (a trading day with its own open and close, such as Muhurat or a Saturday session) are
`sessions`.

This is HAT's copy (K1, backend review C1); the legacy `app.utils.market_hours` keeps its own
until it is deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from ..clock import SYSTEM_CLOCK, Clock, require_aware

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


class MarketPhase(StrEnum):
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    PRE_MARKET = "pre_market"
    OPEN = "open"
    POST_MARKET = "post_market"

    @property
    def is_open(self) -> bool:
        return self is MarketPhase.OPEN


@dataclass(frozen=True)
class MarketHoursPolicy:
    """When the NSE cash market is open.

    `clock` supplies "now" (inject a `FixedClock` in tests). `holidays` holds IST dates on
    which the exchange is closed; `sessions` maps an IST date to its own (open, close), which
    wins over the weekend and holiday rules. `session_open` / `session_close` are the default
    session (the market's own, from `hat_market`); they default to 09:15-15:30.
    """

    clock: Clock = SYSTEM_CLOCK
    holidays: frozenset[date] = field(default_factory=frozenset)
    sessions: Mapping[date, tuple[time, time]] = field(default_factory=dict)
    session_open: time = SESSION_OPEN
    session_close: time = SESSION_CLOSE

    def now_ist(self) -> datetime:
        return self.clock.now().astimezone(IST)

    def phase(self, at: datetime | None = None) -> MarketPhase:
        """The market phase at `at` (default: the clock's now)."""
        now = self.now_ist() if at is None else require_aware(at).astimezone(IST)
        day = now.date()
        special = self.sessions.get(day)
        opens, closes = special or (self.session_open, self.session_close)
        if special is None:
            if now.weekday() >= 5:  # Saturday or Sunday
                return MarketPhase.WEEKEND
            if day in self.holidays:
                return MarketPhase.HOLIDAY
        current = now.time()
        if current < opens:
            return MarketPhase.PRE_MARKET
        if current > closes:
            return MarketPhase.POST_MARKET
        return MarketPhase.OPEN

    def is_open(self, at: datetime | None = None) -> bool:
        return self.phase(at).is_open
