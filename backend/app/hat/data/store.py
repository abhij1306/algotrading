"""Parquet datasets under `HAT_DATA_ROOT` and the DuckDB views over them (HAT-DATA-080..085,
HAT-DEC-T6, HAT-DEC-B27).

Layout: `bars_daily_raw/date=YYYY-MM-DD/part-<ingest_run_id>.parquet`,
`index_daily/date=YYYY-MM-DD/part.parquet`, `raw/<file>` (bhavcopy zips, immutable) and
`ca_bc/bc_<date>.csv` (the nightly bc files, immutable). A partition file is written to a
temporary name and renamed over the final name, so a reader sees the whole old file or the whole
new one (HAT-DATA-082). Raw prices are stored as decimal(18,4) and never rewritten; adjusted
prices exist only as the `bars_daily_adj` view, which multiplies them by cumulative factors
computed from corporate actions (HAT-DATA-053, 054).

Views, per connection from `DataStore.connect`:
- `bars_daily_raw`: every raw row, with the partition's `date`.
- `bars_daily_cap_adj`: the price basis: raw x the cumulative factor of the price-changing
  corporate actions (splits, bonuses, rights, demergers); prev_close by the factor of its own
  day, and on an action's ex-session (the chain's first bar on or after its ex-date) by the
  previous session's factor as well when NSE's PREVCLOSE is the previous raw close, which it
  is on nearly every ex-date (`ex_prev_factor`); volume is divided by the same
  factor (multiplied by its reciprocal, rounded to 12 places); `raw_close` keeps the unadjusted
  close for the rules on an absolute rupee price (`min_price`, the NSE tick slab). A bar of an
  older ISIN linked by the ISIN lineage (`data/lineage.py`) carries the id of its chain's current
  ISIN, so a chain reads as one instrument. The backtest fills, marks and pays dividends on it
  (CA3).
- `bars_daily_adj`: the total-return series every signal reads (CA3): the price basis times the
  cumulative factor of the dividends as well, each 1 - D / the raw close before its ex-date
  (`dividend_factor`), applied once the bars hold its ex-date (a dividend ahead is not priced
  in); an ex-date's prev_close takes the day's dividend factor too (NSE's PREVCLOSE is the raw
  close before it). Volume and `raw_close` are the price basis's: a dividend changes no share
  count.
- `bars_daily` and `bars_daily_cap`: those views with one row per (instrument_id, date), from the
  latest ingest run (`LATEST_ROW`: run ids are uuid v7, so byte order is time order), and
  `bars_daily_raw_latest` the raw bars so.
- `index_daily`: index levels.
- Both adjusted views are hindsight: every factor and link as stored now, whatever date a bar
  has (CA6). A backtest reads them and skips the days they are not point in time
  (`ca_versions.pending_days`).
- Both adjusted views carry `div_factor`: the cumulative factor of the dividends after the
  bar's date (the total-return price over the price-basis one), so a reader can rescale a
  series to any day's scale (`features`, the backtest's `MarketView`).
- tables `div_segment` (that factor per instrument and date range), `dividend` (each priced
  dividend: chain, ex-date, the session before, the cash, that session's raw close and the
  factor), and `ex_cap` / `ex_tr` (per chain and session, what its prev_close takes on top of
  the day's own factor on each basis).

Nothing here opens a network connection.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
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
BC_DIR = "ca_bc"  # the nightly bc files, kept as fetched: bc_<trade date>.csv
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
class DividendCash:
    """A stored dividend as the total-return view needs it: the chain it is paid on (keyed as
    `ActionFactor`), its ex-date and the cash per share in rupees."""

    instrument_id: uuid.UUID | str
    ex_date: date
    amount: Decimal


@dataclass(frozen=True)
class Adjustment:
    """What the adjusted views apply: the price factors, the dividends and the ISIN lineage.
    `lineage` maps the id of each linked older ISIN to the id of its chain's current ISIN; the
    view gives the older bars that id, so the factors and dividends of a chain are keyed by it
    (`chains.stored_adjustment`)."""

    factors: tuple[ActionFactor, ...] = ()
    lineage: Mapping[str, str] = field(default_factory=dict)
    dividends: tuple[DividendCash, ...] = ()


def dividend_factor(amount: Decimal, previous_close: Decimal) -> Decimal | None:
    """The total-return factor of a cash distribution: 1 - amount / the raw close before the
    ex-date, applied to every price before it like any other factor. None when it is not in
    (0, 1): no close, no cash, or cash at or above the price (never a factor)."""
    if not amount.is_finite() or not previous_close.is_finite() or previous_close <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        factor = 1 - amount / previous_close
    return factor if 0 < factor < 1 else None


NO_ADJUSTMENT = Adjustment()  # raw prices: the views without factors or lineage


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


# The factor columns of the segment tables are DECIMAL(20,18) and DECIMAL(24,12) (FACTOR_PLACES
# and INVERSE_PLACES); the views are constants so no text is ever built into SQL. `lineage` maps a
# linked older ISIN's id to its chain's id. `ca_segment` holds the price basis's factors,
# `tr_segment` those times the dividends' (its inverse is unused: volume keeps the price basis's).
# The open-ended first segment's NULL `from_date` is read as the first day there is rather than
# with an `OR`: DuckDB plans an `OR` in the condition as a nested-loop join over every segment
# (24 s of CPU for five years of bars), the equality as a hash join (DB1).
_SEGMENT_TABLES = ("ca_segment", "tr_segment", "div_segment")
_ADJ_VIEW_SQL = """
CREATE VIEW {view} AS
SELECT COALESCE(l.chain_id, r.instrument_id) AS instrument_id, r.date, r.isin, r.symbol, r.series,
       r.open * COALESCE(p.factor, CAST(1 AS DECIMAL(20,18))) AS open,
       r.high * COALESCE(p.factor, CAST(1 AS DECIMAL(20,18))) AS high,
       r.low * COALESCE(p.factor, CAST(1 AS DECIMAL(20,18))) AS low,
       r.close * COALESCE(p.factor, CAST(1 AS DECIMAL(20,18))) AS close,
       r.prev_close * CAST(COALESCE(p.factor, CAST(1 AS DECIMAL(20,18)))
           * COALESCE(x.factor, CAST(1 AS DECIMAL(20,18))) AS DECIMAL(20,18)) AS prev_close,
       CAST(r.volume AS DECIMAL(14,0)) * COALESCE(c.inverse, CAST(1 AS DECIMAL(24,12))) AS volume,
       r.traded_value, r.close AS raw_close, r.source, r.ingest_run_id,
       COALESCE(d.factor, CAST(1 AS DECIMAL(20,18))) AS div_factor
