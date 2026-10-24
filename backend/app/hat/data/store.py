"""Parquet datasets under `HAT_DATA_ROOT` and the DuckDB views over them (HAT-DATA-080..085,
HAT-DEC-T6, HAT-DEC-B27).

Layout: `bars_daily_raw/date=YYYY-MM-DD/part-<ingest_run_id>.parquet`,
`index_daily/date=YYYY-MM-DD/part.parquet` and `raw/<file>` (bhavcopy zips, immutable). A
partition file is written to a temporary name and renamed over the final name, so a reader sees
the whole old file or the whole new one (HAT-DATA-082). Raw prices are stored as decimal(18,4)
and never rewritten; adjusted prices exist only as the `bars_daily_adj` view, which multiplies
them by cumulative factors computed from corporate actions (HAT-DATA-053, 054).

Views, per connection from `DataStore.connect`:
- `bars_daily_raw`: every raw row, with the partition's `date`.
- `bars_daily_adj`: raw x cumulative factor, prev_close by the factor of its own day (NSE's
  ex-date PREVCLOSE is already the adjusted close, F14); volume is divided by the same factor
  (multiplied by its reciprocal, rounded to 12 places); `raw_close` keeps the unadjusted close
  for the rules on an absolute rupee price (`min_price`, the NSE tick slab). A bar of an older
  ISIN linked by the ISIN lineage (`data/lineage.py`) carries the id of its chain's current
  ISIN, so a chain reads as one instrument.
- `bars_daily`: `bars_daily_adj` with one row per (instrument_id, date), from the latest
  ingest run (run ids are uuid v7, so byte order is time order).
- `index_daily`: index levels.

Nothing here opens a network connection.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from ..errors import HatError
from ..money import MONEY_SCALE, exact
from ..settings import DEFAULT_DUCKDB_MEMORY

DATASET_BARS = "bars_daily_raw"
DATASET_INDEX = "index_daily"
DATASET_FEATURES = "features_daily"  # written by data/features.py (HAT-DATA-070)
DATASET_BREADTH = "breadth_daily"  # one row per day (HAT-DATA-076)
DATASET_MARKET_BREADTH = "market_breadth"  # advances and declines, one row per day (HAT-DATA-044)
RAW_DIR = "raw"
SOURCES = frozenset({"bhavcopy", "mcp"})  # adjusted values have no source: they are never stored

PRICE_TYPE = pa.decimal128(18, 4)
BAR_SCHEMA = pa.schema(
    [
        ("instrument_id", pa.string()),
        ("isin", pa.string()),
        ("symbol", pa.string()),
        ("series", pa.string()),
        ("open", PRICE_TYPE),
        ("high", PRICE_TYPE),
        ("low", PRICE_TYPE),
        ("close", PRICE_TYPE),
        ("prev_close", PRICE_TYPE),
        ("volume", pa.int64()),
        ("traded_value", PRICE_TYPE),
        ("source", pa.string()),
        ("ingest_run_id", pa.string()),
    ]
)
INDEX_SCHEMA = pa.schema(
    [
        ("index_name", pa.string()),
        ("open", PRICE_TYPE),
        ("high", PRICE_TYPE),
        ("low", PRICE_TYPE),
        ("close", PRICE_TYPE),
    ]
)
# The feature columns, in order: the instrument, then one float64 per feature.
FEATURE_NAMES = (
    "sma10",
    "sma20",
    "sma50",
    "sma150",
    "sma200",
    "high_52w",
    "low_52w",
    "adr20",
    "adtr20",
    "adv20",
    "turnover20",
    "ret21",
    "ret63",
    "ret126",
    "ret252",
    "rs_weighted",  # 0.4 ret63 + 0.2 ret126 + 0.2 ret189 + 0.2 ret252, what rs_pct ranks
    "rs_pct",
    "mom_rank",
)
FEATURES_SCHEMA = pa.schema(
    [("instrument_id", pa.string()), *[(name, pa.float64()) for name in FEATURE_NAMES]]
)
BREADTH_SCHEMA = pa.schema(
    [
        ("universe_size", pa.int64()),
        ("with_sma50", pa.int64()),
        ("above_sma50", pa.int64()),
        ("breadth_pct", pa.float64()),
    ]
)
MARKET_BREADTH_SCHEMA = pa.schema(
    [
        ("total_stocks", pa.int64()),
        ("advances", pa.int64()),
        ("declines", pa.int64()),
        ("unchanged", pa.int64()),
        ("ad_ratio", pa.float64()),
        ("volume", pa.int64()),
        ("source", pa.string()),
    ]
)
DATASET_SCHEMAS = {
    DATASET_BARS: BAR_SCHEMA,
    DATASET_INDEX: INDEX_SCHEMA,
    DATASET_FEATURES: FEATURES_SCHEMA,
    DATASET_BREADTH: BREADTH_SCHEMA,
    DATASET_MARKET_BREADTH: MARKET_BREADTH_SCHEMA,
}

PART_GLOB = "part-*.parquet"
MAX_VOLUME = 10**14  # the adjusted volume column is decimal(14, 0) x decimal(24, 12)
FACTOR_PLACES = 18  # cumulative factors are stored as decimal(20, 18)
INVERSE_PLACES = 12  # and their reciprocals, for volume, as decimal(24, 12)
FACTOR_MIN = Decimal("1e-9")  # a cumulative factor outside [1e-9, 100) cannot be held exactly
FACTOR_MAX = Decimal(100)  # (a stock needs several reverse splits to get anywhere near it)


class StoreError(HatError):
    """A store rule was broken; `code` is stable and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BarRow:
    """One raw daily bar. `traded_value` is None for MCP rows (HAT-DATA-011)."""

    instrument_id: uuid.UUID | str
    isin: str
    symbol: str
    series: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal | None
    volume: int
    traded_value: Decimal | None
    source: str
    ingest_run_id: uuid.UUID | str

    @property
    def key(self) -> str:
        return str(self.instrument_id)


