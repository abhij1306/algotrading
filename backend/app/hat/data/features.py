"""Daily features on adjusted bars, computed with polars (HAT-DATA-070..078, HAT-DEC-T11).

    features_daily/date=YYYY-MM-DD/part-features.parquet   one row per instrument
    breadth_daily/date=YYYY-MM-DD/part-breadth.parquet     one row per day (HAT-DATA-076)

`compute_features` is a pure function of a bar frame. Every feature of date d is a function of the
instrument's own rows dated on or before d (a window over rows, so a day an instrument did not
trade is not a row), and the percentile columns of d rank only instruments that have a value on d.
`run_features` reads the adjusted `bars_daily` view, computes the dates that still lack a
partition (or every date, with `full=True`) and writes each partition atomically.

Incremental equals full (HAT-DATA-077): both load the same way (the target dates plus, for every
instrument, the 252 rows before the first target), so a date has the same inputs either way. Two
things change old dates and so force a full rebuild even without the flag: a change in the
corporate actions or the universe (adjusted history and the percentiles move; a digest of both
is kept in `features_daily/_state.json`) and a correction of already-featured bars (the caller
passes `full=True`; the ingest does so).

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
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from ..errors import HatError
from ..ops.jobs import JobResult
from .store import (
    BREADTH_SCHEMA,
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    FEATURE_NAMES,
    FEATURES_SCHEMA,
    ActionFactor,
    DataStore,
    atomic_replace,
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
# A rolling mean is a running sum, so its last bits depend on where the frame starts (about 1e-14
# relative; measured). A flat price series then sits a few ulps either side of its own mean, and
# `close > sma` would flip between an incremental run and a rebuild. A close counts as above its
# SMA only when it exceeds it by more than this relative margin: far below the smallest real
# difference (a price in paise over a 50-day mean is at least 2e-4 / price), far above the noise.
ABOVE_MARGIN = 1e-12

BAR_COLUMNS = (
    "instrument_id",
    "date",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "traded_value",
)

_COLUMNS_SQL = """instrument_id, date, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value"""
# The latest ingest run's row per (instrument, date), as the `bars_daily` view gives, but read
# through `bars_daily_adj` with the date filter first: a window over the whole view would not be
# pruned by date and would hold the full history in memory.
_LATEST = (
    "QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1"
)
_RANGE_SQL = f"""
SELECT {_COLUMNS_SQL}, ingest_run_id FROM bars_daily_adj WHERE date BETWEEN ? AND ?
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
        weighted.alias("_weighted"),
    ).drop("high", "low", "prev_close", "volume", "traded_value")