FROM bars_daily_raw r
LEFT JOIN lineage l ON l.instrument_id = r.instrument_id
LEFT JOIN ca_segment c ON c.instrument_id = COALESCE(l.chain_id, r.instrument_id)
     AND r.date >= COALESCE(c.from_date, DATE '0001-01-01') AND r.date < c.to_date
LEFT JOIN div_segment d ON d.instrument_id = COALESCE(l.chain_id, r.instrument_id)
     AND r.date >= COALESCE(d.from_date, DATE '0001-01-01') AND r.date < d.to_date
LEFT JOIN {segment} p ON p.instrument_id = COALESCE(l.chain_id, r.instrument_id)
     AND r.date >= COALESCE(p.from_date, DATE '0001-01-01') AND r.date < p.to_date
LEFT JOIN {ex_day} x ON x.instrument_id = COALESCE(l.chain_id, r.instrument_id)
     AND x.ex_date = r.date
"""
# (view, its price segment table, what an ex-session's prev_close takes on top of its own
# factor, the one-row-per-day view over it). NSE's PREVCLOSE on an ex-date is mostly the previous
# raw close, so it takes the factor the previous bar's close has: the day's price actions'
# (`ex_prev_factor`) and, on the total-return series, its dividends'.
_ADJ_VIEWS = (
    ("bars_daily_cap_adj", "ca_segment", "ex_cap", "bars_daily_cap"),
    ("bars_daily_adj", "tr_segment", "ex_tr", "bars_daily"),
)
# One (instrument, date) written by more than one ingest run reads as the latest run's row: run
# ids are uuid v7, so byte order is time order. `LATEST_ROW` keeps that row of a select (DuckDB
# applies a date or instrument filter before it); `newest` is the same rule inside an aggregate,
# for a reader that joins first. `bars_daily_raw_latest` is the raw bars with the rule applied.
LATEST_ROW = (
    "QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1"
)
RAW_LATEST = "bars_daily_raw_latest"


def newest(column: str, alias: str = "r") -> str:
    """`column` of the latest ingest run's row, in an aggregate over `alias` (`LATEST_ROW`):
    `arg_max_null`, as plain `arg_max` skips a NULL and would return an older run's value."""
    return f"arg_max_null({alias}.{column}, {alias}.ingest_run_id)"


