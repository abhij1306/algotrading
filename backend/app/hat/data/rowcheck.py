"""The daily bar row check (HAT-DATA-042), one rule for every source: the nightly MCP quotes
(`ingest.check_quote`) and every bhavcopy row, nightly or history (`bhavcopy.parse_bhavcopy`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# `bars_daily_raw` prices are decimal(18, 4): a larger value or a finer one cannot be stored
MAX_PRICE = Decimal(10) ** 14
PRICE_STEP = Decimal("0.0001")


@dataclass(frozen=True)
class Candidate:
    """One fetched security-day after parsing, before the row checks."""

    day: date | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    prev_close: Decimal | None
    volume: int | None


def row_ok(c: Candidate, trade_date: date) -> bool:
    """HAT-DATA-042: the row's date is the trade date, low <= open, close <= high, every price is
    above zero, `prev_close` is null or above zero, and volume is not negative. Every price also
    fits the stored decimal(18, 4), so one bad value cannot abort a day's write."""
    if (
        c.day != trade_date
        or c.open is None
        or c.high is None
        or c.low is None
        or c.close is None
        or c.volume is None
    ):
        return False
    prices = (c.open, c.high, c.low, c.close)
    return (
        all(p > 0 and _fits(p) for p in prices)
        and c.low <= c.open <= c.high
        and c.low <= c.close <= c.high
        and (c.prev_close is None or (c.prev_close > 0 and _fits(c.prev_close)))
        and c.volume >= 0
    )


def _fits(price: Decimal) -> bool:
    return price.is_finite() and price < MAX_PRICE and price == price.quantize(PRICE_STEP)
