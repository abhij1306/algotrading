"""NSE tick sizes by price (HAT-EXEC-053).

Since 15 April 2025 the NSE cash-market tick of an equity depends on its price: 0.01 below ₹250,
0.05 up to ₹1,000, 0.10 up to ₹5,000, 0.50 up to ₹10,000, 1.00 up to ₹20,000 and 5.00 above.
The exchange fixes each security's tick for a month from its closing price on the last trading
day of the previous month, so the tick of a trade date is read from that close, never from the
live price. Pure and exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

__all__ = ["NSE_TICK_SLABS", "reference_close", "tick_for_date", "tick_size"]

# (upper bound of the price slab, inclusive, or None for no bound; tick). Below 250 is 0.01; a
# price of exactly 250 is in the 0.05 slab.
NSE_TICK_SLABS: tuple[tuple[Decimal | None, Decimal], ...] = (
    (Decimal(1000), Decimal("0.05")),
    (Decimal(5000), Decimal("0.10")),
    (Decimal(10000), Decimal("0.50")),
    (Decimal(20000), Decimal("1.00")),
    (None, Decimal("5.00")),
)
SUB_250_TICK = Decimal("0.01")
SUB_250_BELOW = Decimal(250)


def tick_size(price: Decimal) -> Decimal | None:
    """The tick of a security whose reference close is `price`; None for a price that is not a
    positive finite Decimal (the caller fails closed)."""
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        return None
    if price < SUB_250_BELOW:
        return SUB_250_TICK
    for upper, tick in NSE_TICK_SLABS:
        if upper is None or price <= upper:
            return tick
    return None  # unreachable: the last slab has no bound


def reference_close(
    dates: Sequence[date], closes: Sequence[Decimal | None], trade_date: date
) -> Decimal | None:
    """The close the tick of `trade_date` is set from: the last close dated before the first day
    of `trade_date`'s month. A security listed this month has none; its latest close before the
    trade date stands in ([OWNER-PENDING]: NSE sets a new listing's tick from its issue price,
    which HAT does not hold). None when there is no earlier close at all."""
    month_start = trade_date.replace(day=1)
    last_month: Decimal | None = None
    latest: Decimal | None = None
    for day, close in sorted(zip(dates, closes, strict=True), key=lambda pair: pair[0]):
        if close is None or day >= trade_date:
            continue
        latest = close
        if day < month_start:
            last_month = close
    return last_month if last_month is not None else latest


def tick_for_date(
    dates: Sequence[date], closes: Sequence[Decimal | None], trade_date: date
) -> Decimal | None:
    """The instrument's tick on `trade_date` from its daily closes; None when it has no close
    before the trade date."""
    close = reference_close(dates, closes, trade_date)
    return None if close is None else tick_size(close)