_LATEST_VIEW_SQL = "CREATE VIEW {latest} AS SELECT * FROM {view} " + LATEST_ROW  # noqa: S608
# Each dividend's chain id, the raw close and date of the chain's last session in the 30 days
# before its ex-date (the latest ingest run's row) and whether the chain has a bar on or after
# the ex-date (a dividend ahead of the bars is not priced in).
_DIVIDEND_CLOSE_SQL = f"""
WITH chained AS (
    SELECT COALESCE(l.chain_id, r.instrument_id) AS chain, r.date, r.close, r.ingest_run_id
    FROM bars_daily_raw r LEFT JOIN lineage l ON l.instrument_id = r.instrument_id
    WHERE COALESCE(l.chain_id, r.instrument_id) IN (SELECT chain FROM div_in)
),
day_close AS (
    SELECT d.k, c.date, {newest("close", "c")} AS close
    FROM div_in d JOIN chained c ON c.chain = d.chain AND c.date BETWEEN d.ex - 30 AND d.ex - 1
    GROUP BY d.k, c.date
),
last_day AS (SELECT chain, max(date) AS last_date FROM chained GROUP BY chain)
SELECT d.k, arg_max(dc.date, dc.date), arg_max(dc.close, dc.date)
FROM div_in d
JOIN day_close dc ON dc.k = d.k
JOIN last_day ld ON ld.chain = d.chain AND ld.last_date >= d.ex
GROUP BY d.k
"""  # noqa: S608
# Each price action's ex-session (the chain's first session on or after its ex-date), that
# session's raw prev_close and the raw close of the chain's session before it (NULL when there is
# none), on the latest ingest run's rows; an action ahead of the bars has no row.
_EX_SESSION_SQL = f"""
WITH chained AS (
    SELECT COALESCE(l.chain_id, r.instrument_id) AS chain, r.date, r.close, r.prev_close,
           r.ingest_run_id
    FROM bars_daily_raw r LEFT JOIN lineage l ON l.instrument_id = r.instrument_id
    WHERE COALESCE(l.chain_id, r.instrument_id) IN (SELECT chain FROM ex_in)
),
day AS (
    SELECT chain, date, {newest("close", "c")} AS close, {newest("prev_close", "c")} AS prev_close
    FROM chained c GROUP BY chain, date
)
SELECT e.k, s.date, s.prev_close, p.close
FROM ex_in e
ASOF JOIN day s ON s.chain = e.chain AND s.date >= e.ex
ASOF LEFT JOIN day p ON p.chain = e.chain AND p.date < e.ex
"""  # noqa: S608


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