def compute_features(
    bars: pl.DataFrame,
    universe: Collection[str] | None = None,
    *,
    from_date: date | None = None,
    batch: int | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(features, breadth) for the dates of `bars` (those on or after `from_date`, if given).

    `bars` has the `BAR_COLUMNS` (instrument_id as text, date, doubles; `traded_value` null where
    unknown). `universe` is the instruments the percentile columns and breadth rank and count
    (default: every instrument with a bar); other instruments get null `rs_pct` and `mom_rank`.
    `features` has `date`, `instrument_id` and the 17 feature columns; `breadth` has `date`
    and the `BREADTH_SCHEMA` columns. The first rows of an instrument have null windows, so the
    caller passes the lookback rows and `from_date`. `batch` computes the per-instrument columns
    that many instruments at a time to bound memory; it changes no value.
    """
    missing = set(BAR_COLUMNS) - set(bars.columns)
    if missing:
        raise FeaturesError("features_missing_columns")
    frame = bars.select(BAR_COLUMNS).sort("instrument_id", "date")
    if frame.select(pl.struct("instrument_id", "date").is_duplicated().any()).item():
        raise FeaturesError("features_duplicate_bar")
    col = pl.col
    in_universe = pl.lit(True) if universe is None else col("instrument_id").is_in(list(universe))

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
        _percentile(pl.when(in_universe).then(col("_weighted"))).alias("rs_pct"),
        *(
            _percentile(pl.when(in_universe).then(col(f"ret{n}"))).alias(f"_pct{n}")
            for n in MOM_WINDOWS
        ),
    ).with_columns(pl.max_horizontal(*(col(f"_pct{n}") for n in MOM_WINDOWS)).alias("mom_rank"))
    features = ranked.select("date", "instrument_id", *FEATURE_NAMES).sort("date", "instrument_id")

    members = ranked.filter(in_universe)
    breadth = (
        members.group_by("date")
        .agg(
            pl.len().cast(pl.Int64).alias("universe_size"),
            col("sma50").is_not_null().sum().cast(pl.Int64).alias("with_sma50"),
            (col("close") > col("sma50") * (1.0 + ABOVE_MARGIN))
            .sum()
            .cast(pl.Int64)
            .alias("above_sma50"),
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
    """What a run did. `full` is true when every date was recomputed, and `reason` says why."""

    full: bool = False
    reason: str = "incremental"
    dates: list[date] = field(default_factory=list)
    rows: int = 0


def inputs_digest(actions: Iterable[ActionFactor], universe: Collection[str] | None) -> str:
    """Order-independent sha256 of what changes old dates without changing their bars: the
    (instrument, ex-date, factor) set the adjusted view uses and the universe the percentiles
    and breadth rank over."""
    lines = sorted(f"{a.instrument_id}|{a.ex_date.isoformat()}|{a.factor}" for a in actions)
    scope = "*" if universe is None else ",".join(sorted(universe))
    return hashlib.sha256(("\n".join(lines) + "\nuniverse:" + scope).encode()).hexdigest()


def _state_path(store: DataStore) -> Path:
    return store.root / DATASET_FEATURES / STATE_FILE


def _read_digest(store: DataStore) -> str | None:
    try:
        return json.loads(_state_path(store).read_text(encoding="utf-8")).get("inputs_digest")
    except (OSError, ValueError):
        return None


def _write_digest(store: DataStore, digest: str) -> None:
    text = json.dumps({"inputs_digest": digest}, sort_keys=True)
    atomic_replace(_state_path(store), lambda tmp: tmp.write_text(text, encoding="utf-8"))


def load_bars(
    con: duckdb.DuckDBPyConnection, first: date, last: date, days: Sequence[date] = ()
) -> pl.DataFrame:
    """The adjusted bars of `first..last` plus, per instrument, the `LOOKBACK_ROWS` rows before
    `first`: exactly what every feature of those dates reads.

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
    frame = con.execute(_RANGE_SQL, [start, last]).pl()
    # The latest run's row per (instrument, date), as in `bars_daily`. Done here, not in SQL (a
    # window over the slice costs several times the memory of the slice), and sorted only when a
    # day was written by more than one run, which is rare.
    if frame.select(pl.struct("instrument_id", "date").is_duplicated().any()).item():
        frame = frame.sort("ingest_run_id").unique(["instrument_id", "date"], keep="last")
    frame = frame.select(BAR_COLUMNS)
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
            frame = pl.concat([frame, older.select(BAR_COLUMNS)])
    ahead = frame.filter(pl.col("date") >= first)
    behind = (
        frame.filter(pl.col("date") < first)
        .sort("date")
        .group_by("instrument_id", maintain_order=True)
        .tail(LOOKBACK_ROWS)
    )
    return pl.concat([behind, ahead]).select(BAR_COLUMNS)


def write_day(store: DataStore, day: date, features: pl.DataFrame, breadth: pl.DataFrame) -> None:
    """One partition of each dataset for `day`; the same inputs rewrite the same bytes."""
    feature_table = features.drop("date").to_arrow().cast(FEATURES_SCHEMA)
    breadth_table = breadth.drop("date").to_arrow().cast(BREADTH_SCHEMA)
    store.write_partition(DATASET_FEATURES, day, FEATURES_PART, feature_table)
    store.write_partition(DATASET_BREADTH, day, BREADTH_PART, breadth_table)


def _has_partitions(store: DataStore, day: date) -> bool:
    return bool(store.partition_files(DATASET_FEATURES, day)) and bool(
        store.partition_files(DATASET_BREADTH, day)
    )


def run_features(
    store: DataStore,
    actions: Iterable[ActionFactor] = (),
    *,
    through: date | None = None,
    full: bool = False,
    universe: Collection[str] | None = None,
    chunk_days: int = CHUNK_DAYS,
) -> FeaturesReport:
    """Compute and write the features of every date up to `through` that lacks a partition (all
    dates when `full` or when the corporate actions or the universe changed since the last run).
    Safe to repeat and to resume after a crash: a partition is replaced whole, and the inputs
    digest is recorded only after the last date is written."""
    if chunk_days < 1:
        raise FeaturesError("features_bad_chunk")
    actions = list(actions)
    store.root.mkdir(parents=True, exist_ok=True)
    with (store.root / ".features.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one run at a time: runs must not interleave writes
        return _run_locked(store, actions, through, full, universe, chunk_days)


def _run_locked(
    store: DataStore,
    actions: list[ActionFactor],
    through: date | None,
    full: bool,
    universe: Collection[str] | None,
    chunk_days: int,
) -> FeaturesReport:
    digest = inputs_digest(actions, universe)
    all_days = store.days(DATASET_BARS)
    days = [d for d in all_days if through is None or d <= through]
    report = FeaturesReport()
    if full:
        report.full, report.reason = True, "full_requested"
    elif (previous := _read_digest(store)) != digest:
        report.full, report.reason = True, "inputs_changed" if previous else "first_run"
    targets = days if report.full else [d for d in days if not _has_partitions(store, d)]
    if targets:
        con = store.connect(actions)
        con.execute(f"SET threads={DUCKDB_THREADS}")
        try:
            for start in range(0, len(targets), chunk_days):
                chunk = targets[start : start + chunk_days]
                bars = load_bars(con, chunk[0], chunk[-1], days)
                features, breadth = compute_features(
                    bars, universe, from_date=chunk[0], batch=BATCH_INSTRUMENTS
                )
                per_day = features.partition_by("date", as_dict=True)
                per_day_breadth = breadth.partition_by("date", as_dict=True)
                for day in chunk:
                    f = per_day[(day,)]
                    write_day(store, day, f, per_day_breadth[(day,)])
                    report.rows += f.height
                report.dates.extend(chunk)
        finally:
            con.close()
    # A rebuild that stopped at `through` leaves later dates on the old inputs, so the new
    # digest is recorded only when every date was covered; the next run then finishes the job.
    if not (report.full and len(days) < len(all_days)):
        _write_digest(store, digest)
    return report


def features_job(
    store: DataStore,
    actions: Callable[[], Iterable[ActionFactor]],
    *,
    through: date | None = None,
    full: bool = False,
    universe: Collection[str] | None = None,
) -> Callable[[], JobResult]:
    """The `features` body for `run_job`. Idempotent: a re-run after a crash between the body's
    return and the success record finds every partition present and writes nothing new."""

    def body() -> JobResult:
        report = run_features(store, actions(), through=through, full=full, universe=universe)
        return JobResult(rows=report.rows)

    return body


# --- command line ------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.features [--full] [--through YYYY-MM-DD] [--chunk-days N]

    Computes the features of every date that lacks them (every date with --full) from the bars
    under `HAT_DATA_ROOT` and the stored corporate actions. The scheduled `features` job calls
    `features_job` instead; this is for the first full-history build and for a rebuild after
    bars were corrected.
    """
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.features")
    parser.add_argument("--full", action="store_true", help="recompute every date")
    parser.add_argument("--through", type=date.fromisoformat, default=None)
    parser.add_argument("--chunk-days", type=int, default=CHUNK_DAYS)
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import SessionLocal

    from .corporate_actions import stored_factors

    logging.basicConfig(level=logging.INFO)
    with SessionLocal() as session:
        actions = stored_factors(session)
    report = run_features(
        DataStore(settings.data_root, memory_limit=settings.duckdb_memory),
        actions,
        through=args.through,
        full=args.full,
        chunk_days=args.chunk_days,
    )
    sys.stdout.write(f"features: {len(report.dates)} dates, {report.rows} rows ({report.reason})\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
