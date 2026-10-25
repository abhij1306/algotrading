"""Daily features on adjusted bars, computed with polars (HAT-DATA-070..078, HAT-DEC-T11).

    features_daily/date=YYYY-MM-DD/part-features.parquet   one row per instrument
    breadth_daily/date=YYYY-MM-DD/part-breadth.parquet     one row per day (HAT-DATA-076)

`compute_features` is a pure function of a bar frame. Every feature of date d is a function of the
instrument's own rows dated on or before d (a window over rows, so a day an instrument did not
trade is not a row), and the percentile columns of d rank only instruments that have a value on d.
`run_features` reads the adjusted `bars_daily` view and writes each partition atomically. The
percentiles and breadth rank the ordinary equity rows only (`ORDINARY_EQUITY`, D257).

Incremental equals full (HAT-DATA-077): both load the same way (the target dates plus, for every
instrument, the 252 rows before the first target), so a date has the same inputs either way.
`features_daily/_state.json` (`FeaturesState`) records the inputs and, per day, the fingerprint of
the bars the day was computed from, so a run recomputes exactly what moved (DB5):
- a day added, re-ingested or corrected, and the later days whose windows read it
  (`stale_days`);
- after a corporate-action or lineage change, the touched chains only (`affected_chains`): their
  rows are spliced into each day whose percentiles they leave alone, and the other days are
  recomputed whole;
- every day on `full`, on the first run and when `FEATURES_RULE` changed.

Full-history rebuild: about 4,150 dates x about 3,000 instruments. The rebuild runs in chunks of
`CHUNK_DAYS` target dates, so memory stays at about (CHUNK_DAYS + 252) x instruments rows.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import sys
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pyarrow as pa

from ..errors import HatError
from ..money import require_exact
from ..ops.jobs import JobResult
from .isin import ORDINARY_EQUITY
from .store import (
    BREADTH_SCHEMA,
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    FEATURE_NAMES,
    FEATURES_SCHEMA,
    NO_ADJUSTMENT,
    ActionFactor,
    Adjustment,
    DataStore,
    DividendCash,
    atomic_replace,
    latest_rows,
)

logger = logging.getLogger(__name__)

SMA_WINDOWS = (10, 20, 50, 150, 200)
WINDOW_52W = 252
WINDOW_20 = 20
RET_WINDOWS = (21, 63, 126, 252)
RS_WEIGHTS = ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2))  # 3/6/9/12 months of 21 days
MOM_WINDOWS = (21, 63, 126)
LOOKBACK_ROWS = 252  # rows before a date that any feature of the date can read
LOOKBACK_SLACK_DAYS = 40  # store days read beyond those rows before falling back to a deeper read
CHUNK_DAYS = 63
BATCH_INSTRUMENTS = 500  # instruments per per-instrument computation
DUCKDB_THREADS = 2
FEATURES_PART = "features"
BREADTH_PART = "breadth"
STATE_FILE = "_state.json"
STATE_VERSION = 2
# More changed chains than this in one night is a bulk reference change (an import): every date
# is recomputed instead, which bounds the memory of the two chain computations.
MAX_CHAINS = 100
# A rolling mean is a running sum, so its last bits depend on where the frame starts (about 1e-14
# relative; measured). A flat price series then sits a few ulps either side of its own mean, and
# `close > sma` would flip between an incremental run and a rebuild. A close counts as above its
# SMA only when it exceeds it by more than this relative margin: far below the smallest real
# difference (a price in paise over a 50-day mean is at least 2e-4 / price), far above the noise.
ABOVE_MARGIN = 1e-12
# The ranked universe (D257) is the rows whose own ISIN is an equity share (`isin.ORDINARY_EQUITY`):
# point in time, with no reference data (a symbol suffix is a convention; `hat_instrument` keeps
# no security type and lacks some short-lived entitlements).
# What else besides the adjustment changes every date: the universe rule and the formulas. A
# change here rebuilds every date (the state records it).
FEATURES_RULE = f"v3|universe:{ORDINARY_EQUITY}"

BAR_COLUMNS = (
    "instrument_id",
    "date",
    "isin",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "traded_value",
)

_COLUMNS_SQL = """instrument_id, date, isin,
       CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value,
       CAST(div_factor AS DOUBLE) AS div_factor"""
# The features in rupees: on the total-return series they carry the factor of the dividends after
# their date, which is divided out (`compute_features`), so a date's levels are the ones live saw
# that day and a later dividend never changes them (CA3).
LEVEL_FEATURES = ("sma10", "sma20", "sma50", "sma150", "sma200", "high_52w", "low_52w", "adtr20")
DIV_FACTOR = "div_factor"
DIVIDEND_LINE = "dividend:"  # a dividend's value in `adjustment_lines`
_LOADED = (*BAR_COLUMNS, DIV_FACTOR)  # what `load_bars` reads
# The latest ingest run's row per (instrument, date), as the `bars_daily` view gives, but read
# through `bars_daily_adj` with the date filter first: a window over the whole view would not be
# pruned by date and would hold the full history in memory.
_LATEST = (
    "QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1"
)
_RANGE_SQL = f"""
SELECT {_COLUMNS_SQL}, ingest_run_id FROM bars_daily_adj WHERE date BETWEEN ? AND ?
"""  # noqa: S608
_CHAINS_SQL = f"""
SELECT {_COLUMNS_SQL}, ingest_run_id FROM bars_daily_adj
WHERE instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""  # noqa: S608
_OLDER_SQL = f"""
SELECT {_COLUMNS_SQL} FROM (
    SELECT *, row_number() OVER (PARTITION BY instrument_id ORDER BY date DESC) AS rn FROM (
        SELECT * FROM bars_daily_adj
        WHERE date < ? AND instrument_id NOT IN (SELECT unnest(CAST(? AS VARCHAR[]))) {_LATEST}))
WHERE rn <= ?
"""  # noqa: S608


