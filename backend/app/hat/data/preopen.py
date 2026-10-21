"""Pre-open gap capture (HAT-DATA-118).

From 09:00 to 09:08 IST the indicative open of each subscribed symbol is recorded against its
previous close. When the feed carries no pre-open field (an `[A]` validated during the build),
the open of the first tick of the session is used instead. Each record says which source
supplied the open.

`gap_pct` and `meets_gap` are shared with the 09:16 gap scan, so one definition of "gap" exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction

from app.utils.clock import require_aware
from app.utils.market_hours import IST

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
    prev_close: Decimal
    open: Decimal
    gap_pct: Decimal
    source: GapSource
    recorded_at: datetime


def _price(payload: Mapping[str, object], names: tuple[str, ...]) -> Decimal | None:
    for name in names:
        raw = payload.get(name)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = Decimal(str(raw))
        except InvalidOperation:
            continue
        if value.is_finite() and value > 0:
            return value
    return None


class PreopenGaps:
    """One gap record per symbol per day: the latest indicative open in the pre-open window, else
    the open of the first tick."""

    def __init__(self) -> None:
        self._records: dict[str, GapRecord] = {}

    def records(self) -> dict[str, GapRecord]:
        return dict(self._records)

    def observe(self, payload: Mapping[str, object], now: datetime) -> GapRecord | None:
        """Record from one normalized tick payload; returns the record it wrote, if any."""
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            return None
        prev_close = _price(payload, (PREV_CLOSE_FIELD,))
        if prev_close is None:
            return None
        local = require_aware(now).astimezone(IST).time()
        indicative = _price(payload, INDICATIVE_OPEN_FIELDS)
        if indicative is not None and PREOPEN_START <= local <= PREOPEN_END:
            return self.record(symbol, indicative, prev_close, GapSource.PREOPEN, now)
        opened = _price(payload, (OPEN_FIELD,))
        if opened is not None and symbol not in self._records:
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
        found = GapRecord(symbol, prev_close, open_price, pct, source, now)
        self._records[symbol] = found
        return found
