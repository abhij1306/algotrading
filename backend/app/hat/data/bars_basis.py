"""The raw basis of `bars_5m` (audit D1): Fyers minute candles to raw prices, and the envelope
check of a day's 5-minute bars against the raw daily bar of the same day.

Fyers answers its history with prices adjusted for every split and bonus up to the day of the
request; `bars_daily_raw` and the live `BarBuilder` are raw. A candle of day `d` fetched on day
`D` is on the basis `raw x F`, where `F` is the product of the price factors of the stored
corporate actions with `d < ex_date <= D` (the factors `bars_daily_adj` multiplies by, HAT-DATA-
053). `to_raw` divides prices by `F` and multiplies the volume by it (the inverse of the adjusted
view's volume rule), so the store holds one basis whatever day a symbol was downloaded.

The corporate-action table can be incomplete, and a day's candles can be wrong, so the conversion
is never trusted alone: `check_day` compares the day's 5-minute bars with the raw daily bar
(`bars_daily_raw`, the latest ingest). The day's highest high and lowest low must equal the daily
high and low within `tolerance` (a fraction), and the first open and the last close must lie in
the daily range widened by it. The close is not compared with the daily close: NSE's close is the
closing-auction price, not the last trade. A day that fails, or has no daily bar, is not written
and goes to the reject ledger instead (fails closed: a missing bar, never a wrong one).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from ..money import MONEY_SCALE
from .bars import Bar
from .store import ActionFactor, DataStore

__all__ = [
    "BASIS_MISMATCH",
    "NO_DAILY_BAR",
    "DailyRange",
    "DailySource",
    "RawBasis",
    "check_day",
    "fyers_factor",
    "store_daily",
    "to_raw",
]

BASIS_MISMATCH = "basis_mismatch"  # the day's bars do not fit the raw daily bar
NO_DAILY_BAR = "no_daily_bar"  # there is no raw daily bar to check the day against
_PRICE_STEP = Decimal(1).scaleb(-MONEY_SCALE)


@dataclass(frozen=True)
class DailyRange:
    """What the envelope check needs of one raw daily bar."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


# (instrument ids, first day, last day) -> the raw daily bar of each (instrument id, day) held
DailySource = Callable[[Sequence[str], date, date], Mapping[tuple[str, date], DailyRange]]


def fyers_factor(actions: Iterable[tuple[date, Decimal]], day: date, fetched_on: date) -> Decimal:
    """The factor a candle of `day` fetched on `fetched_on` carries: the product of the factors
    of the actions `(ex_date, factor)` with `day < ex_date <= fetched_on` (1 when none)."""
    product = Decimal(1)
    for ex_date, factor in actions:
        if day < ex_date <= fetched_on:
            product *= factor
    return product


def to_raw(bar: Bar, factor: Decimal) -> Bar:
    """`bar` on the raw basis: prices divided by `factor` at the store's 4 places, the volume
    multiplied by it (half-even to whole shares). A factor of 1 returns the bar unchanged."""
    if factor == 1:
        return bar

    def price(value: Decimal) -> Decimal:
        return (value / factor).quantize(_PRICE_STEP, ROUND_HALF_EVEN)

    volume = int((bar.volume * factor).to_integral_value(ROUND_HALF_EVEN))
    return Bar(
        bar.symbol,
        bar.start,
        price(bar.open),
        price(bar.high),
        price(bar.low),
        price(bar.close),
        volume,
    )


def check_day(bars: Sequence[Bar], daily: DailyRange | None, tolerance: Decimal) -> str | None:
    """None when one day's 5-minute bars (ordered by start, at least one) fit the raw daily bar
    `daily` within `tolerance`, else the reject code (see the module docstring)."""
    if daily is None:
        return NO_DAILY_BAR
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    floor = daily.low * (1 - tolerance)
    ceiling = daily.high * (1 + tolerance)
    fits = (
        abs(high - daily.high) <= daily.high * tolerance
        and abs(low - daily.low) <= daily.low * tolerance
        and floor <= bars[0].open <= ceiling
        and floor <= bars[-1].close <= ceiling
    )
    return None if fits else BASIS_MISMATCH


class RawBasis:
    """The stored corporate actions (by instrument id) and the raw daily bars the loader
    converts and checks against."""

    def __init__(
        self, actions: Iterable[ActionFactor], daily: DailySource, tolerance: Decimal
    ) -> None:
        self._actions: dict[str, list[tuple[date, Decimal]]] = {}
        for a in actions:
            self._actions.setdefault(str(a.instrument_id), []).append((a.ex_date, a.factor))
        self.daily = daily
        self.tolerance = tolerance

    def factor(self, instrument_id: str, day: date, fetched_on: date) -> Decimal:
        return fyers_factor(self._actions.get(instrument_id, ()), day, fetched_on)


_DAILY_SQL = """
SELECT CAST(instrument_id AS VARCHAR), date, open, high, low, close FROM bars_daily_raw
WHERE date BETWEEN ? AND ? AND list_contains(?, CAST(instrument_id AS VARCHAR))
QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1
"""


def store_daily(store: DataStore) -> DailySource:
    """The raw daily bars of `store`, read through DuckDB for the instruments and days asked for
    (never the whole table)."""

    def daily(
        instrument_ids: Sequence[str], first: date, last: date
    ) -> dict[tuple[str, date], DailyRange]:
        with closing(store.connect()) as con:
            rows = con.execute(_DAILY_SQL, [first, last, list(instrument_ids)]).fetchall()
        return {
            (instrument_id, day): DailyRange(open_, high, low, close)
            for instrument_id, day, open_, high, low, close in rows
        }

    return daily