class FeaturesError(HatError):
    """A features rule was broken; `code` is stable and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mean(expr: pl.Expr, window: int) -> pl.Expr:
    """Mean over the last `window` rows of the instrument; null unless the window is full (a
    null input in the window also gives null)."""
    return expr.rolling_mean(window, min_samples=window).over("instrument_id")


def _ret(window: int) -> pl.Expr:
    """close_d / close_{d-window} - 1, null without that many rows (or a non-positive base)."""
    base = pl.col("close").shift(window).over("instrument_id")
    return pl.when(base > 0).then(pl.col("close") / base - 1.0)


def _percentile(value: pl.Expr) -> pl.Expr:
    """100 x average rank / count over the day's instruments that have a value (ties share the
    average rank); null where the value is null."""
    return 100.0 * value.rank("average").over("date") / value.count().over("date")


def _per_instrument(frame: pl.DataFrame) -> pl.DataFrame:
    """Every feature that depends on one instrument's own rows (all but the percentiles)."""
    col = pl.col
    ratio = pl.when(col("low") > 0).then(col("high") / col("low") - 1.0)
    prev = pl.when(col("prev_close") > 0).then(col("prev_close"))  # no usable prev close: ignored
    true_range = pl.max_horizontal(col("high"), prev) - pl.min_horizontal(col("low"), prev)
    traded = pl.coalesce(col("traded_value"), col("close") * col("volume"))
    # 0.4 r3 + 0.2 r6 + 0.2 r9 + 0.2 r12, added left to right as `compute_rs_map` does; a null
    # horizon makes the whole value null.
    weighted = pl.lit(0.0)
    for n, w in RS_WEIGHTS:
        weighted = weighted + w * _ret(n)
    return frame.with_columns(
        *(_mean(col("close"), n).alias(f"sma{n}") for n in SMA_WINDOWS),
        col("high")
        .rolling_max(WINDOW_52W, min_samples=WINDOW_52W)
        .over("instrument_id")
        .alias("high_52w"),
        col("low")
        .rolling_min(WINDOW_52W, min_samples=WINDOW_52W)
        .over("instrument_id")
        .alias("low_52w"),
        (100.0 * _mean(ratio, WINDOW_20)).alias("adr20"),
        _mean(true_range, WINDOW_20).alias("adtr20"),
        _mean(col("volume"), WINDOW_20).alias("adv20"),
        _mean(traded, WINDOW_20).alias("turnover20"),
        *(_ret(n).alias(f"ret{n}") for n in RET_WINDOWS),
        weighted.alias("rs_weighted"),
    ).drop("high", "low", "prev_close", "volume", "traded_value")


def _above() -> pl.Expr:
    """The close is above its SMA50 as breadth counts it (null without an SMA50)."""
    return pl.col("close") > pl.col("sma50") * (1.0 + ABOVE_MARGIN)


def _member() -> pl.Expr:
    """The row's ISIN is in the ranked universe (`isin.ordinary_equity`, as a polars expression)."""
    return pl.col("isin").str.contains(ORDINARY_EQUITY).fill_null(value=False)


