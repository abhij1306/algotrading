"""The raw basis of `bars_5m` (audit D1): Fyers minute candles to raw prices, and the envelope
check of a day's 5-minute bars against the raw daily bar of the same day.

Fyers answers its history with prices adjusted for every split and bonus up to the day of the
request; `bars_daily_raw` and the live `BarBuilder` are raw. A candle of day `d` fetched on day `D`
is on the basis `raw x F`, where `F` is the product of the price factors of the stored corporate
actions with `d < ex_date <= D`: the factors `bars_daily_cap_adj` multiplies by
(`chains.stored_adjustment`, once per ISIN chain under its current id, HAT-DATA-053; dividends are
not in them). `to_raw` divides prices by `F` and multiplies the volume by it (the
inverse of the adjusted view's volume rule), so the store holds one basis whatever day a symbol was
downloaded.

The corporate-action table can be incomplete, and a day's candles can be wrong, so the conversion
is never trusted alone: `check_day` compares the day's 5-minute bars with the raw daily bar
(`bars_daily_raw`, the latest ingest). The day's highest high and lowest low must equal the daily
high and low within `tolerance` (a fraction), and the first open and the last close must lie in
the daily range widened by it. The close is not compared with the daily close: NSE's close is the
closing-auction price, not the last trade. The daily open is the pre-open auction price and the
5-minute bars start at 09:15, so a stock that opened at its high (or low) on a gap and never
traded back to it has 5-minute bars that miss that extreme: such a miss is accepted when the
official open fully explains it (the daily high is the open within `tolerance`, and no bar is
above it; the same for the low), and only then. A day that fails, or has no daily bar, is not
written and goes to the reject ledger instead (fails closed: a missing bar, never a wrong one).
`open_explained_rejects` counts the ledgered day refusals the open could explain.

`misfit_days` runs the same envelope over every stored day, and `mend_day` decides what a stored
day that fails it becomes (`bars5m_repair`; `bars5m_import` refuses to run while one is left).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from ..money import MONEY_SCALE
from .bars import Bar
from .bars_store import DATASET, FILE_NAME, BarStore, RejectRow
from .minute_limits import MinuteHistoryError
from .store import RAW_LATEST, ActionFactor, DataStore

__all__ = [
    "BASIS_MISMATCH",
    "CONVERTED",
    "KEPT",
    "NO_DAILY_BAR",
    "DailyRange",
    "DailySource",
    "RawBasis",
    "check_day",
    "explained_rejects",
    "fyers_factor",
    "mend_day",
    "misfit_days",
    "open_explains",
    "store_daily",
    "to_raw",
]

BASIS_MISMATCH = "basis_mismatch"  # the day's bars do not fit the raw daily bar
NO_DAILY_BAR = "no_daily_bar"  # there is no raw daily bar to check the day against
KEPT = "kept"  # a stored day that fits as it is
CONVERTED = "converted"  # a stored day that fits once converted to the raw basis
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
        _meets(high, daily.high, daily, tolerance, upper=True)
        and _meets(low, daily.low, daily, tolerance, upper=False)
        and floor <= bars[0].open <= ceiling
        and floor <= bars[-1].close <= ceiling
    )
    return None if fits else BASIS_MISMATCH


def _meets(
    extreme: Decimal, official: Decimal, daily: DailyRange, tolerance: Decimal, *, upper: bool
) -> bool:
    """The bars' `extreme` (high when `upper`, else low) equals the daily one within
    `tolerance`, or falls short of it only because the auction open made it: the daily extreme
    is the open within `tolerance` and the bars never pass it."""
    band = official * tolerance
    if abs(extreme - official) <= band:
        return True
    if abs(daily.open - official) > band:
        return False
    return extreme < official if upper else extreme > official


def open_explains(daily: DailyRange, tolerance: Decimal) -> bool:
    """The official open is the day's high or its low within `tolerance`: the one case where
    5-minute bars may miss a daily extreme and still fit (`check_day`)."""
    return (
        abs(daily.open - daily.high) <= daily.high * tolerance
        or abs(daily.open - daily.low) <= daily.low * tolerance
    )


def explained_rejects(
    store: BarStore, daily: DailySource, tolerance: Decimal
) -> tuple[list[RejectRow], list[RejectRow]]:
    """The ledger's `BASIS_MISMATCH` day refusals whose day is still not stored, oldest first,
    and those among them whose raw daily bar the open explains (`open_explains`): the days the
    accepted open-explained miss can let in once refetched (the ledger keeps no bars, so the
    rest of the envelope is checked only then)."""
    refused = sorted(
        {
            (r.day, r.instrument_id): r
            for r in store.read_rejects()
            if r.rule == BASIS_MISMATCH and r.bar_start is None and r.day is not None
        }.items()
    )
    stored: dict[date, set[str]] = {}
    missing = []
    for (day, instrument_id), row in refused:
        if instrument_id not in stored.setdefault(day, store.instruments(day)):
            missing.append(row)
    if not missing:
        return [], []
    found = daily(
        sorted({r.instrument_id for r in missing}),
        min(r.day for r in missing if r.day is not None),
        max(r.day for r in missing if r.day is not None),
    )
    explained = []
    for row in missing:
        bar = found.get((row.instrument_id, row.day)) if row.day is not None else None
        if bar is not None and open_explains(bar, tolerance):
            explained.append(row)
    return missing, explained


def mend_day(
    bars: Sequence[Bar], factor: Decimal, daily: DailyRange | None, tolerance: Decimal
) -> tuple[str, list[Bar]]:
    """A stored day's bars on the raw basis: `KEPT` and the bars when they fit `daily` as
    stored; else `CONVERTED` and the bars `to_raw` by `factor` (the adjustment an earlier
    download wrote them with) when those fit; else the reject code and no bars. A day that fits
    as stored is never converted, so mending a mended day changes nothing."""
    code = check_day(bars, daily, tolerance)
    if code is None:
        return KEPT, list(bars)
    if factor != 1:
        raw = [to_raw(b, factor) for b in bars]
        if check_day(raw, daily, tolerance) is None:
            return CONVERTED, raw
    return code, []


class RawBasis:
    """The adjusted views' factors (by chain id) and the raw daily bars the loader converts and
    checks against."""

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

    def actions(self, instrument_id: str) -> list[tuple[date, Decimal]]:
        """The (ex-date, factor) of the chain's stored actions."""
        return list(self._actions.get(instrument_id, ()))