def _keep_once(path: Path, data: bytes) -> bool:
    """Write `data` to `path` once, never over another file. True when written, False when the
    identical bytes were already there; different bytes under an existing name raise."""
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
        return _keep_once(self.raw_path(name), data)

    def store_bc(self, day: date, text: str) -> Path:
        """Keep the night's `bc` file of NSE's PR archive untouched under `ca_bc/`, dated by its
        trade date (owner 2026-10-25: so moved demerger notices can later be checked against
        the files NSE listed each night). A later, different file of the same day is kept beside
        the first under its full SHA-256; the identical file is kept once. The kept path."""
        data = text.encode("utf-8")
        path = self._under_root(self.root / BC_DIR / f"bc_{day.isoformat()}.csv")
        try:
            _keep_once(path, data)
        except StoreError:
            digest = hashlib.sha256(data).hexdigest()
            path = path.with_name(f"bc_{day.isoformat()}_{digest}.csv")
            _keep_once(path, data)
        return path

    def raw_exists(self, name: str) -> bool:
        return self.raw_path(name).is_file()

    def read_raw(self, name: str) -> bytes:
        return self.raw_path(name).read_bytes()

    # --- reads ------------------------------------------------------------------------------

    def read_bars(self, day: date) -> pl.DataFrame:
        """The raw rows of a day, the latest ingest run's row per instrument when a day was
        written by more than one run (as the `bars_daily` view keeps); an absent day is empty,
        never filled."""
        files = self.partition_files(DATASET_BARS, day)
        if not files:
            return pl.DataFrame(schema=pl.from_arrow(BAR_SCHEMA.empty_table()).schema)  # type: ignore[union-attr]
        return latest_rows(pl.concat([pl.read_parquet(f) for f in files]), ("instrument_id",))

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
        """An in-memory DuckDB connection with the views; `adjustment` is what the adjusted
        view applies (`chains.stored_adjustment`: the corporate actions and the ISIN lineage).
        DuckDB's own default is 80% of RAM; the store's `memory_limit` caps it and a larger query
        spills to `temp_dir`."""
        con = duckdb.connect(
            ":memory:",
            config={"memory_limit": self.memory_limit, "temp_directory": str(self.temp_dir)},
        )
        self._dataset_view(con, DATASET_BARS, "bars_daily_raw")
        con.execute(_LATEST_VIEW_SQL.format(latest=RAW_LATEST, view="bars_daily_raw"))
        self._dataset_view(con, DATASET_INDEX, "index_daily")
        # Loaded from Arrow tables: row-by-row inserts of ~1,500 segments cost half a second.
        links = sorted(adjustment.lineage.items())
        _load(
            con,
            "lineage",
            pa.table(
                {
                    "instrument_id": pa.array([old for old, _ in links], pa.string()),
                    "chain_id": pa.array([new for _, new in links], pa.string()),
                }
            ),
        )
        priced = _price_dividends(con, adjustment.dividends)
        _load(con, "dividend", dividend_table(priced))
        paid = [ActionFactor(row[0], row[1], row[5]) for row in priced]
        for table, actions in zip(
            _SEGMENT_TABLES, (adjustment.factors, (*adjustment.factors, *paid), paid), strict=True
        ):
            _load(con, table, segment_table(cumulative_segments(actions)))
        ex_price = _ex_prev_factors(con, adjustment.factors)
        ex_total = dict(ex_price)
        for chain, ex, *_, factor in priced:
            ex_total[(chain, ex)] = ex_total.get((chain, ex), Decimal(1)) * factor
        _load(con, "ex_cap", ex_day_table(ex_price))
        _load(con, "ex_tr", ex_day_table(ex_total))
        for view, segment, ex_day, latest in _ADJ_VIEWS:
            con.execute(_ADJ_VIEW_SQL.format(view=view, segment=segment, ex_day=ex_day))
            con.execute(_LATEST_VIEW_SQL.format(latest=latest, view=view))
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


def _price_dividends(
    con: duckdb.DuckDBPyConnection, dividends: Sequence[DividendCash]
) -> list[tuple[str, date, date, Decimal, Decimal, Decimal]]:
    """(chain, ex-date, previous session, cash, its raw close, factor) of each dividend the bars
    can price: one per chain and ex-date (the amounts of two rows of one day add up), with a
    session of the chain in the 30 days before the ex-date and a bar on or after it."""
    cash: dict[tuple[str, date], Decimal] = defaultdict(Decimal)
    for d in dividends:
        cash[(str(d.instrument_id), d.ex_date)] += d.amount
    if not cash:
        return []
    keys = sorted(cash)
    _load(
        con,
        "div_in",
        pa.table(
            {
                "k": pa.array(range(len(keys)), pa.int32()),
                "chain": pa.array([c for c, _ in keys], pa.string()),
                "ex": pa.array([ex for _, ex in keys], pa.date32()),
            }
        ),
    )
    out = []
    for k, prev_date, prev_close in con.execute(_DIVIDEND_CLOSE_SQL).fetchall():
        chain, ex = keys[k]
        factor = dividend_factor(cash[(chain, ex)], Decimal(prev_close))
        if factor is not None:
            out.append((chain, ex, prev_date, cash[(chain, ex)], Decimal(prev_close), factor))
    con.execute("DROP TABLE div_in")
    return sorted(out)


def dividend_table(rows: Sequence[tuple[str, date, date, Decimal, Decimal, Decimal]]) -> pa.Table:
    """`_price_dividends`'s rows as the Arrow table of the connection's `dividend` columns."""
    return pa.table(
        {
            "instrument_id": pa.array([r[0] for r in rows], pa.string()),
            "ex_date": pa.array([r[1] for r in rows], pa.date32()),
            "prev_date": pa.array([r[2] for r in rows], pa.date32()),
            "amount": pa.array([_places(r[3], 4) for r in rows], pa.decimal128(18, 4)),
            "prev_close": pa.array([_places(r[4], 4) for r in rows], pa.decimal128(18, 4)),
            "factor": pa.array(
                [_places(r[5], FACTOR_PLACES) for r in rows], pa.decimal128(20, FACTOR_PLACES)
            ),
        }
    )