@dataclass(frozen=True)
class IndexRow:
    index_name: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ActionFactor:
    """What the adjusted view needs of a corporate action."""

    instrument_id: uuid.UUID | str
    ex_date: date
    factor: Decimal


@dataclass(frozen=True)
class Adjustment:
    """What the adjusted view applies: the price factors and the ISIN lineage. `lineage` maps
    the id of each linked older ISIN to the id of its chain's current ISIN; the view gives the
    older bars that id, so the factors of a chain are keyed by it (`lineage.stored_adjustment`)."""

    factors: tuple[ActionFactor, ...] = ()
    lineage: Mapping[str, str] = field(default_factory=dict)


NO_ADJUSTMENT = Adjustment()  # raw prices: the views without factors or lineage


@dataclass
class PartitionReport:
    """Which expected days have data (HAT-DATA-091, 096). A missing day stays missing."""

    row_counts: dict[date, int] = field(default_factory=dict)
    missing: list[date] = field(default_factory=list)
    unexpected: list[date] = field(default_factory=list)  # data on a day that is not expected


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if exact(value, places=MONEY_SCALE) != value:
        raise StoreError("store_price_precision")  # never rounded silently
    return value


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise StoreError("store_bad_source")


def bars_table(rows: Sequence[BarRow]) -> pa.Table:
    """The rows as an Arrow table of `BAR_SCHEMA`; rejects duplicates, a mixed run, bad values."""
    keys = [r.key for r in rows]
    if len(set(keys)) != len(keys):
        raise StoreError("store_duplicate_key")
    if len({str(r.ingest_run_id) for r in rows}) > 1:
        raise StoreError("store_mixed_run")
    for r in rows:
        _check_source(r.source)
        if not 0 <= r.volume < MAX_VOLUME:
            raise StoreError("store_bad_volume")
    columns = {
        "instrument_id": [r.key for r in rows],
        "isin": [r.isin for r in rows],
        "symbol": [r.symbol for r in rows],
        "series": [r.series for r in rows],
        "open": [_money(r.open) for r in rows],
        "high": [_money(r.high) for r in rows],
        "low": [_money(r.low) for r in rows],
        "close": [_money(r.close) for r in rows],
        "prev_close": [_money(r.prev_close) for r in rows],
        "volume": [r.volume for r in rows],
        "traded_value": [_money(r.traded_value) for r in rows],
        "source": [r.source for r in rows],
        "ingest_run_id": [str(r.ingest_run_id) for r in rows],
    }
    return pa.table(columns, schema=BAR_SCHEMA)