_DAILY_SQL = f"""
SELECT CAST(instrument_id AS VARCHAR), date, open, high, low, close FROM {RAW_LATEST}
WHERE date BETWEEN ? AND ? AND list_contains(?, CAST(instrument_id AS VARCHAR))
"""  # noqa: S608


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


# The envelope of `check_day` over the stored (instrument, day) pairs of some day files, up to the
# last raw daily bar (later days, today's live bars, have no daily bar yet).
_MISFITS_SQL = f"""
WITH day AS (
    SELECT instrument_id, CAST(timezone('Asia/Kolkata', bar_start) AS DATE) AS day,
        max(high) AS high, min(low) AS low,
        arg_min(open, bar_start) AS open, arg_max(close, bar_start) AS close
    FROM read_parquet($files) GROUP BY ALL
),
raw AS (
    SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date, open, high, low
    FROM {RAW_LATEST} WHERE date BETWEEN $first AND $final
)
SELECT d.instrument_id, d.day FROM day d
LEFT JOIN raw r ON r.instrument_id = d.instrument_id AND r.date = d.day
WHERE d.day <= $last AND (
    r.high IS NULL
    OR (abs(d.high - r.high) > r.high * $tol
        AND NOT (abs(r.open - r.high) <= r.high * $tol AND d.high < r.high))
    OR (abs(d.low - r.low) > r.low * $tol
        AND NOT (abs(r.open - r.low) <= r.low * $tol AND d.low > r.low))
    OR d.open NOT BETWEEN r.low * (1 - $tol) AND r.high * (1 + $tol)
    OR d.close NOT BETWEEN r.low * (1 - $tol) AND r.high * (1 + $tol)
)
"""  # noqa: S608
_MISFIT_CHUNK = 60  # day files per query: the aggregate stays well inside the DuckDB memory cap


def misfit_days(store: DataStore, tolerance: Decimal) -> list[tuple[str, date]]:
    """Every stored `bars_5m` (instrument, day) that `check_day` refuses against the raw daily
    bar (none, or outside the envelope), oldest first; `_MISFIT_CHUNK` day files per query.
    Stored days without any raw daily bar raise `bars5m_no_daily_bars` (fails closed: none
    of them can be vouched for, and none is dropped for it)."""
    files = sorted((store.root / DATASET).glob(f"date=*/{FILE_NAME}"))
    found: list[tuple[str, date]] = []
    with closing(store.connect()) as con:
        last = con.execute(f"SELECT max(date) FROM {RAW_LATEST}").fetchone()  # noqa: S608
        if not files:
            return []
        if last is None or last[0] is None:  # stored days with nothing to check them against
            raise MinuteHistoryError("bars5m_no_daily_bars")
        for at in range(0, len(files), _MISFIT_CHUNK):
            chunk = files[at : at + _MISFIT_CHUNK]
            first, final = (date.fromisoformat(f.parent.name[5:]) for f in (chunk[0], chunk[-1]))
            rows = con.execute(
                _MISFITS_SQL,
                {
                    "files": [str(f) for f in chunk],
                    "first": first,
                    "final": final,
                    "last": last[0],
                    "tol": tolerance,
                },
            ).fetchall()
            found.extend((str(i), day) for i, day in rows)
    return sorted(found, key=lambda pair: (pair[1], pair[0]))


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data
    """python -m app.hat.data.bars_basis explained-rejects [--out FILE]

    Counts the ledgered `basis_mismatch` days under `HAT_DATA_ROOT` still without bars, and those
    whose raw daily bar opened at its high or low (`explained_rejects`), the days a refetch can
    now store; `--out` writes those (symbol, day) pairs as CSV. Reads only the data root."""
    import argparse
    import csv
    import sys

    from ..settings import operator_settings

    parser = argparse.ArgumentParser(prog="python -m app.hat.data.bars_basis")
    parser.add_argument("command", choices=["explained-rejects"])
    parser.add_argument("--out", help="write the explained (symbol, day) pairs here as CSV")
    args = parser.parse_args(argv)
    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    tolerance = settings.history_envelope_tol
    missing, explained = explained_rejects(
        BarStore(settings.data_root), store_daily(store), tolerance
    )
    sys.stdout.write(
        f"basis_mismatch days without bars: {len(missing)}; "
        f"opened at their high or low (the open explains a miss; a refetch checks the rest): "
        f"{len(explained)}\n"
    )
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "instrument_id", "day"])
            writer.writerows((r.symbol, r.instrument_id, r.day) for r in explained)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
