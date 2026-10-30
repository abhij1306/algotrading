"""The price an order may fill at no earlier than 09:20 IST (owner 2026-10-30: "keep 9.20 at
least"), shared by the backtest, live's rotation and the shadow-day replay.

No backtest order fills at the 09:15 open: a rotation's orders trade at `FILL_AT`, the instant
live's `rotation.rebalance` runs, and a daily-estimate entry that gaps over its level fills at
the same price live's first trade after 09:20 is estimated at. The price at 09:20
(`price_0920`) is the last trade before 09:20: the close of the session's 09:15 five-minute bar
(not the 09:20 bar's open, which is a trade after 09:20; live's last price at 09:20 is that
close). Without that bar (no 5-minute store before 2021-10-26, a name without bars, a session
the backtest refuses to read) it is the day's open moved against the order by `OPEN_TO_0920`,
the median move from the open to 09:20 measured on the five-minute years
(`scripts/research/open_to_0920.py`, decisions.md "Orders at 09:20"). `fill_0920` says which
it used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal

from ..data.bars import Bar
from ..money import MONEY_SCALE
from ..time import IST

__all__ = [
    "ESTIMATED",
    "FILL_AT",
    "FIRST_BAR",
    "OPEN_TO_0920",
    "OPEN_TO_0920_MEASURED",
    "REAL",
    "OpenTo0920",
    "estimated_0920",
    "fill_0920",
    "gap_over_fill",
    "price_0920",
]

FILL_AT = time(9, 20)  # every rotation order trades at this instant (live's `rotation.rebalance`)
FIRST_BAR = time(9, 15)  # the session's first five-minute bar: its close is the price at 09:20
REAL, ESTIMATED = "0920", "estimate"  # what a rotation fill's price was read from
_PRICE_STEP = Decimal(1).scaleb(-MONEY_SCALE)


@dataclass(frozen=True)
class OpenTo0920:
    """The median adverse move from the day's open to 09:20, as a fraction of the open: a buy's
    (`buy`: 09:20 price over the open less 1) and a sale's (`sell`: 1 less that ratio), measured
    on the five-minute years (`scripts/research/open_to_0920.py`, decisions.md)."""

    buy: Decimal
    sell: Decimal


# Measured 2026-10-30 (`scripts/research/open_to_0920.py`, decisions.md "Orders at 09:20"): per
# side the larger of the all-member-days and rebalance-session medians, at least 0. Were it not
# measured (`OPEN_TO_0920_MEASURED` False), runs estimating with it would report the known gap
# `fill_0920_estimate_provisional` (`spec.PROVISIONAL_0920`).
OPEN_TO_0920 = OpenTo0920(buy=Decimal("0.0002"), sell=Decimal("0.0003"))
OPEN_TO_0920_MEASURED = True


def price_0920(day: date, bars: Sequence[Bar]) -> tuple[Decimal, Bar] | None:
    """(the price at 09:20, the bar it is read from) of a session's five-minute bars: the close
    of its 09:15 bar, the last trade before 09:20; None without that bar."""
    first = datetime.combine(day, FIRST_BAR, tzinfo=IST)
    found = next((b for b in bars if b.start == first), None)
    return None if found is None else (found.close, found)


def estimated_0920(
    open_: Decimal,
    side: str,
    estimate: OpenTo0920 = OPEN_TO_0920,
    *,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> Decimal:
    """The day's open moved against an order by the measured median move to 09:20: up for a buy,
    down for a sale, to the price step, and within the day's traded range (`low`..`high`, when
    given): a day that opened at its high buys at most there, one that opened at its low sells
    at least there."""
    move = estimate.buy if side == "buy" else -estimate.sell
    price = (open_ * (1 + move)).quantize(_PRICE_STEP, ROUND_HALF_EVEN)
    if high is not None:
        price = min(price, high)
    if low is not None:
        price = max(price, low)
    return price


def fill_0920(
    day: date,
    side: str,
    bars: Sequence[Bar] | None,
    open_: Decimal,
    estimate: OpenTo0920 = OPEN_TO_0920,
    *,
    low: Decimal | None = None,
    high: Decimal | None = None,
) -> tuple[Decimal, str]:
    """(the price a rotation order fills at on `day`, `REAL` or `ESTIMATED`): the session's price
    at 09:20 from its five-minute `bars` (None: the caller has none it may read, or refused
    them), else `estimated_0920` of the day's open, within the day's `low`..`high`."""
    found = price_0920(day, bars) if bars else None
    if found is not None:
        return found[0], REAL
    return estimated_0920(open_, side, estimate, low=low, high=high), ESTIMATED


def gap_over_fill(open_: Decimal, high: Decimal, estimate: OpenTo0920 = OPEN_TO_0920) -> Decimal:
    """A daily-estimate entry whose session opened over its level: the buy's estimated price at
    09:20 (`estimated_0920`), at most the day's high (no fill above what traded)."""
    return estimated_0920(open_, "buy", estimate, high=high)