def index_table(rows: Sequence[IndexRow]) -> pa.Table:
    names = [r.index_name for r in rows]
    if len(set(names)) != len(names):
        raise StoreError("store_duplicate_key")
    columns = {
        "index_name": names,
        "open": [_money(r.open) for r in rows],
        "high": [_money(r.high) for r in rows],
        "low": [_money(r.low) for r in rows],
        "close": [_money(r.close) for r in rows],
    }
    return pa.table(columns, schema=INDEX_SCHEMA)


def cumulative_segments(
    actions: Iterable[ActionFactor],
) -> list[tuple[str, date | None, date | None, Decimal, Decimal]]:
    """(instrument_id, from, to, factor, inverse): for dates d with from <= d < to the factor is
    the product of the action factors with ex_date > d (HAT-DATA-053). The first segment has no
    lower bound; dates on or after the last ex-date need no row (factor 1)."""
    by_instrument: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for a in actions:
        if not a.factor.is_finite() or a.factor <= 0:
            raise StoreError("store_bad_factor")
        per_day = by_instrument[str(a.instrument_id)]
        per_day[a.ex_date] = per_day.get(a.ex_date, Decimal(1)) * a.factor
    out = []
    with localcontext() as ctx:
        ctx.prec = 80
        for instrument, per_day in by_instrument.items():
            days = sorted(per_day)
            for i, start in enumerate([None, *days[:-1]]):
                end = days[i]
                product = Decimal(1)
                for later in days[i:]:
                    product *= per_day[later]
                if not FACTOR_MIN <= product < FACTOR_MAX:
                    raise StoreError("store_factor_range")  # would overflow the view's decimals
                factor = product.quantize(Decimal(1).scaleb(-FACTOR_PLACES), ROUND_HALF_EVEN)
                inverse = (Decimal(1) / product).quantize(
                    Decimal(1).scaleb(-INVERSE_PLACES), ROUND_HALF_EVEN
                )
                out.append((instrument, start, end, factor, inverse))
    return out


# The factor columns of ca_segment are DECIMAL(20,18) and DECIMAL(24,12) (FACTOR_PLACES and
# INVERSE_PLACES); the view is a constant so no text is ever built into SQL. `lineage` maps a
# linked older ISIN's id to its chain's id.
_ADJ_VIEW_SQL = """
CREATE VIEW bars_daily_adj AS
SELECT COALESCE(l.chain_id, r.instrument_id) AS instrument_id, r.date, r.isin, r.symbol, r.series,
       r.open * COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS open,
       r.high * COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS high,
       r.low * COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS low,
       r.close * COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS close,
       r.prev_close * COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS prev_close,
       CAST(r.volume AS DECIMAL(14,0)) * COALESCE(c.inverse, CAST(1 AS DECIMAL(24,12))) AS volume,
       r.traded_value, r.close AS raw_close, r.source, r.ingest_run_id
FROM bars_daily_raw r
LEFT JOIN lineage l ON l.instrument_id = r.instrument_id
LEFT JOIN ca_segment c ON c.instrument_id = COALESCE(l.chain_id, r.instrument_id)
     AND r.date >= COALESCE(c.from_date, DATE '0001-01-01') AND r.date < c.to_date
"""
# The open-ended first segment's NULL `from_date` is read as the first day there is rather than
# with an `OR`: DuckDB plans an `OR` in the condition as a nested-loop join over every segment
# (24 s of CPU for five years of bars), the equality as a hash join (DB1).
_COUNT_SQL = {
    DATASET_BARS: "SELECT date, count(*) FROM bars_daily_raw GROUP BY date ORDER BY date",
    DATASET_INDEX: "SELECT date, count(*) FROM index_daily GROUP BY date ORDER BY date",
}


