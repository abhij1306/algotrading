"""The market a backtest reads (HAT-BT-011, HAT-BT-012), loaded point in time, and the day loop's
views of it.

Bars come from the adjusted view of the Parquet store (`bars_daily_adj`: raw bars times the stored
corporate-action factors, the latest ingest run's row per instrument and day, read for the
universe's instruments only), features from the `features_daily` partitions the nightly job wrote,
the regime index from `index_daily`, and membership from the universe's intervals, delisted
instruments included. Nothing is filtered by what happened later: the day loop reads each input
only up to the date it decides for (`MarketView`). The features must be the ones the nightly
job wrote for the corporate actions the bars are adjusted with: the job's stored inputs digest
must equal the digest of the stored actions over the whole store (HAT-DATA-077, PR 67). A run
under the `policy` regime rule also reads the regime engine's inputs: the NIFTY 500 and India VIX
closes and the features job's breadth of each session (PR 68).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.features import STATE_FILE, inputs_digest
from ..data.identity import series_changes
from ..data.lineage import stored_adjustment, unexplained_jumps
from ..data.store import (
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    Adjustment,
    DataStore,
    latest_rows,
)
from ..data.ticks import scaled_tick, tick_for_date
from ..engine.portfolio import regime
from ..engine.portfolio.universe import Interval
from ..engine.stops import lowest, mean_true_range, sma
from ..models import HatIndustry, HatInstrument, HatUniverseMember
from .spec import BacktestError

__all__ = [
    "JUMP_SKIP_SESSIONS",
    "LOOKBACK_SESSIONS",
    "STOPPED_SLACK_SESSIONS",
    "Bar",
    "MarketData",
    "MarketView",
    "classifications",
    "industry_on",
    "jump_skips",
    "load_market",
    "stopped_trading",
]

# The scan reads at most 652 sessions back (a 400-session SMA compared 252 sessions earlier);
# the window it is given holds more, so its watchlist equals a scan of the full history.
LOOKBACK_SESSIONS = 700
_PRICE_PLACES = Decimal("0.0001")
_ALWAYS = date(1900, 1, 1)
_EPOCH = date(1970, 1, 1)  # polars' Date is days since this day
# Sessions without a bar, up to the store's last day, after which an instrument that never trades
# again counts as delisted (`MarketData.last_traded`).
STOPPED_SLACK_SESSIONS = 20
# Sessions either side of an open review jump whose bars are not traded (`MarketData.skipped`).
JUMP_SKIP_SESSIONS = 10
# The exact prices in 1/10,000 of a rupee (the store's 4 places), beside the scan's doubles.
_EXACT = ("open_e4", "high_e4", "low_e4", "close_e4")
_BAR_COLUMNS = (
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "traded_value",
    "raw_close",
    *_EXACT,
)


@dataclass(frozen=True)
class MarketData:
    """Everything a run reads, point in time.

    `days`: every session of the store, sorted. `bars`: the universe's adjusted daily bars,
    sorted by instrument and date (`instrument_id` text, `date`, `open`, `high`, `low`, `close`,
    `prev_close`, `volume`, `traded_value`, and `raw_close`, the unadjusted close the rules on
    a rupee price read (`min_price`, the tick slab), as doubles, for the scan, and `open_e4` ..
    `close_e4`, the exact 4-place prices as integers of 1/10,000, for the day loop's Decimals).
    `features`: the daily features (`date`, `instrument_id`, columns). `index_closes`:
    per index name, (date, close) oldest first. `members`: the universe's membership intervals,
    delisted instruments included. `industries`: each instrument's industry, either one name
    or its classifications as (effective_from, industry), read point in time. `series`: each
    instrument's series as change points (first day, series) from its bars, so a scan reads
    the series of its own day (`identity.series_on`), not today's. `breadth`: the features job's
    `breadth_pct` per session, read with the `regime.INDEX_NAME` and `regime.VIX_NAME` closes of
    `index_closes` only by a run under the `policy` regime rule (PR 68).

    `last_traded`: the last session of each instrument delisted by the period's end, where the
    day loop closes a position in it (HAT-BT-012). It is read from the bars (`stopped_trading`),
    by chain id, so a linked older ISIN is not a delisting: an instrument whose last bar is on or
    before the period's end, which has no bar after it anywhere in the store, and whose last bar
    is more than `STOPPED_SLACK_SESSIONS` sessions before the store's last day, has stopped
    trading. A gap of up to the slack before the store's end may be a suspension that resumes,
    so it is not called a delisting (the position is carried and marked down, `fills`); a
    suspension that resumes later in the store is never one. `delisted_on`, set by the live
    listing refresh, wins when it is set and gives an earlier last session.

    `skipped`: the instrument-days with a bar within `JUMP_SKIP_SESSIONS` sessions (either side,
    the jump day included) of an open jump of the lineage review list
    (`lineage.unexplained_jumps`: a price-changing action with no stored factor, such as a
    demerger whose ratio the owner has not set; a reviewed one has its factor and is no longer a
    jump). Their bars stay in `bars` for the scan, but the day loop does not trade or mark on
    them (`MarketView.bar`), though their closes still set the tick and the indicators (they
    are real sessions), and they do not count as sessions without a bar for the suspension
    mark. An interim rule (audit H7) until the review list's actions are loaded; the run's
    snapshot reports how many instrument-days it skipped.
    """

    days: tuple[date, ...]
    bars: pl.DataFrame
    features: pl.DataFrame
    index_closes: Mapping[str, Sequence[tuple[date, Decimal]]]
    members: tuple[Interval, ...]
    industries: Mapping[str, str | Sequence[tuple[date, str]]] = field(default_factory=dict)
    series: Mapping[str, Sequence[tuple[date, str]]] = field(default_factory=dict)
    last_traded: Mapping[str, date] = field(default_factory=dict)
    # 5-minute bars of a session, keyed (instrument_id, date): (high, low) per bar in time order,
    # the 09:15 bar first, on the daily bars' (adjusted) scale. A session without them uses the
    # daily estimate (PR 60).
    intraday: Mapping[tuple[str, date], Sequence[tuple[Decimal, Decimal]]] = field(
        default_factory=dict
    )
    breadth: Mapping[date, Decimal] = field(default_factory=dict)
    skipped: frozenset[tuple[str, date]] = frozenset()


def classifications(data: MarketData, instrument_id: str) -> list[tuple[date, str]]:
    """The instrument's industry classifications, oldest first."""
    found = data.industries.get(instrument_id, "unknown")
    if isinstance(found, str):
        return [(_ALWAYS, found)]
    return sorted(found) or [(_ALWAYS, "unknown")]


def industry_on(data: MarketData, instrument_id: str, day: date) -> str:
    """The industry in force on `day`: the newest classification effective on or before it."""
    rows = [name for start, name in classifications(data, instrument_id) if start <= day]
    return rows[-1] if rows else "unknown"


def _price(value: object) -> Decimal | None:
    """A stored price as an exact Decimal of the store's 4 places; None when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        number = Decimal(repr(value))
    else:
        return None
    return number.quantize(_PRICE_PLACES) if number.is_finite() else None


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _ordinal(day: date) -> int:
    return (day - _EPOCH).days


def _exact(units: object) -> Decimal:
    """A price held in 1/10,000 as the Decimal of the store's 4 places."""
    return Decimal(int(units)).scaleb(-4)  # type: ignore[call-overload]


@dataclass(frozen=True)
class _Series:
    """One instrument's bars with four positive prices, columnar: the dates as days since
    `_EPOCH`, ascending, (open, high, low, close) in 1/10,000 per row, and per row the row of
    the last bar on or before it that is not skipped near a jump (-1 before one)."""

    days: np.ndarray
    prices: np.ndarray
    traded: np.ndarray

    def bars(self, start: int, stop: int) -> list[Bar]:
        return [Bar(*(_exact(v) for v in row)) for row in self.prices[start:stop]]


_NO_SERIES = _Series(
    np.empty(0, dtype=np.int32), np.empty((0, 4), dtype=np.int64), np.empty(0, dtype=np.int64)
)


@dataclass(frozen=True)
class _RawCloses:
    """One instrument's bars with a positive raw close, whatever their other prices (live reads
    every raw close for the tick, `daily.market._RAW_SQL`): the dates as days since `_EPOCH`,
    ascending, and the raw and adjusted closes in 1/10,000 (adjusted 0 when absent)."""

    days: np.ndarray
    raw: np.ndarray
    adjusted: np.ndarray


_NO_RAW = _RawCloses(
    np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
)


class MarketView:
    """The point-in-time reads the day loop makes of `MarketData` (HAT-BT-011): each session's
    tradeable bars as exact Decimals, an instrument's bars up to a day and the SMA, swing low,
    ATR and NSE tick live computes from them, the features of a session and the closes of the
    strategy's regime index (`index`). A bar without four positive prices is not tradeable and
    is left out; a skipped one (`MarketData.skipped`) is not traded on (`bar`, `last_close`)
    but stays in the history the indicators and the tick read. The bars are held per instrument as
    arrays; a `Bar` of Decimals is built only for the bars a read returns."""

    def __init__(self, data: MarketData, index: str) -> None:
        self.days = tuple(sorted(set(data.days)))
        self.index = {d: n for n, d in enumerate(self.days)}
        self.bars = data.bars
        self.series = self._series(data.skipped)
        self.raw_closes = self._raw_closes()
        self.skipped: dict[str, list[date]] = {}
        for instrument_id, day in sorted(data.skipped):
            self.skipped.setdefault(instrument_id, []).append(day)
        self.feature_rows: dict[date, dict[str, dict[str, Any]]] = {}
        self.features = {
            key[0]: part for key, part in data.features.partition_by("date", as_dict=True).items()
        }
        self.closes = list(data.index_closes.get(index, ()))

    def _series(self, skipped: Collection[tuple[str, date]]) -> dict[str, _Series]:
        # a bar without four positive prices cannot be traded on
        frame = self.bars.select("instrument_id", "date", *_EXACT).filter(
            pl.all_horizontal(pl.col(c) > 0 for c in _EXACT).fill_null(value=False)
        )
        # A skipped bar stays in the history: it is a real session, whose close sets the tick and
        # the SMA, ATR and swing low; only the loop's trades and marks leave it out.
        left_out = pl.DataFrame(
            sorted(skipped), schema={"instrument_id": pl.String, "date": pl.Date}, orient="row"
        ).with_columns(skipped=pl.lit(value=True))
        frame = frame.join(left_out, on=["instrument_id", "date"], how="left")
        found: dict[str, _Series] = {}
        for key, part in frame.partition_by("instrument_id", as_dict=True).items():
            rows = np.arange(part.height)
            kept = np.where(part["skipped"].is_null().to_numpy(), rows, -1)
            found[str(key[0])] = _Series(
                part["date"].to_physical().to_numpy(),
                part.select(_EXACT).to_numpy(),
                np.maximum.accumulate(kept) if part.height else kept,
            )
        return found

    def _raw_closes(self) -> dict[str, _RawCloses]:
        frame = self.bars.select(
            "instrument_id",
            "date",
            # a raw price has at most the store's 4 places, so the double rounds back exactly
            raw=(pl.col("raw_close") * 10_000).round(0).cast(pl.Int64),
            adjusted=pl.col("close_e4").fill_null(0),
        ).filter(pl.col("raw") > 0)
        return {
            str(key[0]): _RawCloses(
                part["date"].to_physical().to_numpy(),
                part["raw"].to_numpy(),
                part["adjusted"].to_numpy(),
            )
            for key, part in frame.partition_by("instrument_id", as_dict=True).items()
        }

    def _find(self, instrument_id: str, day: date, *, inclusive: bool) -> tuple[_Series, int]:
        """The instrument's bars and the count of them dated on or before `day` (before it, if
        not `inclusive`)."""
        series = self.series.get(instrument_id, _NO_SERIES)
        side = "right" if inclusive else "left"
        return series, int(np.searchsorted(series.days, _ordinal(day), side=side))

    def bar(self, instrument_id: str, day: date) -> Bar | None:
        """The instrument's tradeable bar of `day`; None without one."""
        series, at = self._find(instrument_id, day, inclusive=False)
        if at < len(series.days) and series.days[at] == _ordinal(day) and series.traded[at] == at:
            return series.bars(at, at + 1)[0]
        return None

    def last_close(self, instrument_id: str, day: date) -> tuple[date, Decimal] | None:
        """The instrument's last tradeable session on or before `day` and its close; None
        without one."""
        series, end = self._find(instrument_id, day, inclusive=True)
        at = int(series.traded[end - 1]) if end else -1
        if at < 0:
            return None
        return _EPOCH + timedelta(days=int(series.days[at])), _exact(series.prices[at][3])

    def skipped_after(self, instrument_id: str, after: date, through: date) -> int:
        """The count of the instrument's skipped sessions (`MarketData.skipped`) after `after`, up
        to `through`."""
        days = self.skipped.get(instrument_id, [])
        return bisect_right(days, through) - bisect_right(days, after)

    def prev(self, day: date) -> date | None:
        n = self.index[day]
        return self.days[n - 1] if n > 0 else None

    def closes_through(self, day: date) -> list[Decimal]:
        return [c for d, c in self.closes if d <= day]

    def feature(self, day: date, instrument_id: str, name: str) -> Decimal | None:
        rows = self.feature_rows.get(day)
        if rows is None:
            frame = self.features.get(day)
            rows = {} if frame is None else {str(r["instrument_id"]): r for r in frame.to_dicts()}
            self.feature_rows[day] = rows
        return _price(rows.get(instrument_id, {}).get(name))

    def bars_through(
        self, instrument_id: str, day: date, count: int, *, inclusive: bool = True
    ) -> list[Bar]:
        """The instrument's last `count` bars dated on or before `day` (before it, if not
        `inclusive`), oldest first."""
        series, end = self._find(instrument_id, day, inclusive=inclusive)
        return series.bars(max(0, end - count), end)

    def tick(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's NSE tick on `day` (HAT-EXEC-053) in the units of its adjusted bars.
        The slab is picked from its raw closes before `day`, as the live trigger prices it (a
        later split must not move a day to a finer slab), and the tick is scaled by the
        adjustment of its bar of `day` (or the last before it, `ticks.scaled_tick`): on a bar no
        action adjusts it is the NSE tick itself. None without an earlier raw close."""
        closes = self.raw_closes.get(instrument_id, _NO_RAW)
        end = int(np.searchsorted(closes.days, _ordinal(day), side="left"))
        month = int(np.searchsorted(closes.days[:end], _ordinal(day.replace(day=1)), side="left"))
        # the only closes the tick reads: the last before the month, and the last before `day`
        rows = sorted({n for n in (month - 1, end - 1) if n >= 0})
        dates = [_EPOCH + timedelta(days=int(closes.days[n])) for n in rows]
        tick = tick_for_date(dates, [_exact(closes.raw[n]) for n in rows], day)
        through = int(np.searchsorted(closes.days, _ordinal(day), side="right"))
        if tick is None or not through:
            return None
        last = through - 1
        return scaled_tick(tick, _exact(closes.adjusted[last]), _exact(closes.raw[last]))

    def sma_through(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        """The `length`-session SMA of the closes up to `day`."""
        bars = self.bars_through(instrument_id, day, length)
        return sma([b.close for b in bars], length)

    def lowest_low_before(self, instrument_id: str, before: date, lookback: int) -> Decimal | None:
        """The lowest low of the `lookback` sessions the instrument traded before `before`."""
        bars = self.bars_through(instrument_id, before, lookback, inclusive=False)
        return lowest([b.low for b in bars], lookback)

    def atr(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        """The mean true range of the `length` sessions up to `day`."""
        window = self.bars_through(instrument_id, day, length + 1)
        return mean_true_range(
            [b.high for b in window], [b.low for b in window], [b.close for b in window], length
        )

    def window(self, as_of: date) -> pl.DataFrame:
        """The bars of the `LOOKBACK_SESSIONS` sessions up to `as_of`: what the scan reads."""
        first = self.days[max(0, self.index[as_of] - LOOKBACK_SESSIONS)]
        return self.bars.filter((pl.col("date") >= first) & (pl.col("date") <= as_of))

    def prev_close(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's last close before `day`; None without one."""
        bars = self.bars_through(instrument_id, day, 1, inclusive=False)
        return bars[0].close if bars else None


_DIGEST_SLICE = 100_000  # rows per CSV slice of the content digest
FEATURES_LOCK = ".features.lock"  # the lock `data.features.run_features` holds while it writes

# The universe's adjusted rows of every ingest run, deduped in polars (`latest_rows`, as
# `features.load_bars` does: a window in SQL costs several times the memory of the rows), with
# the series each bar traded under (a dated attribute, audit D5), sorted by DuckDB within its
# memory limit (a sort in polars would copy the rows). The prices come as doubles for
# the scan, as live reads them, and exactly, as integers of 1/10,000 (`_PRICE_E4_SQL`).
_BARS_SQL = """
SELECT instrument_id, date, series,
       CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value,
       CAST(raw_close AS DOUBLE) AS raw_close,
       price_e4(open) AS open_e4,
       price_e4(high) AS high_e4,
       price_e4(low) AS low_e4,
       price_e4(close) AS close_e4,
       ingest_run_id
FROM bars_daily_adj
WHERE date BETWEEN ? AND ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
ORDER BY instrument_id, date
"""
# An adjusted price rounded to the store's 4 places half to even, as `Decimal.quantize` (and so
# `_price`) rounds, in exact decimal arithmetic, as an integer of 1/10,000. A cast to DECIMAL(18,4)
# would round a half away from zero.
_PRICE_E4_SQL = """
CREATE TEMP MACRO price_e4(p) AS (
    CAST(floor(p * 10000) AS BIGINT) + CASE
        WHEN p * 10000 - floor(p * 10000) > 0.5 THEN 1
        WHEN p * 10000 - floor(p * 10000) = 0.5 THEN CAST(floor(p * 10000) AS BIGINT) % 2
        ELSE 0 END)
"""
# the universe's instruments that still trade after the run's end
_LATER_SQL = """
SELECT DISTINCT instrument_id FROM bars_daily_adj
WHERE date > ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""
# every index the run reads, in one statement, so a concurrent upsert cannot split them
_INDEX_SQL = """
SELECT index_name, date, CAST(close AS VARCHAR) FROM index_daily
WHERE list_contains(?, index_name) AND date <= ? ORDER BY index_name, date
"""


def _members(
    session: Session, universe_id: uuid.UUID, lineage: Mapping[str, str]
) -> tuple[Interval, ...]:
    """The universe's membership intervals; a linked older ISIN's are its chain's, whose id its
    bars carry in `bars_daily`."""
    rows = session.scalars(
        select(HatUniverseMember)
        .where(HatUniverseMember.universe_id == universe_id)
        .order_by(HatUniverseMember.instrument_id, HatUniverseMember.effective_from)
    ).all()
    return tuple(
        Interval(_chain_id(r.instrument_id, lineage), r.effective_from, r.effective_to)
        for r in rows
    )


def _chain_id(instrument_id: uuid.UUID, lineage: Mapping[str, str]) -> uuid.UUID:
    chain = lineage.get(str(instrument_id))
    return instrument_id if chain is None else uuid.UUID(chain)


def _industries(
    session: Session, instruments: Iterable[uuid.UUID], lineage: Mapping[str, str]
) -> dict[str, list[tuple[date, str]]]:
    """Each instrument's industry classifications as (effective_from, industry), oldest first:
    the run reads the one in force on each trade date, never a later reclassification. A
    linked older ISIN's classifications count for its chain (the id the run knows it by)."""
    rows = session.scalars(select(HatIndustry))
    return chain_industries(
        ((r.instrument_id, r.effective_from, r.industry) for r in rows), instruments, lineage
    )


def chain_industries(
    rows: Iterable[tuple[uuid.UUID, date, str]],
    instruments: Iterable[uuid.UUID],
    lineage: Mapping[str, str],
) -> dict[str, list[tuple[date, str]]]:
    """(instrument, effective_from, industry) rows grouped under each wanted instrument's id,
    oldest first, a linked older ISIN's under its chain's id."""
    wanted = set(instruments)
    found: dict[str, list[tuple[date, str]]] = {}
    for instrument_id, effective_from, industry in rows:
        chain = _chain_id(instrument_id, lineage)
        if chain in wanted:
            found.setdefault(str(chain), []).append((effective_from, industry))
    return {k: sorted(v) for k, v in found.items()}


def _delisted(session: Session, instruments: Iterable[uuid.UUID]) -> dict[str, date]:
    wanted = set(instruments)
    return {
        str(row.id): row.delisted_on
        for row in session.scalars(select(HatInstrument))
        if row.id in wanted and row.delisted_on is not None
    }


def stopped_trading(
    last_bar: Mapping[str, date], later: Collection[str], days: Sequence[date], slack: int
) -> dict[str, date]:
    """Each instrument that stopped trading, at its last bar (`MarketData.last_traded`):
    `last_bar` is each instrument's last bar up to the run's end, `later` the instruments with a
    bar after the end, `days` every session of the store. One with a later bar, or whose last
    bar is no more than `slack` sessions before the store's last day, has not stopped."""
    if not days:
        return {}
    last = len(days) - 1
    return {
        instrument_id: day
        for instrument_id, day in last_bar.items()
        if instrument_id not in later and last - bisect_left(days, day) > slack
    }


def jump_skips(
    jumps: Iterable[tuple[str, date]], days: Sequence[date], sessions: int
) -> set[tuple[str, date]]:
    """The (instrument, session) pairs within `sessions` sessions of a jump of (instrument, day),
    either side, the jump day included (`MarketData.skipped`); `days` are the store's sessions."""
    found: set[tuple[str, date]] = set()
    for instrument_id, day in jumps:
        at = bisect_left(days, day)
        found.update((instrument_id, d) for d in days[max(0, at - sessions) : at + sessions + 1])
    return found


def _series_points(frame: pl.DataFrame) -> dict[str, list[tuple[date, str]]]:
    """`identity.series_changes` of the bars, fed only each instrument's first row and the rows
    whose series differs from the row before (`frame` sorted by instrument and date)."""
    by = pl.col("series").shift(1).over("instrument_id")
    first = pl.int_range(pl.len()).over("instrument_id") == 0
    changed = frame.select("instrument_id", "date", "series").filter(
        first | pl.col("series").ne_missing(by)
    )
    return series_changes(changed.iter_rows())


def _skipped(
    store: DataStore, frame: pl.DataFrame, adjustment: Adjustment, window: Sequence[date], end: date
) -> tuple[frozenset[tuple[str, date]], int]:
    """The universe's instrument-days with a bar near an open review jump (`MarketData.skipped`),
    and the count of those jumps."""
    wanted = set(frame["instrument_id"].unique().to_list())
    jumps = [
        (j.instrument_id, j.day)
        for j in unexplained_jumps(store, adjustment, window[0], until=end)
        if j.instrument_id in wanted
    ]
    near = jump_skips(jumps, window, JUMP_SKIP_SESSIONS)
    if not near:
        return frozenset(), len(jumps)
    pairs = pl.DataFrame(
        sorted(near), schema={"instrument_id": pl.String, "date": pl.Date}, orient="row"
    )
    found = frame.select("instrument_id", "date").join(pairs, on=["instrument_id", "date"])
    return frozenset(found.iter_rows()), len(jumps)


def _features(store: DataStore, days: Iterable[date], wanted: Sequence[str]) -> pl.DataFrame:
    """The features of `days` of the `wanted` instruments (the scan ranks within the universe, so
    no other row is read)."""
    frames = [
        part.with_columns(pl.lit(day).alias("date"))
        for day in days
        if (
            part := store.read_partition(DATASET_FEATURES, day)
            .with_columns(pl.col("instrument_id").cast(pl.String))
            .filter(pl.col("instrument_id").is_in(wanted))
        ).height
    ]
    if not frames:
        raise BacktestError("backtest_no_features")
    return pl.concat(frames, how="diagonal_relaxed")


def _features_digest(store: DataStore) -> str | None:
    """The inputs digest the last complete `features` run recorded; None when none did."""
    try:
        state = json.loads((store.root / DATASET_FEATURES / STATE_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    digest = state.get("inputs_digest") if isinstance(state, dict) else None
    return digest if isinstance(digest, str) else None


def _current_features(
    store: DataStore,
    digest: str,
    days: list[date],
    breadth_days: list[date],
    wanted: Sequence[str],
) -> tuple[pl.DataFrame, dict[date, Decimal]]:
    """The features of `days` and the breadth of `breadth_days`, read under the features job's
    lock (shared), so a run that is rewriting partitions finishes first and both come from one
    generation; `backtest_features_stale` unless the job's recorded digest is `digest` and
    every day has a partition."""
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / FEATURES_LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if _features_digest(store) != digest or not all(
            store.partition_files(DATASET_FEATURES, day) for day in days
        ):
            raise BacktestError("backtest_features_stale")
        return _features(store, days, wanted), _breadth(store, breadth_days)


def _breadth(store: DataStore, days: Iterable[date]) -> dict[date, Decimal]:
    """The features job's `breadth_pct` of each of `days` that has one, as the regime job
    reads it (`portfolio.jobs.read_breadth`)."""
    found: dict[date, Decimal] = {}
    for day in days:
        frame = store.read_partition(DATASET_BREADTH, day)
        if frame.height and frame["breadth_pct"][0] is not None:
            found[day] = Decimal(str(frame["breadth_pct"][0]))
    return found


def _content_digest(market: MarketData) -> str:
    """sha256 of what the run read: the bars, features, index closes, membership intervals and
    industry history, each in a fixed order. Two snapshots with the same row counts but
    different prices or members have different digests."""
    digest = hashlib.sha256()
    for frame in (market.bars, market.features):
        ordered = frame.select(sorted(frame.columns)).sort(["date", "instrument_id"])
        # the CSV of the whole frame, written a slice at a time
        for at in range(0, max(1, ordered.height), _DIGEST_SLICE):
            part = ordered.slice(at, _DIGEST_SLICE)
            digest.update(part.write_csv(include_header=at == 0).encode())
    for name, points in sorted(market.index_closes.items()):
        digest.update(repr((name, [(d.isoformat(), str(c)) for d, c in points])).encode())
    digest.update(
        repr(
            sorted((str(m.instrument_id), m.effective_from, m.effective_to) for m in market.members)
        ).encode()
    )
    digest.update(repr(sorted(market.industries.items())).encode())
    if market.breadth:
        digest.update(
            repr(sorted((d.isoformat(), str(v)) for d, v in market.breadth.items())).encode()
        )
    return digest.hexdigest()


def load_market(
    store: DataStore,
    session: Session,
    *,
    universe_id: uuid.UUID,
    index: str,
    start: date,
    end: date,
    regime_inputs: bool = False,
) -> tuple[MarketData, dict[str, Any]]:
    """The market of a run of `start..end` on the universe, and its data snapshot (what was
    read, for `hat_backtest_run.data_snapshot`). Raises `BacktestError` when the store has no
    session in the period or no features, and `backtest_features_stale` when the features were
    not written for the stored corporate actions over every instrument (the nightly job's
    digest, `universe` None) or a session of the window has none: the loop would read prices
    adjusted one way and features computed another, or miss a night's scan, which the nightly
    job never does. With `regime_inputs` it also reads the regime engine's index and VIX closes
    and the window's breadth, and raises `backtest_no_regime_inputs` when the store has none
    of one of them (PR 68)."""
    every_day = store.days(DATASET_BARS)
    days = [d for d in every_day if d <= end]
    first = bisect_left(days, start)
    if first >= len(days):
        raise BacktestError("backtest_no_data")
    window = days[max(0, first - LOOKBACK_SESSIONS) :]
    adjustment = stored_adjustment(session)
    expected = inputs_digest(adjustment, None)
    members = _members(session, universe_id, adjustment.lineage)
    instruments = {m.instrument_id for m in members}
    wanted = sorted(str(i) for i in instruments)
    with store.connect(adjustment) as con:
        con.execute(_PRICE_E4_SQL)
        frame = con.execute(_BARS_SQL, [window[0], end, wanted]).pl()
        later = (
            {r[0] for r in con.execute(_LATER_SQL, [end, wanted]).fetchall()}
            if every_day[-1] > end
            else set()
        )
        names = sorted({index, *((regime.INDEX_NAME, regime.VIX_NAME) if regime_inputs else ())})
        rows = con.execute(_INDEX_SQL, [names, end]).fetchall()
    # read once DuckDB has let go of the bars' memory
    features, breadth = _current_features(
        store, expected, days[max(0, first - 1) :], window if regime_inputs else [], wanted
    )
    index_closes: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in names}
    for name, day, close in rows:
        index_closes[name].append((day, Decimal(close)))
    closes = index_closes[index]
    if regime_inputs and not (
        breadth and all(index_closes[n] for n in (regime.INDEX_NAME, regime.VIX_NAME))
    ):
        raise BacktestError("backtest_no_regime_inputs")
    frame = latest_rows(frame)
    series = _series_points(frame)
    bars = frame.select(_BAR_COLUMNS)
    del frame
    last_bar = dict(bars.group_by("instrument_id").agg(pl.col("date").max()).iter_rows())
    last_traded = stopped_trading(last_bar, later, every_day, STOPPED_SLACK_SESSIONS)
    # Only an instrument delisted by the period's end has a last traded session in the run; one
    # delisted later simply has no more bars after `end`, which is not a delisting.
    for instrument_id, delisted_on in _delisted(session, instruments).items():
        if delisted_on > end:
            continue
        dates = bars.filter(
            (pl.col("instrument_id") == instrument_id) & (pl.col("date") <= delisted_on)
        )["date"]
        if dates.len():
            found: date = dates.max()  # type: ignore[assignment]
            last_traded[instrument_id] = min(found, last_traded.get(instrument_id, found))
    skipped, jumps = _skipped(store, bars, adjustment, window, end)
    market = MarketData(
        days=tuple(window),
        bars=bars,
        features=features,
        index_closes=index_closes,
        members=members,
        industries=_industries(session, instruments, adjustment.lineage),
        series=series,
        last_traded=last_traded,
        breadth=breadth,
        skipped=skipped,
    )
    snapshot = {
        "store_first": window[0].isoformat(),
        "store_last": window[-1].isoformat(),
        "sessions": len(window),
        "bars": bars.height,
        "features": features.height,
        "index": index,
        "index_points": len(closes),
        **(
            {
                "breadth_points": len(breadth),
                "regime_index_points": len(index_closes[regime.INDEX_NAME]),
                "vix_points": len(index_closes[regime.VIX_NAME]),
            }
            if regime_inputs
            else {}
        ),
        "members": len(members),
        "last_traded": len(last_traded),
        "jump_skip": {
            "rule": f"no trade or mark within {JUMP_SKIP_SESSIONS} sessions of an open jump",
            "jumps": jumps,
            "instrument_days": len(skipped),
        },
        "actions_digest": expected,
        "content_digest": _content_digest(market),
    }
    return market, snapshot