def compute_features(
    bars: pl.DataFrame,
    *,
    from_date: date | None = None,
    batch: int | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(features, breadth) for the dates of `bars` (those on or after `from_date`, if given).

    `bars` has the `BAR_COLUMNS` (instrument_id and isin as text, date, doubles; `traded_value`
    null where unknown) and may have `div_factor` (the total-return series' factor of the
    dividends after the date): the `LEVEL_FEATURES` of a date are then divided by its own factor.
    The percentile columns and breadth rank and count the rows of the ranked universe
    (`isin.ordinary_equity` of the row's own ISIN, so a day's members are those of that day); other
    rows get null `rs_pct` and `mom_rank`. `features` has `date`, `instrument_id` and the 18
    feature columns; `breadth` has `date` and the `BREADTH_SCHEMA` columns. The first rows of an
    instrument have null windows, so the caller passes the lookback rows and `from_date`. `batch`
    computes the per-instrument columns that many instruments at a time to bound memory; it
    changes no value.
    """
    missing = set(BAR_COLUMNS) - set(bars.columns)
    if missing:
        raise FeaturesError("features_missing_columns")
    scaled = DIV_FACTOR in bars.columns
    frame = bars.select(_LOADED if scaled else BAR_COLUMNS).sort("instrument_id", "date")
    if frame.select(pl.struct("instrument_id", "date").is_duplicated().any()).item():
        raise FeaturesError("features_duplicate_bar")
    col = pl.col
    in_universe = _member()

    ids = frame["instrument_id"].unique(maintain_order=True).to_list()
    step = len(ids) or 1 if batch is None else max(1, batch)
    parts = []
    for start in range(0, len(ids), step):
        wanted = ids[start : start + step] if batch is not None else None
        part = frame if wanted is None else frame.filter(col("instrument_id").is_in(wanted))
        part = _per_instrument(part)
        parts.append(part if from_date is None else part.filter(col("date") >= from_date))
    wide = pl.concat(parts) if parts else _per_instrument(frame)
    # Returns of the universe members only are ranked; the weighted return is null unless all
    # four horizons exist, as in the pandas `compute_rs_map` (fewer than 253 bars: left out).
    ranked = wide.with_columns(
        _percentile(pl.when(in_universe).then(col("rs_weighted"))).alias("rs_pct"),
        *(
            _percentile(pl.when(in_universe).then(col(f"ret{n}"))).alias(f"_pct{n}")
            for n in MOM_WINDOWS
        ),
    ).with_columns(pl.max_horizontal(*(col(f"_pct{n}") for n in MOM_WINDOWS)).alias("mom_rank"))
    # each date's levels on that date's scale (`LEVEL_FEATURES`); breadth compares within a date
    leveled = (
        ranked.with_columns(col(n) / col(DIV_FACTOR) for n in LEVEL_FEATURES) if scaled else ranked
    )
    features = leveled.select("date", "instrument_id", *FEATURE_NAMES).sort("date", "instrument_id")

    members = ranked.filter(in_universe)
    breadth = (
        members.group_by("date")
        .agg(
            pl.len().cast(pl.Int64).alias("universe_size"),
            col("sma50").is_not_null().sum().cast(pl.Int64).alias("with_sma50"),
            _above().sum().cast(pl.Int64).alias("above_sma50"),
        )
        .with_columns(
            pl.when(col("with_sma50") > 0)
            .then(100.0 * col("above_sma50") / col("with_sma50"))
            .alias("breadth_pct")
        )
        .sort("date")
    )
    all_dates = ranked.select("date").unique()
    empty = all_dates.join(breadth, on="date", how="anti").with_columns(
        pl.lit(0, pl.Int64).alias("universe_size"),
        pl.lit(0, pl.Int64).alias("with_sma50"),
        pl.lit(0, pl.Int64).alias("above_sma50"),
        pl.lit(None, pl.Float64).alias("breadth_pct"),
    )
    return features, pl.concat([breadth, empty.select(breadth.columns)]).sort("date")


# --- the job ----------------------------------------------------------------------------------


@dataclass
class FeaturesReport:
    """What a run did. `full` is true when every date was recomputed, and `reason` says why.
    `dates` were recomputed whole; on `spliced` only the rows of the `chains` whose adjustment
    changed were rewritten (and the day's breadth counts moved by them)."""

    full: bool = False
    reason: str = "incremental"
    dates: list[date] = field(default_factory=list)
    rows: int = 0
    chains: list[str] = field(default_factory=list)
    spliced: list[date] = field(default_factory=list)


def adjustment_lines(adjustment: Adjustment) -> tuple[list[str], list[str]]:
    """The adjustment as sorted text: `instrument|ex_date|factor` per factor of the adjusted
    view and `instrument|ex_date|dividend:amount` per dividend (both keyed by chain id), and
    `old>chain` per linked ISIN. The state keeps them, so a later run knows which chains a change
    touches and can rebuild their old series. A dividend imported for past dates changes the
    returns before it; one that goes ex later changes no stored date (`LEVEL_FEATURES`)."""
    factors = sorted(
        [f"{a.instrument_id}|{a.ex_date.isoformat()}|{a.factor}" for a in adjustment.factors]
        + [
            f"{d.instrument_id}|{d.ex_date.isoformat()}|{DIVIDEND_LINE}{d.amount}"
            for d in adjustment.dividends
        ]
    )
    lineage = sorted(f"{old}>{new}" for old, new in adjustment.lineage.items())
    return factors, lineage


def inputs_digest(adjustment: Adjustment) -> str:
    """Order-independent sha256 of what changes old dates without changing their bars: the
    (instrument, ex-date, factor) set the adjusted view uses, its dividends, its ISIN lineage, and
    the ranked universe and formulas (`FEATURES_RULE`). No lineage or dividend leaves the digest
    of the factors alone."""
    factors, lineage = adjustment_lines(adjustment)
    lines = [*factors, *(f"lineage:{line}" for line in lineage)]
    return hashlib.sha256(("\n".join(lines) + "\nrule:" + FEATURES_RULE).encode()).hexdigest()


@dataclass
class FeaturesState:
    """`features_daily/_state.json`: the inputs the partitions were written for and, per day,
    the bars fingerprint (`DataStore.fingerprints`) the day's partitions were computed from. A
    day without a record, or whose bars now have another fingerprint, is stale. A run removes
    the records of the days it is about to rewrite before it writes any, and records each day
    once written, so a run that dies part-way (a `--full` included) leaves the rest unrecorded
    and the next run resumes there."""

    inputs_digest: str
    rule: str
    factors: list[str]
    lineage: list[str]
    days: dict[date, str] = field(default_factory=dict)


def _state_path(store: DataStore) -> Path:
    return store.root / DATASET_FEATURES / STATE_FILE


def read_state(store: DataStore) -> FeaturesState | None:
    """The recorded state; None when there is none or it is unreadable or of an older format
    (every date is then recomputed)."""
    try:
        raw = json.loads(_state_path(store).read_text(encoding="utf-8"))
        if raw.get("version") != STATE_VERSION:
            return None
        return FeaturesState(
            str(raw["inputs_digest"]),
            str(raw["rule"]),
            [str(line) for line in raw["factors"]],
            [str(line) for line in raw["lineage"]],
            {date.fromisoformat(day): str(fp) for day, fp in raw["days"].items()},
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_state(store: DataStore, state: FeaturesState) -> None:
    text = json.dumps(
        {
            "version": STATE_VERSION,
            "inputs_digest": state.inputs_digest,
            "rule": state.rule,
            "factors": state.factors,
            "lineage": state.lineage,
            "days": {day.isoformat(): fp for day, fp in sorted(state.days.items())},
        },
        sort_keys=True,
    )
    atomic_replace(_state_path(store), lambda tmp: tmp.write_text(text, encoding="utf-8"))


def features_current(
    store: DataStore, digest: str, days: Iterable[date], read: Mapping[date, str] | None = None
) -> bool:
    """True when every one of `days` has features written for the inputs `digest` from the bars
    the store holds now (the backtest's gate; the caller holds the features lock). A rebuild in
    progress, or bars corrected since, leaves some day unrecorded or with another fingerprint.
    `read` is the fingerprints a caller took before reading the bars itself: they must be the
    recorded ones and still the store's, so features never pair with bars read before a change."""
    state = read_state(store)
    if state is None or state.inputs_digest != digest:
        return False
    prints = store.fingerprints(DATASET_BARS)
    seen = prints if read is None else read
    return all(
        day in prints
        and state.days.get(day) == prints[day] == seen.get(day)
        and store.partition_files(DATASET_FEATURES, day)
        for day in days
    )


def stale_days(
    days: Sequence[date],
    dirty: Collection[date],
    presence: Callable[[date], Collection[str]],
    lost: Collection[date] = (),
) -> set[date]:
    """The days to recompute when the bars of `dirty` (days of `days`) changed or were never
    featured, or a recorded part file of `lost` disappeared.

    A feature of day x reads the instrument's own last `LOOKBACK_ROWS` rows before x, so a
    changed day d moves, besides d itself, every later day on which an instrument with a row
    on d has one of its next `LOOKBACK_ROWS` rows (counted per instrument: one that trades
    rarely reaches further than `LOOKBACK_ROWS` store days), and with it the percentiles and
    breadth of those days; the `LOOKBACK_ROWS` store days after d are stale in any case. A lost
    day (removed, or one of its recorded files removed or rewritten) no longer lists the
    instruments it had, so every day from it on is stale. `presence(day)` is the ids with a bar
    on a day of `days`, as the adjusted view names them."""
    stale: set[date] = set()
    if lost:
        first_lost = min(lost)
        stale.update(d for d in days if d >= first_lost)
    if not dirty:
        return stale
    remaining: dict[str, int] = {}  # rows still to come that read a dirty day, per instrument
    floor = -1  # the last index stale by the store-day rule
    last_dirty = max(dirty)
    for i in range(bisect_left(days, min(dirty)), len(days)):
        day = days[i]
        if day in dirty:
            stale.add(day)
            floor = max(floor, i + LOOKBACK_ROWS)
            remaining.update(dict.fromkeys(presence(day), LOOKBACK_ROWS))
            continue
        if not remaining and i > floor:
            if day > last_dirty:
                break
            continue
        hit = [x for x in presence(day) if x in remaining] if remaining else []
        if hit or i <= floor:
            stale.add(day)
        for x in hit:
            remaining[x] -= 1
            if not remaining[x]:
                del remaining[x]
    return stale


def _parts(fingerprint: str) -> set[str]:
    """The `name:size` parts of a `DataStore.fingerprints` entry."""
    return {part for part in fingerprint.split(";") if part}


def affected_chains(
    old: tuple[Sequence[str], Sequence[str]], new: tuple[Sequence[str], Sequence[str]]
) -> set[str]:
    """The ids whose adjusted series may differ between two adjustments given as
    `adjustment_lines`: a factor line belongs to its chain, a lineage line `old>chain` to both
    ids (the old ISIN's bars leave its own series and join the chain's, or the reverse)."""

    def groups(lines: tuple[Sequence[str], Sequence[str]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for line in lines[0]:
            out[line.split("|", 1)[0]].add(line)
        for line in lines[1]:
            member, chain = line.split(">", 1)
            out[member].add(line)
            out[chain].add(line)
        return out

    before, after = groups(old), groups(new)
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def adjustment_of(
    factors: Sequence[str], lineage: Sequence[str], chains: Collection[str]
) -> Adjustment:
    """The part of an adjustment (as `adjustment_lines`) that the series of `chains` read: their
    factors and every link into or out of them. A link out of a chain to an untouched one keeps
    that ISIN's bars under the other id, as in the whole adjustment."""
    picked, paid = [], []
    for line in factors:
        instrument, ex_date, value = line.split("|")
        if instrument not in chains:
            continue
        day = date.fromisoformat(ex_date)
        if value.startswith(DIVIDEND_LINE):
            paid.append(
                DividendCash(
                    instrument, day, require_exact(value.removeprefix(DIVIDEND_LINE), "dividend")
                )
            )
        else:
            picked.append(ActionFactor(instrument, day, Decimal(value)))
    links = {}
    for line in lineage:
        member, chain = line.split(">", 1)
        if member in chains or chain in chains:
            links[member] = chain
    return Adjustment(tuple(picked), links, tuple(paid))


def load_bars(
    con: duckdb.DuckDBPyConnection, first: date, last: date, days: Sequence[date] = ()
) -> pl.DataFrame:
    """The adjusted bars of `first..last` (with their `div_factor`) plus, per instrument, the
    `LOOKBACK_ROWS` rows before `first`: exactly what every feature of those dates reads.

    `days` is the sorted list of every store date. It bounds the first read to the
    `LOOKBACK_ROWS + LOOKBACK_SLACK_DAYS` days before `first`, which is enough unless an
    instrument stopped trading for longer than the slack; for the instruments that fall short a
    second read goes further back, so the result is the same as an unbounded read at a fraction
    of the memory (without `days` the read is unbounded).
    """
    start = first
    if days:
        position = bisect_left(days, first)
        start = days[max(0, position - LOOKBACK_ROWS - LOOKBACK_SLACK_DAYS)]
    # the latest run's row per (instrument, date), as in `bars_daily`
    frame = latest_rows(con.execute(_RANGE_SQL, [start, last]).pl()).select(_LOADED)
    before = frame.filter(pl.col("date") < first)
    complete = (
        before.group_by("instrument_id")
        .len()
        .filter(pl.col("len") >= LOOKBACK_ROWS)["instrument_id"]
        .to_list()
    )
    if not days or start > days[0]:  # the slice does not reach the start of the store
        # every instrument without a full lookback in the slice, including one with no row in it
        older = con.execute(_OLDER_SQL, [start, complete, LOOKBACK_ROWS]).pl()
        if older.height:
            frame = pl.concat([frame, older.select(_LOADED)])
    ahead = frame.filter(pl.col("date") >= first)
    behind = (
        frame.filter(pl.col("date") < first)
        .sort("date")
        .group_by("instrument_id", maintain_order=True)
        .tail(LOOKBACK_ROWS)
    )
    return pl.concat([behind, ahead]).select(_LOADED)


def write_day(store: DataStore, day: date, features: pl.DataFrame, breadth: pl.DataFrame) -> None:
    """One partition of each dataset for `day`; the same inputs rewrite the same bytes."""
    feature_table = features.drop("date").to_arrow().cast(FEATURES_SCHEMA)
    breadth_table = breadth.drop("date").to_arrow().cast(BREADTH_SCHEMA)
    store.write_partition(DATASET_FEATURES, day, FEATURES_PART, feature_table)
    store.write_partition(DATASET_BREADTH, day, BREADTH_PART, breadth_table)


# --- an adjustment change: only the touched chains --------------------------------------------

# What rs_pct and mom_rank rank, and the stored percentile that is non-null exactly for the
# members with a value of it (so the stored rows give each column's ranked multiset).
RANK_INPUTS = ("ret21", "ret63", "ret126", "rs_weighted")
RANKED_BY = {
    "ret21": "mom_rank",
    "ret63": "mom_rank",
    "ret126": "mom_rank",
    "rs_weighted": "rs_pct",
}
OWN_FEATURES = tuple(n for n in FEATURE_NAMES if n not in ("rs_pct", "mom_rank"))
# Two computations of one value from inputs scaled by a factor differ in the last bits; a change
# this small is float noise, not a new value (a split moves a price by percents).
SAME_TOLERANCE = 1e-12


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= SAME_TOLERANCE * max(1.0, abs(a), abs(b))


@dataclass(frozen=True)
class DaySplice:
    """The touched chains' new feature rows of one day and how the day's breadth counts move."""

    rows: list[dict[str, Any]]
    universe_size: int
    with_sma50: int
    above_sma50: int


def splice_day(
    old: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
    ranks: Mapping[str, tuple[float | None, float | None]],
    others: Mapping[str, Collection[float]],
) -> DaySplice | None:
    """The new rows of the touched chains on one day and the breadth deltas, when the change
    leaves every percentile of the day as it was; None when the day must be recomputed whole.

    `old` and `new` are the chains' computed rows of the day under the previous and the current
    adjustment (`instrument_id`, `_member`, `_with`, `_above` and `OWN_FEATURES`); `ranks` is the
    stored (rs_pct, mom_rank) of each old row; `others` is, per `RANK_INPUTS` column, the stored
    values the day ranks of every other member. A percentile depends only on the value's place
    among the day's member values, so the splice needs the chains' member values to pair up old
    to new (sorted) within float noise and no value to change place: no other member's value,
    nor another chain row's old or new value, lies between a row's old and new value (ends
    included, so a tie made or broken also recomputes). Then every other row keeps its
    percentiles and each new row takes those of its old row. A split with a later ex-date
    scales a whole history, which leaves every return as it was up to the last bits."""

    def key(row: Mapping[str, Any]) -> tuple[tuple[bool, float], ...]:
        return tuple((row[c] is None, row[c] or 0.0) for c in RANK_INPUTS)

    def ranked(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        valued = (r for r in rows if r["_member"] and any(r[c] is not None for c in RANK_INPUTS))
        return sorted(valued, key=key)

    before, after = ranked(old), ranked(new)
    if len(before) != len(after) or any(r["instrument_id"] not in ranks for r in before):
        return None
    pairs = list(zip(before, after, strict=True))
    if not all(_same(a[c], b[c]) for a, b in pairs for c in RANK_INPUTS):
        return None
    for c in RANK_INPUTS:
        for i, (a, b) in enumerate(pairs):
            if a[c] == b[c]:
                continue  # unchanged: no other value moved past it
            low, high = min(a[c], b[c]), max(a[c], b[c])
            chain = [r[c] for j, pair in enumerate(pairs) if j != i for r in pair]
            if any(v is not None and low <= v <= high for v in (*others[c], *chain)):
                return None
    taken = {b["instrument_id"]: ranks[a["instrument_id"]] for a, b in pairs}
    rows = []
    for r in sorted(new, key=lambda r: r["instrument_id"]):
        rs_pct, mom_rank = taken.get(r["instrument_id"], (None, None))
        own = {n: r[n] for n in OWN_FEATURES}
        rows.append(
            {"instrument_id": r["instrument_id"], **own, "rs_pct": rs_pct, "mom_rank": mom_rank}
        )

    def count(rows: Sequence[Mapping[str, Any]], column: str | None) -> int:
        return sum(1 for r in rows if r["_member"] and (column is None or r[column]))

    return DaySplice(
        rows,
        count(new, None) - count(old, None),
        count(new, "_with") - count(old, "_with"),
        count(new, "_above") - count(old, "_above"),
    )


def _changed(old: Sequence[Mapping[str, Any]], new: Sequence[Mapping[str, Any]]) -> bool:
    """The chains' rows of a day differ between two adjustments (beyond float noise)."""
    before = {r["instrument_id"]: r for r in old}
    after = {r["instrument_id"]: r for r in new}
    if before.keys() != after.keys():
        return True
    return any(
        before[i]["_member"] != after[i]["_member"]
        or before[i]["_above"] != after[i]["_above"]
        or not all(_same(before[i][c], after[i][c]) for c in OWN_FEATURES)
        for i in before
    )


def chain_features(
    con: duckdb.DuckDBPyConnection, chains: Collection[str], days: Sequence[date], chunk_days: int
) -> dict[date, pl.DataFrame]:
    """The per-instrument features of `chains` on every day, by day, computed on the chunks a
    full rebuild uses (`chunk_days` store days from the first), so each value has the inputs it
    has there: the chunk's rows and the `LOOKBACK_ROWS` before. `_member` and `_above` say
    whether the row is ranked and above its SMA50 as `compute_features` counts breadth; the
    `LEVEL_FEATURES` are on each date's own scale, as there."""
    bars = latest_rows(con.execute(_CHAINS_SQL, [sorted(chains)]).pl()).select(_LOADED)
    bars = bars.sort("instrument_id", "date")
    col = pl.col
    parts = []
    for start in range(0, len(days), chunk_days):
        first, last = days[start], days[min(start + chunk_days, len(days)) - 1]
        ahead = bars.filter(col("date").is_between(first, last))
        if not ahead.height:
            continue
        behind = (
            bars.filter(col("date") < first)
            .group_by("instrument_id", maintain_order=True)
            .tail(LOOKBACK_ROWS)
        )
        frame = pl.concat([behind, ahead]).sort("instrument_id", "date")
        parts.append(_per_instrument(frame).filter(col("date") >= first))
    if not parts:
        return {}
    frame = pl.concat(parts).with_columns(
        _member().alias("_member"),
        col("sma50").is_not_null().alias("_with"),
        _above().fill_null(value=False).alias("_above"),
    )
    # each date's levels on that date's scale, as `compute_features` writes them
    frame = frame.with_columns(col(n) / col(DIV_FACTOR) for n in LEVEL_FEATURES)
    frame = frame.select("date", "instrument_id", "_member", "_with", "_above", *OWN_FEATURES)
    return {
        key[0]: part.drop("date") for key, part in frame.partition_by("date", as_dict=True).items()
    }


def _rows(frames: Mapping[date, pl.DataFrame], day: date) -> list[dict[str, Any]]:
    frame = frames.get(day)
    return [] if frame is None else frame.to_dicts()


def _splice(
    store: DataStore,
    day: date,
    chains: Collection[str],
    old: Sequence[Mapping[str, Any]],
    new: Sequence[Mapping[str, Any]],
) -> int | None:
    """Rewrite the touched chains' rows of `day` (rows written), or None when the day needs a
    whole recompute: the stored rows of the chains are not the ones the previous adjustment
    gives, or the percentiles move."""
    stored = store.read_partition(DATASET_FEATURES, day)
    breadth = store.read_partition(DATASET_BREADTH, day)
    mine = stored.filter(pl.col("instrument_id").is_in(list(chains)))
    if breadth.height != 1 or set(mine["instrument_id"].to_list()) != {
        r["instrument_id"] for r in old
    }:
        return None
    ranks = {
        r[0]: (r[1], r[2]) for r in mine.select("instrument_id", "rs_pct", "mom_rank").iter_rows()
    }
    kept = stored.filter(~pl.col("instrument_id").is_in(list(chains)))
    others = {
        c: kept.filter(pl.col(by).is_not_null())[c].drop_nulls().to_list()
        for c, by in RANKED_BY.items()
    }
    splice = splice_day(old, new, ranks, others)
    if splice is None:
        return None
    schema = pl.from_arrow(FEATURES_SCHEMA.empty_table()).schema  # type: ignore[union-attr]
    rows = pl.DataFrame(splice.rows, schema=schema)
    table = pl.concat([kept, rows]).sort("instrument_id").to_arrow().cast(FEATURES_SCHEMA)
    counts = breadth.row(0, named=True)
    size = counts["universe_size"] + splice.universe_size
    with_sma50 = counts["with_sma50"] + splice.with_sma50
    above = counts["above_sma50"] + splice.above_sma50
    pct = 100.0 * above / with_sma50 if with_sma50 > 0 else None
    breadth_table = pa.table(
        {
            "universe_size": [size],
            "with_sma50": [with_sma50],
            "above_sma50": [above],
            "breadth_pct": [pct],
        },
        schema=BREADTH_SCHEMA,
    )
    store.write_partition(DATASET_FEATURES, day, FEATURES_PART, table)
    store.write_partition(DATASET_BREADTH, day, BREADTH_PART, breadth_table)
    return len(splice.rows)


# --- the run ------------------------------------------------------------------------------------


def run_features(
    store: DataStore,
    adjustment: Adjustment = NO_ADJUSTMENT,
    *,
    through: date | None = None,
    full: bool = False,
    chunk_days: int = CHUNK_DAYS,
) -> FeaturesReport:
    """Bring the features of every date up to `through` in line with the bars and `adjustment`:
    the stale days (`stale_days`: bars changed or never featured) are recomputed whole; after an
    adjustment change only the touched chains are recomputed (`chain_features`) and spliced into
    each day whose percentiles they leave alone, the other days recomputed whole. Every date is
    recomputed with `full`, on the first run, when the rule changed (`FEATURES_RULE`) or when
    more than `MAX_CHAINS` chains changed. Safe to repeat and to resume after a crash
    (`FeaturesState`)."""
    if chunk_days < 1:
        raise FeaturesError("features_bad_chunk")
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / ".features.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one run at a time: runs must not interleave writes
        return _run_locked(store, adjustment, through, full, chunk_days)


def _run_locked(
    store: DataStore,
    adjustment: Adjustment,
    through: date | None,
    full: bool,
    chunk_days: int,
) -> FeaturesReport:
    lines = adjustment_lines(adjustment)
    digest = inputs_digest(adjustment)
    prints = store.fingerprints(DATASET_BARS)
    all_days = sorted(prints)
    days = [d for d in all_days if through is None or d <= through]
    report = FeaturesReport()
    state = read_state(store)
    chains: set[str] = set()
    if full:
        report.full, report.reason = True, "full_requested"
    elif state is None:
        report.full, report.reason = True, "first_run"
    elif state.rule != FEATURES_RULE:
        report.full, report.reason = True, "inputs_changed"
    elif (state.factors, state.lineage) != lines:
        chains = affected_chains((state.factors, state.lineage), lines)
        if len(chains) > MAX_CHAINS:
            report.full, report.reason = True, "inputs_changed"
    if report.full or state is None:
        state = FeaturesState(digest, FEATURES_RULE, *lines)
        _write_state(store, state)
        stale = set(days)
    else:
        stale = _update(store, state, report, chains, lines, digest, prints, chunk_days)
    targets = [d for d in days if d in stale]
    if targets:
        _recompute(store, adjustment, state, report, targets, days, prints, chunk_days)
    return report


def _update(
    store: DataStore,
    state: FeaturesState,
    report: FeaturesReport,
    chains: set[str],
    lines: tuple[list[str], list[str]],
    digest: str,
    prints: Mapping[date, str],
    chunk_days: int,
) -> set[date]:
    """The incremental path: the stale days (returned, to be recomputed whole) and, after an
    adjustment change, the touched chains spliced into the days that allow it."""
    all_days = sorted(prints)
    written = set(store.days(DATASET_FEATURES)) & set(store.days(DATASET_BREADTH))
    dirty = {d for d in all_days if state.days.get(d) != prints[d] or d not in written}
    # A day keeps its instruments only when every recorded file is still there unchanged (a new
    # run beside them adds rows); a file removed or rewritten took rows nobody lists any more.
    lost = {d for d, fp in state.days.items() if not _parts(fp) <= _parts(prints.get(d, ""))}
    # an id as either adjustment names it, so a lineage change on the same night is covered
    old_links = dict(line.split(">", 1) for line in state.lineage)
    new_links = dict(line.split(">", 1) for line in lines[1])

    def presence(day: date) -> set[str]:
        raw = store.bar_instruments(day)
        return {old_links.get(i, i) for i in raw} | {new_links.get(i, i) for i in raw}

    stale = stale_days(all_days, dirty, presence, lost)
    old: dict[date, pl.DataFrame] = {}
    new: dict[date, pl.DataFrame] = {}
    candidates: list[date] = []
    if chains:
        report.reason, report.chains = "adjustment_changed", sorted(chains)
        for frames, (factors, lineage) in ((old, (state.factors, state.lineage)), (new, lines)):
            with store.connect(adjustment_of(factors, lineage, chains)) as con:
                con.execute(f"SET threads={DUCKDB_THREADS}")
                frames.update(chain_features(con, chains, all_days, chunk_days))
        candidates = sorted(
            d
            for d in old.keys() | new.keys()
            if d in state.days and d not in stale and _changed(_rows(old, d), _rows(new, d))
        )
    # forget every day about to be rewritten before writing any, then adopt the new inputs
    drop = stale | set(candidates)
    state.days = {d: fp for d, fp in state.days.items() if d in prints and d not in drop}
    state.inputs_digest, (state.factors, state.lineage) = digest, lines
    _write_state(store, state)
    for i, day in enumerate(candidates, 1):
        written_rows = _splice(store, day, chains, _rows(old, day), _rows(new, day))
        if written_rows is None:
            stale.add(day)
        else:
            state.days[day] = prints[day]
            report.spliced.append(day)
            report.rows += written_rows
        if i % chunk_days == 0 or i == len(candidates):
            _write_state(store, state)
    return stale


def chunks(targets: Sequence[date], days: Sequence[date], chunk_days: int) -> list[list[date]]:
    """`targets` (sorted, a subset of `days`) in runs that each span fewer than `chunk_days`
    store days, so a chunk reads at most that many days besides its lookback however scattered
    the targets are."""
    position = {d: i for i, d in enumerate(days)}
    out: list[list[date]] = []
    for day in targets:
        if out and position[day] - position[out[-1][0]] < chunk_days:
            out[-1].append(day)
        else:
            out.append([day])
    return out


def _recompute(
    store: DataStore,
    adjustment: Adjustment,
    state: FeaturesState,
    report: FeaturesReport,
    targets: list[date],
    days: list[date],
    prints: Mapping[date, str],
    chunk_days: int,
) -> None:
    """Compute and write `targets` whole, a chunk at a time, recording each chunk once written."""
    con = store.connect(adjustment)
    con.execute(f"SET threads={DUCKDB_THREADS}")
    try:
        for chunk in chunks(targets, days, chunk_days):
            bars = load_bars(con, chunk[0], chunk[-1], days)
            features, breadth = compute_features(bars, from_date=chunk[0], batch=BATCH_INSTRUMENTS)
            per_day = features.partition_by("date", as_dict=True)
            per_day_breadth = breadth.partition_by("date", as_dict=True)
            for day in chunk:
                f = per_day[(day,)]
                write_day(store, day, f, per_day_breadth[(day,)])
                report.rows += f.height
            report.dates.extend(chunk)
            state.days.update({day: prints[day] for day in chunk})
            _write_state(store, state)
    finally:
        con.close()


def features_job(
    store: DataStore,
    adjustment: Callable[[], Adjustment],
    *,
    through: date | None = None,
    full: bool = False,
) -> Callable[[], JobResult]:
    """The `features` body for `run_job`. Idempotent: a re-run after a crash between the body's
    return and the success record finds every day recorded and writes nothing new."""

    def body() -> JobResult:
        report = run_features(store, adjustment(), through=through, full=full)
        logger.info(
            "features: %d dates recomputed, %d spliced for %d chains, %d rows (%s)",
            len(report.dates),
            len(report.spliced),
            len(report.chains),
            report.rows,
            report.reason,
        )
        return JobResult(rows=report.rows)

    return body


# --- command line ------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.features [--full] [--through YYYY-MM-DD] [--chunk-days N]

    Brings the features in line with the bars under `HAT_DATA_ROOT`, the stored corporate
    actions and the ISIN lineage, as the scheduled `features` job does (`run_features`: what
    changed and what reads it); `--full` recomputes every date.
    """
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.features")
    parser.add_argument("--full", action="store_true", help="recompute every date")
    parser.add_argument("--through", type=date.fromisoformat, default=None)
    parser.add_argument("--chunk-days", type=int, default=CHUNK_DAYS)
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from .chains import stored_adjustment

    logging.basicConfig(level=logging.INFO)
    with HatSessionLocal() as session:
        adjustment = stored_adjustment(session)
    report = run_features(
        DataStore(settings.data_root, memory_limit=settings.duckdb_memory),
        adjustment,
        through=args.through,
        full=args.full,
        chunk_days=args.chunk_days,
    )
    sys.stdout.write(
        f"features: {len(report.dates)} dates recomputed, {len(report.spliced)} spliced for"
        f" {len(report.chains)} chains, {report.rows} rows ({report.reason})\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