def atomic_replace(
    path: Path,
    write: Callable[[Path], None],
    replace: Callable[[Path, Path], Any] = os.replace,
) -> None:
    """`write` fills a temporary file next to `path`, which then replaces it in one rename; any
    failure removes the temporary file and leaves `path` as it was (HAT-DATA-082)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write(tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class DataStore:
    """The HAT Parquet datasets under one root: the settings' `data_root` (HAT-DATA-080). There
    is no default: `app.paths.HAT_DATA_ROOT` is read when `app.paths` is imported, before
    anything loads `.env`, so a store built from it would ignore a root set only there."""

    def __init__(
        self,
        root: Path,
        *,
        writer: Callable[[pa.Table, Path], None] | None = None,
        replace: Callable[[Path, Path], Any] = os.replace,
        memory_limit: str = DEFAULT_DUCKDB_MEMORY,
        temp_dir: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.memory_limit = memory_limit
        self.temp_dir = Path(temp_dir) if temp_dir is not None else self.root / "tmp"
        self._writer = writer or (lambda table, path: pq.write_table(table, path))
        self._replace = replace

    # --- paths ------------------------------------------------------------------------------

    def _under_root(self, path: Path) -> Path:
        base = self.root.resolve()
        resolved = path.resolve()
        if resolved != base and base not in resolved.parents:
            raise StoreError("store_outside_root")
        return path

    def partition_dir(self, dataset: str, day: date) -> Path:
        if dataset not in DATASET_SCHEMAS:
            raise StoreError("store_unknown_dataset")
        return self._under_root(self.root / dataset / f"date={day.isoformat()}")

    def partition_files(self, dataset: str, day: date) -> list[Path]:
        directory = self.partition_dir(dataset, day)
        return sorted(directory.glob(PART_GLOB)) if directory.is_dir() else []

    def raw_path(self, name: str) -> Path:
        if not name or name != Path(name).name or name.startswith("."):
            raise StoreError("store_bad_raw_name")
        return self._under_root(self.root / RAW_DIR / name)

    # --- writes -----------------------------------------------------------------------------

    def _write(self, path: Path, table: pa.Table) -> None:
        atomic_replace(path, lambda tmp: self._writer(table, tmp), self._replace)

    def write_bars(self, day: date, rows: Sequence[BarRow]) -> Path:
        """One file per ingest run in the day's partition; rewriting the same run replaces it, so
        repeating a write changes nothing. Returns the file."""
        if not rows:
            raise StoreError("store_empty_write")
        table = bars_table(rows)
        run_id = str(rows[0].ingest_run_id)
        path = self.partition_dir(DATASET_BARS, day) / f"part-{run_id}.parquet"
        self._write(path, table)
        return path

    def upsert_index(self, day: date, rows: Sequence[IndexRow]) -> int:
        """Merge index rows into the day's file: a row replaces the one of the same index, others
        stay, so each index has exactly one row per day however often a load repeats."""
        incoming = {r.index_name: r for r in rows}
        if len(incoming) != len(rows):
            raise StoreError("store_duplicate_key")
        path = self.partition_dir(DATASET_INDEX, day) / "part-index.parquet"
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".index.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # read-merge-write must not interleave
            merged = {r.index_name: r for r in self.read_index(day)}
            merged.update(incoming)
            self._write(path, index_table([merged[k] for k in sorted(merged)]))
        return len(merged)

    def write_partition(self, dataset: str, day: date, name: str, table: pa.Table) -> Path:
        """Replace the day's `part-<name>.parquet` of a derived dataset (features, breadth) by
        `table`, which must have exactly the dataset's schema. Writing the same table again
        rewrites the same file (HAT-DATA-082)."""
        if (
            dataset not in (DATASET_FEATURES, DATASET_BREADTH, DATASET_MARKET_BREADTH)
            or table.schema != DATASET_SCHEMAS[dataset]
        ):
            raise StoreError("store_bad_schema")
        if not name.isalnum():
            raise StoreError("store_bad_raw_name")
        path = self.partition_dir(dataset, day) / f"part-{name}.parquet"
        self._write(path, table)
        return path

    def read_partition(self, dataset: str, day: date) -> pl.DataFrame:
        """The day's rows of a dataset across its files; an absent day is empty."""
        files = self.partition_files(dataset, day)
        if not files:
            return pl.DataFrame(schema=pl.from_arrow(DATASET_SCHEMAS[dataset].empty_table()).schema)  # type: ignore[union-attr]
        return pl.concat([pl.read_parquet(f) for f in files])

    def store_raw(self, name: str, data: bytes) -> bool:
        """Keep a raw bhavcopy zip untouched forever (HAT-DATA-083). True when written, False when
        the identical bytes were already there; different bytes under an existing name raise."""
        path = self.raw_path(name)
        if path.exists():
            if path.read_bytes() == data:
                return False
            raise StoreError("store_raw_immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            try:
                os.link(tmp, path)  # fails if the name appeared meanwhile: never overwritten
            except FileExistsError:
                if path.read_bytes() != data:
                    raise StoreError("store_raw_immutable") from None
                return False
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return True

    def raw_exists(self, name: str) -> bool:
        return self.raw_path(name).is_file()

    def read_raw(self, name: str) -> bytes:
        return self.raw_path(name).read_bytes()

    # --- reads ------------------------------------------------------------------------------

    def read_bars(self, day: date) -> pl.DataFrame:
        """All raw rows of a day across runs; an absent day is empty, never filled."""
        files = self.partition_files(DATASET_BARS, day)
        if not files:
            return pl.DataFrame(schema=pl.from_arrow(BAR_SCHEMA.empty_table()).schema)  # type: ignore[union-attr]
        return pl.concat([pl.read_parquet(f) for f in files])

    def read_index(self, day: date) -> list[IndexRow]:
        rows: list[IndexRow] = []
        for path in self.partition_files_index(day):
            table = pq.read_table(path)
            rows.extend(
                IndexRow(r["index_name"], r["open"], r["high"], r["low"], r["close"])
                for r in table.to_pylist()
            )
        return rows

    def partition_files_index(self, day: date) -> list[Path]:
        directory = self.partition_dir(DATASET_INDEX, day)
        return sorted(directory.glob(PART_GLOB)) if directory.is_dir() else []

    def days(self, dataset: str) -> list[date]:
        base = self._under_root(self.root / dataset)
        if not base.is_dir():
            return []
        found = []
        for child in sorted(base.glob("date=*")):
            if any(child.glob(PART_GLOB)):
                found.append(date.fromisoformat(child.name[5:]))
        return found

    def fingerprints(self, dataset: str) -> dict[date, str]:
        """Per day with data, its part files as `name:size:mtime_ns` joined by `;`. A bars file
        name carries its ingest run id and every write replaces the file (a new mtime), so a new
        run, a rewritten run or a removed file changes the day's fingerprint (the features job's
        invalidation, DB5). A copy that does not keep mtimes changes every fingerprint."""
        base = self._under_root(self.root / dataset)
        if not base.is_dir():
            return {}
        found = {}
        for child in sorted(base.glob("date=*")):
            parts = sorted(
                f"{f.name}:{stat.st_size}:{stat.st_mtime_ns}"
                for f in child.glob(PART_GLOB)
                if (stat := f.stat())
            )
            if parts:
                found[date.fromisoformat(child.name[5:])] = ";".join(parts)
        return found

    def bar_instruments(self, day: date) -> set[str]:
        """The instrument ids with a raw bar on `day`, across runs (only that column is read)."""
        return {
            str(i)
            for path in self.partition_files(DATASET_BARS, day)
            for i in pq.read_table(path, columns=["instrument_id"]).column(0).to_pylist()
        }

    # --- DuckDB -----------------------------------------------------------------------------

    def connect(self, adjustment: Adjustment = NO_ADJUSTMENT) -> duckdb.DuckDBPyConnection:
        """An in-memory DuckDB connection with the four views; `adjustment` is what the adjusted
        view applies (`lineage.stored_adjustment`: the corporate actions and the ISIN lineage).
        DuckDB's own default is 80% of RAM; the store's `memory_limit` caps it and a larger query
        spills to `temp_dir`."""
        con = duckdb.connect(
            ":memory:",
            config={"memory_limit": self.memory_limit, "temp_directory": str(self.temp_dir)},
        )
        self._dataset_view(con, DATASET_BARS, "bars_daily_raw")
        self._dataset_view(con, DATASET_INDEX, "index_daily")
        # Loaded from Arrow tables: row-by-row inserts of ~1,500 segments cost half a second.
        links = sorted(adjustment.lineage.items())
        tables = {
            "ca_segment": segment_table(cumulative_segments(adjustment.factors)),
            "lineage": pa.table(
                {
                    "instrument_id": pa.array([old for old, _ in links], pa.string()),
                    "chain_id": pa.array([new for _, new in links], pa.string()),
                }
            ),
        }
        for name, table in tables.items():
            con.register("_load", table)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM _load")  # noqa: S608 - constant
            con.unregister("_load")
        con.execute(_ADJ_VIEW_SQL)
        con.execute(
            """
            CREATE VIEW bars_daily AS
            SELECT * FROM bars_daily_adj
            QUALIFY row_number() OVER (
                PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1
            """
        )
        return con

    def _dataset_view(self, con: duckdb.DuckDBPyConnection, dataset: str, view: str) -> None:
        """A view over the dataset's part files by a glob that DuckDB expands and prunes by the
        `date=` partition at query time (listing ~7,400 files inline cost most of a connect)."""
        base = self._under_root(self.root / dataset)
        if next(base.glob(f"date=*/{PART_GLOB}"), None) is not None:
            pattern = str(base / "date=*" / PART_GLOB).replace("'", "''")
            con.execute(
                f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{pattern}', "  # noqa: S608
                "hive_partitioning = true, hive_types = {'date': DATE})"
            )
            return
        columns = ", ".join(f"{f.name} {_duck_type(f.type)}" for f in DATASET_SCHEMAS[dataset])
        con.execute(f"CREATE TABLE {view} ({columns}, date DATE)")


def segment_table(
    segments: Sequence[tuple[str, date | None, date | None, Decimal, Decimal]],
) -> pa.Table:
    """`cumulative_segments` as the Arrow table of the view's `ca_segment` columns."""
    return pa.table(
        {
            "instrument_id": pa.array([s[0] for s in segments], pa.string()),
            "from_date": pa.array([s[1] for s in segments], pa.date32()),
            "to_date": pa.array([s[2] for s in segments], pa.date32()),
            "factor": pa.array([s[3] for s in segments], pa.decimal128(20, FACTOR_PLACES)),
            "inverse": pa.array([s[4] for s in segments], pa.decimal128(24, INVERSE_PLACES)),
        }
    )


def latest_rows(frame: pl.DataFrame) -> pl.DataFrame:
    """The latest ingest run's row per (`instrument_id`, `date`) of rows read from
    `bars_daily_adj` with their `ingest_run_id`, as the `bars_daily` view keeps, in the order
    read; done in polars, as a window over a slice in DuckDB costs several times the memory of
    the slice, and only when a day was written by more than one run, which is rare."""
    key = ["instrument_id", "date"]
    if not frame.select(pl.struct(key).is_duplicated().any()).item():
        return frame
    latest = frame.filter(pl.col("ingest_run_id") == pl.col("ingest_run_id").max().over(key))
    return latest.unique(key, keep="last", maintain_order=True)


def _duck_type(arrow_type: pa.DataType) -> str:
    if pa.types.is_decimal(arrow_type):
        return f"DECIMAL({arrow_type.precision},{arrow_type.scale})"
    if pa.types.is_int64(arrow_type):
        return "BIGINT"
    return "VARCHAR"


def partition_report(
    store: DataStore, dataset: str, expected_days: Collection[date]
) -> PartitionReport:
    """Row count per day found, the expected days with no rows (missing) and the days with data
    that are not expected. Missing days are reported and left empty (HAT-DATA-091)."""
    report = PartitionReport()
    con = store.connect()
    try:
        for day, count in con.execute(_COUNT_SQL[dataset]).fetchall():
            report.row_counts[day] = count
    finally:
        con.close()
    expected = set(expected_days)
    report.missing = sorted(d for d in expected if report.row_counts.get(d, 0) == 0)
    report.unexpected = sorted(d for d in report.row_counts if d not in expected)
    return report


def index_coverage(
    store: DataStore, trading_days: Collection[date], starts: dict[str, date]
) -> dict[str, list[date]]:
    """Per index, the trading days on or after its start date that have no row (HAT-DATA-095).
    Raises when an index has more than one row on a day."""
    con = store.connect()
    try:
        counts = {
            (name, day): n
            for name, day, n in con.execute(
                "SELECT index_name, date, count(*) FROM index_daily GROUP BY index_name, date"
            ).fetchall()
        }
    finally:
        con.close()
    if any(n > 1 for n in counts.values()):
        raise StoreError("store_duplicate_key")
    return {
        name: sorted(d for d in trading_days if d >= start and (name, d) not in counts)
        for name, start in starts.items()
    }