def ex_prev_factor(
    prev_close: Decimal | None, previous_close: Decimal | None, day_factor: Decimal
) -> Decimal:
    """What an ex-session's raw prev_close takes on top of the session's own cumulative factor,
    for the price actions going ex on it (their factors' product `day_factor`): `day_factor` when
    NSE's PREVCLOSE is the previous session's raw close (nearly every ex-date: then it is on the
    old basis), 1 when NSE restated it to the new basis (nearer the previous close times
    `day_factor`, by ratio). Without a positive prev_close or previous close: `day_factor`."""
    if not prev_close or not previous_close or prev_close <= 0 or previous_close <= 0:
        return day_factor
    with localcontext() as ctx:
        ctx.prec = 40
        as_raw = abs((prev_close / previous_close).ln())
        as_restated = abs((prev_close / (previous_close * day_factor)).ln())
    return Decimal(1) if as_restated < as_raw else day_factor


def _ex_prev_factors(
    con: duckdb.DuckDBPyConnection, factors: Sequence[ActionFactor]
) -> dict[tuple[str, date], Decimal]:
    """(chain, ex-session) -> `ex_prev_factor` of the price actions going ex on that session,
    for each session where it is not 1."""
    per_day: dict[tuple[str, date], Decimal] = defaultdict(lambda: Decimal(1))
    for a in factors:
        per_day[(str(a.instrument_id), a.ex_date)] *= a.factor
    if not per_day:
        return {}
    keys = sorted(per_day)
    _load(
        con,
        "ex_in",
        pa.table(
            {
                "k": pa.array(range(len(keys)), pa.int32()),
                "chain": pa.array([c for c, _ in keys], pa.string()),
                "ex": pa.array([ex for _, ex in keys], pa.date32()),
            }
        ),
    )
    sessions: dict[tuple[str, date], list[Decimal]] = {}
    for k, session, prev_close, previous_close in con.execute(_EX_SESSION_SQL).fetchall():
        chain, ex = keys[k]
        held = sessions.setdefault((chain, session), [Decimal(1), prev_close, previous_close])
        held[0] *= per_day[(chain, ex)]  # actions whose ex-dates share a session go ex together
    con.execute("DROP TABLE ex_in")
    out = {}
    for key, (day_factor, prev_close, previous_close) in sessions.items():
        factor = ex_prev_factor(
            None if prev_close is None else Decimal(prev_close),
            None if previous_close is None else Decimal(previous_close),
            day_factor,
        )
        if factor != 1:
            out[key] = factor
    return out


def ex_day_table(factors: Mapping[tuple[str, date], Decimal]) -> pa.Table:
    """(chain, ex-session) -> factor as the Arrow table of the connection's `ex_cap` / `ex_tr`."""
    keys = sorted(factors)
    for key in keys:
        if not FACTOR_MIN <= factors[key] < FACTOR_MAX:
            raise StoreError("store_factor_range")  # would overflow the view's decimals
    return pa.table(
        {
            "instrument_id": pa.array([c for c, _ in keys], pa.string()),
            "ex_date": pa.array([ex for _, ex in keys], pa.date32()),
            "factor": pa.array(
                [_places(factors[k], FACTOR_PLACES) for k in keys], pa.decimal128(20, FACTOR_PLACES)
            ),
        }
    )


def _places(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN)


def _load(con: duckdb.DuckDBPyConnection, name: str, table: pa.Table) -> None:
    """A table `name` of the connection holding `table`'s rows (`name` is a constant)."""
    con.register("_load", table)
    con.execute(f"CREATE TABLE {name} AS SELECT * FROM _load")  # noqa: S608 - constant
    con.unregister("_load")


def latest_rows(
    frame: pl.DataFrame, key: Sequence[str] = ("instrument_id", "date")
) -> pl.DataFrame:
    """The latest ingest run's row per `key` (by default (`instrument_id`, `date`) of rows read
    from `bars_daily_adj`) by `ingest_run_id`, as the `bars_daily` view keeps, in the order
    read; done in polars, as a window over a slice in DuckDB costs several times the memory of
    the slice, and only when a day was written by more than one run, which is rare."""
    key = list(key)
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
