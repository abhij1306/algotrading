"""Pre-open gap capture (HAT-DATA-118).

From 09:00 to 09:08 IST (15 to 7 minutes before the session open: a special session's own
pre-open, given by `hours`) the indicative open of each subscribed symbol is recorded against its
previous close. When the feed carries no pre-open field (an `[A]` validated during the build),
the open of the first tick of the session is used instead. Each record says which source
supplied the open. Records are kept per (symbol, trade date), and the first-tick open is taken
only from a tick at or after the session open: before it the feed's open is the previous
session's (PR 22).

`gap_pct` and `meets_gap` are shared with the 09:16 gap scan, so one definition of "gap" exists.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from app.hat.clock import require_aware
from app.hat.data.sessions import SESSION_OPEN
from app.hat.money import exact
from app.hat.time import IST

__all__ = [
    "INDICATIVE_OPEN_FIELDS",
    "PREOPEN_END",
    "PREOPEN_START",
    "GapRecord",
    "GapSource",
    "PreopenGaps",
    "gap_pct",
    "meets_gap",
]

PREOPEN_START = time(9, 0)
PREOPEN_END = time(9, 8)
_START_BEFORE_OPEN = timedelta(minutes=15)  # 09:00 before the 09:15 open
_END_BEFORE_OPEN = timedelta(minutes=7)  # 09:08
# [A] Fyers fields that may carry an indicative pre-open price; validated during the build.
INDICATIVE_OPEN_FIELDS = ("indicative_open_price", "pre_open_price")
OPEN_FIELD = "open_price"
PREV_CLOSE_FIELD = "prev_close_price"


class GapSource(StrEnum):
    PREOPEN = "preopen"
    FIRST_TICK = "first_tick"


def gap_pct(open_price: Decimal, prev_close: Decimal) -> Decimal | None:
    """`100 * (open / prev_close - 1)`; None when either price is not positive."""
    if open_price <= 0 or prev_close <= 0:
        return None
    return Decimal(100) * (open_price / prev_close - 1)


def meets_gap(open_price: Decimal, prev_close: Decimal, minimum_pct: Decimal) -> bool:
    """Whether the gap is at least `minimum_pct`, decided in exact rational arithmetic so a gap
    of exactly the minimum is never lost to rounding."""
    if open_price <= 0 or prev_close <= 0:
        return False
    gap = 100 * (Fraction(open_price) / Fraction(prev_close) - 1)
    return gap >= Fraction(minimum_pct)


@dataclass(frozen=True)
class GapRecord:
    symbol: str
    trade_date: date
    prev_close: Decimal
    open: Decimal
    gap_pct: Decimal
    source: GapSource
    recorded_at: datetime


def _price(payload: Mapping[str, object], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        value = exact(payload.get(name), floats=True, positive=True)
        if value is not None:
            return value
    return None


class PreopenGaps:
    """One gap record per symbol per trade date: the latest indicative open in the pre-open
    window, else the open of the first in-session tick."""

    def __init__(self, opens: Callable[[date], time] | None = None) -> None:
        self._records: dict[tuple[str, date], GapRecord] = {}
        self._opens = opens  # a day's own session open (IST); default the regular one

    def _window(self, day: date) -> tuple[time, time, time]:
        """`day`'s pre-open start, end and session open, IST wall times."""
        if self._opens is None:
            return PREOPEN_START, PREOPEN_END, SESSION_OPEN
        opens = datetime.combine(day, self._opens(day))
        return (
            (opens - _START_BEFORE_OPEN).time(),
            (opens - _END_BEFORE_OPEN).time(),
            opens.time(),
        )

    def records(self, day: date) -> dict[str, GapRecord]:
        """The records of trade date `day`, by symbol."""
        return {symbol: r for (symbol, d), r in self._records.items() if d == day}

    def observe(self, payload: Mapping[str, object], now: datetime) -> GapRecord | None:
        """Record from one normalized tick payload; returns the record it wrote, if any."""
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return None
        prev_close = _price(payload, (PREV_CLOSE_FIELD,))
        if prev_close is None:
            return None
        local = require_aware(now).astimezone(IST)
        start, end, session_open = self._window(local.date())
        indicative = _price(payload, INDICATIVE_OPEN_FIELDS)
        if indicative is not None and start <= local.time() <= end:
            return self.record(symbol, indicative, prev_close, GapSource.PREOPEN, now)
        opened = _price(payload, (OPEN_FIELD,))
        if (
            opened is not None
            and local.time() >= session_open
            and (symbol, local.date()) not in self._records
        ):
            return self.record(symbol, opened, prev_close, GapSource.FIRST_TICK, now)
        return None

    def record(
        self,
        symbol: str,
        open_price: Decimal,
        prev_close: Decimal,
        source: GapSource,
        now: datetime,
    ) -> GapRecord | None:
        pct = gap_pct(open_price, prev_close)
        if pct is None:
            return None
        day = require_aware(now).astimezone(IST).date()
        found = GapRecord(symbol, day, prev_close, open_price, pct, source, now)
        for key in [k for k in self._records if k[1] < day]:  # earlier days are never read
            del self._records[key]
        self._records[(symbol, day)] = found
        return found
