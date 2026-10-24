"""HAT's one trading calendar, as a pure value (HAT-RISK-075, HAT-SCH-086, HAT-OVR-001).

Every reader (risk, execution, the scheduler, the feed, portfolio and Telegram) reads the session
of a trade date from `hat_trading_calendar` rows, loaded once by `data.calendar.session_calendar`,
never from a weekday rule, so a holiday is closed and a special session is open. An instant whose
date the calendar does not know is never open: risk fails closed on it, and every other reader
treats the day as no trading day.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import cached_property

from ..time import as_utc, trade_date_of


class SessionState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"  # no row for this trade date, or a trading row without its hours


@dataclass(frozen=True)
class SessionDay:
    """One calendar row: a trading day has an open and a close instant."""

    is_trading: bool
    open_at: datetime | None = None
    close_at: datetime | None = None


@dataclass(frozen=True)
class SessionCalendar:
    days: Mapping[date, SessionDay] = field(default_factory=dict)

    @classmethod
    def from_rows(
        cls, rows: Iterable[tuple[date, bool, datetime | None, datetime | None]]
    ) -> SessionCalendar:
        """Build from `(date, is_trading, open_at, close_at)` rows; a later row wins."""
        return cls(
            {day: SessionDay(trading, opens, closes) for day, trading, opens, closes in rows}
        )

    def state(self, at: datetime) -> SessionState:
        """OPEN from the session open to its close, both ends inclusive (as the market-hours
        policy); CLOSED on a non-trading day, before the open or after the close; UNKNOWN when
        the date is missing or a trading day lacks its times."""
        day = self.days.get(trade_date_of(at))
        if day is None:
            return SessionState.UNKNOWN
        if not day.is_trading:
            return SessionState.CLOSED
        if day.open_at is None or day.close_at is None:
            return SessionState.UNKNOWN
        moment = as_utc(at)
        if as_utc(day.open_at) <= moment <= as_utc(day.close_at):
            return SessionState.OPEN
        return SessionState.CLOSED

    def is_open(self, at: datetime) -> bool:
        """Whether `state(at)` is OPEN; an unknown date is not open."""
        return self.state(at) is SessionState.OPEN

    def open_on(self, day: date) -> datetime | None:
        """The session open of `day`; None unless the calendar holds it as a trading day with its
        times."""
        row = self.days.get(day)
        if row is None or not row.is_trading or row.open_at is None or row.close_at is None:
            return None
        return as_utc(row.open_at)

    def next_open_after(self, at: datetime) -> datetime | None:
        """The `open_at` of the first trading day whose open is strictly after `at`, or None when
        the calendar holds no such day."""
        moment = as_utc(at)
        opens = [
            as_utc(day.open_at)
            for day in self.days.values()
            if day.is_trading and day.open_at is not None and as_utc(day.open_at) > moment
        ]
        return min(opens) if opens else None

    @cached_property
    def _trading(self) -> tuple[date, ...]:
        return tuple(sorted(day for day, row in self.days.items() if row.is_trading))

    def trading_days(self) -> list[date]:
        """The calendar's trading days, in date order."""
        return list(self._trading)

    @cached_property
    def holidays(self) -> frozenset[date]:
        """The dates the calendar holds as closed (holidays and listed weekends). A property, as
        the set it is: passing the bound method where a set was meant crashed PR 42's build."""
        return frozenset(day for day, row in self.days.items() if not row.is_trading)

    def previous_trading_day(self, day: date) -> date | None:
        """The last trading day before `day`; None when the calendar holds none."""
        earlier = [d for d in self.trading_days() if d < day]
        return earlier[-1] if earlier else None

    def next_trading_day(self, day: date) -> date | None:
        """The first trading day after `day`; None when the calendar holds none."""
        return next((d for d in self.trading_days() if d > day), None)

    def first_open_at_or_after(self, at: datetime) -> datetime | None:
        """The first session open at or after `at` (None when the calendar holds none)."""
        moment = as_utc(at)
        opens = [
            as_utc(day.open_at)
            for day in self.days.values()
            if day.is_trading and day.open_at is not None and as_utc(day.open_at) >= moment
        ]
        return min(opens) if opens else None


def is_trading_day(calendar: SessionCalendar | None, day: date) -> bool:
    """A day the calendar holds as a trading day; an unknown day (or no calendar) is not one."""
    found = calendar.days.get(day) if calendar is not None else None
    return found is not None and found.is_trading


CALENDAR_AHEAD = timedelta(days=7)


def calendar_short(
    calendar: SessionCalendar | None, today: date, ahead: timedelta = CALENDAR_AHEAD
) -> bool:
    """True when the calendar holds no trading day after `today + ahead` (or there is no
    calendar): every trade-date job stops once its days run out (`is_trading_day`), so a
    calendar that is not refreshed (the year-end gap) must be noticed before then (P5)."""
    if calendar is None:
        return True
    days = calendar.trading_days()
    return not days or days[-1] <= today + ahead


def next_session_open(calendar: SessionCalendar, after: datetime) -> datetime | None:
    """`SessionCalendar.next_open_after`, as a function for callers that hold only the calendar."""
    return calendar.next_open_after(after)
