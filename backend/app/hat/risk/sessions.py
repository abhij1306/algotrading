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
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import cached_property

from ..time import IST, as_utc, trade_date_of

# The regular session (IST). The open-phase times (`live.prepare`, `modes.apply`, the gap scan,
# the regime gate, the pre-open capture) are set against its open and move with a session that
# opens at another time (`open_shift`); the close-phase times (`exits.close`, the GTT swap, the
# sales' cut-off, the end of the trigger window) against its continuous-trading end, and the
# session end against its close, both moving with the close (`close_shift`): an early close, or a
# special (Muhurat) session.
REGULAR_OPEN = time(9, 15)
REGULAR_CLOSE = time(15, 30)
# NSE's closing auction session is live for every stock (owner 2026-10-25): continuous trading
# ends this long before the session close (the auction and its closing window), 15:15 in a regular
# session, and HAT's square-off finishes before it. The price then is not the close: the official
# close is the auction's, settled around 15:40, so a close-phase rule decides on the live last
# trade and only the nightly work (ingest, close gaps, features) reads the official close.
CLOSING_AUCTION = timedelta(minutes=15)
REGULAR_CONTINUOUS_END = (datetime.combine(date.min, REGULAR_CLOSE) - CLOSING_AUCTION).time()


def before_continuous_end(minutes: int) -> time:
    """The IST wall time `minutes` before the regular session's continuous-trading end (15:05
    for 10); a job set at it moves with the session's close (`close_shift`)."""
    return (datetime.combine(date.min, REGULAR_CONTINUOUS_END) - timedelta(minutes=minutes)).time()


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

    def close_on(self, day: date) -> datetime | None:
        """The session close of `day`; None unless the calendar holds it as a trading day with
        its times."""
        row = self.days.get(day)
        if row is None or not row.is_trading or row.open_at is None or row.close_at is None:
            return None
        return as_utc(row.close_at)

    def continuous_end_on(self, day: date) -> datetime | None:
        """When `day`'s continuous trading ends (`CLOSING_AUCTION` before its close, 15:15 IST in
        a regular session); None unless the calendar holds the day's times."""
        closes = self.close_on(day)
        return None if closes is None else closes - CLOSING_AUCTION

    def close_shift(self, day: date) -> timedelta:
        """How far `day`'s session close, and so its continuous-trading end, is from the regular
        one (`close_shift`)."""
        return close_shift(day, self.close_on(day))

    def open_shift(self, day: date) -> timedelta:
        """How far `day`'s session open is from the regular open (`open_shift`)."""
        return open_shift(day, self.open_on(day))

    def hours(self, day: date) -> tuple[time, time]:
        """`day`'s session open and close as IST wall times; the regular session when the
        calendar does not hold the day's times."""
        opens, closes = self.open_on(day), self.close_on(day)
        if opens is None or closes is None:
            return REGULAR_OPEN, REGULAR_CLOSE
        return opens.astimezone(IST).time(), closes.astimezone(IST).time()

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


def close_shift(day: date, close_at: datetime | None) -> timedelta:
    """How far a session closing at `close_at` closes from `REGULAR_CLOSE` on `day` (negative for
    an early close); none when the close is unknown, so the regular times stand."""
    if close_at is None:
        return timedelta(0)
    return as_utc(close_at) - as_utc(datetime.combine(day, REGULAR_CLOSE, tzinfo=IST))


def open_shift(day: date, open_at: datetime | None) -> timedelta:
    """How far a session opening at `open_at` opens from `REGULAR_OPEN` on `day` (5 h 05 min for
    a 14:20 Muhurat open); none when the open is unknown, so the regular times stand."""
    if open_at is None:
        return timedelta(0)
    return as_utc(open_at) - as_utc(datetime.combine(day, REGULAR_OPEN, tzinfo=IST))


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
