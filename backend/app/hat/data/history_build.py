"""The 2010-to-today historical build (HAT-DATA-090..097, HAT-DEC-I2).

    python -m app.hat.data.history_build --from 2010-01-01 [--to YYYY-MM-DD] [--resume]

Runs by hand, once, against the real NSE archive and the two MCP servers. It downloads about
4,150 bhavcopy zips at about one per second (roughly 70 minutes), keeps them untouched under
`HAT_DATA_ROOT/raw/`, writes `bars_daily_raw` partitions, loads corporate actions and index
history, then validates 50 random symbol-days against `get_stock_history`. `--resume` skips every
day already downloaded or known to have no file, so an interrupted run continues where it
stopped; every step is idempotent (the same input rewrites the same files). Nothing in the tests
or the verification touches the network: the sources below are injected.

Environment: `HAT_DATA_ROOT` (data directory); the NSE bhavcopy MCP endpoint variable of the ops
spec (corporate actions, index history and validation; without it those steps are skipped); a
PostgreSQL database as for the rest of HAT (`DB_*`). Add `--skip-actions`, `--skip-index` or
`--skip-validation` to leave a step out.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from ..ids import uuid7
from ..ops.jobs import JobError, JobResult
from ..settings import operator_settings, process_env
from .bhavcopy import (
    BhavRow,
    BuildState,
    DayReconciliation,
    Downloader,
    DownloadReport,
    Fetcher,
    ParsedDay,
    archive_name,
    parse_bhavcopy,
    reconcile_days,
    to_bar_rows,
    weekdays,
)
from .corporate_actions import ActionRecord, LoadReport, Normalised, load_actions
from .mcp_client import McpClient, McpError, parse_data_date
from .run_report import save_report
from .sessions import IST
from .store import (
    DATASET_BARS,
    DataStore,
    IndexRow,
    PartitionReport,
    index_coverage,
    partition_report,
)

logger = logging.getLogger(__name__)

INDEX_NAMES = ("NIFTY 50", "NIFTY 500", "NIFTY SMALLCAP 250", "INDIA VIX")
# [A] Each index's first day in the archive. Smallcap 250's start date is to be checked on the
# first run (HAT-DATA-095); until then every index is expected from the start of the build.
DEFAULT_INDEX_START = date(2010, 1, 1)
SAMPLE_SIZE = 50
MCP_HISTORY_MONTHS = 12
REPORT_DIR = Path("history_build")  # under the HAT data root: report-YYYY-MM-DD.txt per run day


# --- instruments ---------------------------------------------------------------------------------


class InstrumentCatalog(Protocol):
    """(isin, series) to instrument id. `register` adds securities that are seen in history but
    not in the master, so a delisted stock keeps its bars."""

    def lookup(self, isin: str, series: str) -> object | None: ...

    def register(self, rows: Sequence[BhavRow]) -> None: ...


class DictCatalog:
    """An in-memory catalog (fixtures, dry runs). With `auto` it mints an id for unknown keys."""

    def __init__(
        self, ids: dict[tuple[str, str], object] | None = None, *, auto: bool = False
    ) -> None:
        self.ids: dict[tuple[str, str], object] = dict(ids or {})
        self._auto = auto

    def lookup(self, isin: str, series: str) -> object | None:
        return self.ids.get((isin, series))

    def register(self, rows: Sequence[BhavRow]) -> None:
        if self._auto:
            for row in rows:
                self.ids.setdefault(row.key, uuid7())


class DbCatalog:
    """The catalog over `hat_instrument` (NSE). Unknown securities are registered inactive."""

    def __init__(self, session_factory: Callable, market_id: object) -> None:
        from ..models import HatInstrument

        self._factory = session_factory
        self._market_id = market_id
        with session_factory() as session:
            self._ids = {
                (r.isin, r.series): r.id
                for r in session.scalars(
                    select(HatInstrument).where(HatInstrument.market_id == market_id)
                )
            }

    def lookup(self, isin: str, series: str) -> object | None:
        return self._ids.get((isin, series))

    def register(self, rows: Sequence[BhavRow]) -> None:
        from .instruments import register_history_instrument

        with self._factory() as session:
            for row in rows:
                if (row.isin, row.series) in self._ids:
                    continue
                self._ids[row.key] = register_history_instrument(
                    session,
                    self._market_id,  # type: ignore[arg-type]
                    isin=row.isin,
                    series=row.series,
                    symbol=row.symbol,
                    first_seen=row.trade_date,
                )
            session.commit()


# --- index history (HAT-DATA-095) ----------------------------------------------------------------


@dataclass(frozen=True)
class IndexPoint:
    day: date
    row: IndexRow


IndexSource = Callable[[str, date, date], Iterable[IndexPoint]]


def _dec(raw: object) -> Decimal | None:
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def index_point_from_mcp(name: str, row: dict) -> IndexPoint | None:
    """One `get_index_history` row; None when a value or the date is missing."""
    day = parse_data_date(row.get("date"))
    values = [_dec(row.get(k)) for k in ("open", "high", "low", "close")]
    if day is None or any(v is None for v in values):
        return None
    o, h, low, c = values
    assert o is not None
    assert h is not None
    assert low is not None
    assert c is not None
    return IndexPoint(day, IndexRow(name.upper(), o, h, low, c))


def mcp_index_source(client: McpClient) -> IndexSource:
    """Chain `get_index_history` chunks (12 months each) back from `end` to `start`."""

    def source(name: str, start: date, end: date) -> Iterable[IndexPoint]:
        cursor: date | None = end
        while cursor is not None and cursor >= start:
            rows, nxt = client.index_history(name, cursor, MCP_HISTORY_MONTHS)
            for row in rows:
                point = index_point_from_mcp(name, row)
                if point is not None and start <= point.day <= end:
                    yield point
            if nxt is None or nxt >= cursor:
                break
            cursor = nxt

    return source


def parse_index_archive_csv(
    text: str, day: date, names: Collection[str] = INDEX_NAMES
) -> list[IndexPoint]:
    """NSE's `ind_close_all_DDMMYYYY.csv` rows for the wanted indices."""
    wanted = {n.upper() for n in names}
    points = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("index name", "").upper()
        if name not in wanted:
            continue
        values = [
            _dec(row.get(k))
            for k in (
                "open index value",
                "high index value",
                "low index value",
                "closing index value",
            )
        ]
        if any(v is None for v in values):
            continue
        o, h, low, c = values
        assert o is not None
        assert h is not None
        assert low is not None
        assert c is not None
        points.append(IndexPoint(day, IndexRow(name, o, h, low, c)))
    return points


@dataclass
class IndexLoadReport:
    days_written: int = 0
    points: int = 0


def load_index_history(
    store: DataStore,
    source: IndexSource,
    start: date,
    end: date,
    names: Sequence[str] = INDEX_NAMES,
) -> IndexLoadReport:
    """Pull every index, then write each day once with all its indices (idempotent)."""
    by_day: dict[date, dict[str, IndexRow]] = defaultdict(dict)
    for name in names:
        for point in source(name, start, end):
            by_day[point.day][point.row.index_name] = point.row
    report = IndexLoadReport()
    for day in sorted(by_day):
        store.upsert_index(day, list(by_day[day].values()))
        report.days_written += 1
        report.points += len(by_day[day])
    return report


# --- validation (HAT-DATA-096) --------------------------------------------------------------------

CloseSource = Callable[[str, date], Decimal | None]


def mcp_close_source(client: McpClient) -> CloseSource:
    """The close `get_stock_history` gives for a symbol on a day (raw, unadjusted)."""

    def close(symbol: str, day: date) -> Decimal | None:
        try:
            rows = client.stock_history(symbol, day, 1)
        except McpError:
            return None
        for row in rows:
            if parse_data_date(row.get("date")) == day:
                return _dec(row.get("close"))
        return None

    return close


@dataclass(frozen=True)
class SampleCheck:
    symbol: str
    day: date
    ours: Decimal
    theirs: Decimal | None

    @property
    def mismatch(self) -> bool:
        return self.theirs is not None and self.theirs != self.ours


@dataclass
class ValidationReport:
    row_counts: dict[date, int] = field(default_factory=dict)
    samples: list[SampleCheck] = field(default_factory=list)

    @property
    def mismatches(self) -> list[SampleCheck]:
        return [s for s in self.samples if s.mismatch]

    @property
    def unchecked(self) -> list[SampleCheck]:
        return [s for s in self.samples if s.theirs is None]

    @property
    def failed(self) -> bool:
        """True iff some sampled close differs from the MCP close (exact comparison)."""
        return bool(self.mismatches)


def validate_build(
    store: DataStore,
    close_source: CloseSource,
    *,
    sample_size: int = SAMPLE_SIZE,
    seed: int = 0,
) -> ValidationReport:
    """Row counts per day, and `sample_size` random symbol-days compared with the MCP."""
    report = ValidationReport()
    con = store.connect()
    try:
        report.row_counts = dict(
            con.execute(
                "SELECT date, count(*) FROM bars_daily GROUP BY date ORDER BY date"
            ).fetchall()
        )
        pool = con.execute(
            "SELECT r.symbol, r.date, r.close FROM bars_daily_raw r "
            "QUALIFY row_number() OVER (PARTITION BY instrument_id, date "
            "ORDER BY ingest_run_id DESC) = 1 ORDER BY r.date, r.symbol, r.instrument_id"
        ).fetchall()
    finally:
        con.close()

    # a reproducible sample: the rows with the smallest hash of (seed, symbol, day)
    def rank(row: tuple[str, date, Decimal]) -> str:
        return hashlib.sha256(f"{seed}:{row[0]}:{row[1]}".encode()).hexdigest()

    chosen = sorted(pool, key=rank)[:sample_size]
    for symbol, day, ours in sorted(chosen, key=lambda r: (r[1], r[0])):
        report.samples.append(SampleCheck(symbol, day, ours, close_source(symbol, day)))
    return report


# --- the build ---------------------------------------------------------------------------


@dataclass
class BuildReport:
    download: DownloadReport
    days: DayReconciliation
    partitions: PartitionReport
    rows_written: int = 0
    files_written: int = 0
    unresolved: set[tuple[str, str]] = field(default_factory=set)
    problems: dict[date, str] = field(default_factory=dict)  # day -> stable code
    left_out: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    actions: LoadReport | None = None
    action_symbols_failed: int = 0
    index: IndexLoadReport | None = None
    index_missing: dict[str, list[date]] = field(default_factory=dict)
    validation: ValidationReport | None = None


ActionLoader = Callable[[], Normalised]


@dataclass(frozen=True)
class Pacing:
    """Request spacing and the clock it is measured on (injected by tests)."""

    interval: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class ActionHooks:
    """Where corporate actions come from and where they are stored."""

    loader: Callable[[], tuple[Sequence[ActionRecord], int]]
    sink: Callable[[Sequence[ActionRecord]], LoadReport]


@dataclass(frozen=True)
class Validation:
    """The MCP close lookup and the size and seed of the sample compared with it."""

    close_source: CloseSource
    sample_size: int = SAMPLE_SIZE
    seed: int = 0


def run_history_build(
    *,
    store: DataStore,
    fetch: Fetcher,
    catalog: InstrumentCatalog,
    start: date,
    end: date,
    holidays: Collection[date] = (),
    resume: bool = True,
    pacing: Pacing | None = None,
    actions: ActionHooks | None = None,
    index_source: IndexSource | None = None,
    index_starts: dict[str, date] | None = None,
    validation: Validation | None = None,
) -> BuildReport:
    """Download, parse, write, load, validate. Safe to repeat: raw zips are never fetched twice,
    a day's file is rewritten from the same zip with the same run id, and actions and index rows
    are upserts."""
    days = weekdays(start, end)
    state = BuildState.load(store.root / "build" / "state.json")
    if state.run_id is None:
        state.run_id = str(uuid7())
        state.save()
    pacing = pacing or Pacing()
    downloader = Downloader(
        store, fetch, state, interval=pacing.interval, clock=pacing.clock, sleep=pacing.sleep
    )
    download = downloader.run(days, resume=resume)
    report = BuildReport(download, DayReconciliation(), PartitionReport())
    for day in days:
        _write_day(store, catalog, state.run_id, day, resume, report)
    have = set(store.days(DATASET_BARS))
    report.days = reconcile_days(days, have, state.candidates, holidays)
    # every weekday that is not an explained holiday should have rows, so gaps show as missing
    trading_days = [d for d in days if d not in report.days.holidays]
    report.partitions = partition_report(store, DATASET_BARS, trading_days)
    if actions is not None:
        records, failed = actions.loader()
        report.actions = actions.sink(records)
        report.action_symbols_failed = failed
    if index_source is not None:
        report.index = load_index_history(store, index_source, start, end)
        starts = index_starts or {n: max(start, DEFAULT_INDEX_START) for n in INDEX_NAMES}
        report.index_missing = index_coverage(store, trading_days, starts)
    if validation is not None:
        report.validation = validate_build(
            store, validation.close_source, sample_size=validation.sample_size, seed=validation.seed
        )
    return report


def _write_day(
    store: DataStore,
    catalog: InstrumentCatalog,
    run_id: str,
    day: date,
    resume: bool,
    report: BuildReport,
) -> None:
    """Parse one stored zip and write its bars as this run's partition file."""
    name = archive_name(day)
    if not store.raw_exists(name):
        return
    if resume and (store.partition_dir(DATASET_BARS, day) / f"part-{run_id}.parquet").exists():
        return
    try:
        parsed = parse_bhavcopy(day, store.read_raw(name))
    except Exception as exc:
        report.problems[day] = getattr(exc, "code", "bhavcopy_unreadable")
        return
    _record_left_out(report, parsed)
    unknown = [r for r in parsed.rows if catalog.lookup(r.isin, r.series) is None]
    if unknown:
        catalog.register(unknown)
    rows, unresolved = to_bar_rows(parsed, catalog.lookup, run_id)
    report.unresolved.update(unresolved)
    if not rows:
        report.problems[day] = "no_rows"
        return
    store.write_bars(day, rows)
    report.rows_written += len(rows)
    report.files_written += 1


def _record_left_out(report: BuildReport, parsed: ParsedDay) -> None:
    report.left_out["skipped_series"] += parsed.skipped_series
    report.left_out["invalid"] += parsed.invalid
    report.left_out["wrong_date"] += parsed.wrong_date
    report.left_out["duplicates"] += parsed.duplicates


def history_build_job(**kwargs: object) -> Callable[[], JobResult]:
    """The `history.build` body for `run_job`: the arguments of `run_history_build`. It fails with
    a stable code when the build left a problem, a mismatch or an unresolved security."""

    def body() -> JobResult:
        report = run_history_build(**kwargs)  # type: ignore[arg-type]
        if report.validation is not None and report.validation.failed:
            raise JobError("history_validation_failed")
        if report.unresolved or report.problems or report.days.gaps or report.days.unaccounted:
            raise JobError("history_incomplete")
        if report.action_symbols_failed:
            raise JobError("history_actions_incomplete")
        return JobResult(calls=len(report.download.requested), rows=report.rows_written)

    return body


# --- command line (the only code here that reaches the network) -------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.history_build", description=__doc__)
    p.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2010, 1, 1))
    p.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    p.add_argument("--resume", action="store_true", help="skip days already downloaded or empty")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between requests")
    p.add_argument("--seed", type=int, default=0, help="seed of the validation sample")
    p.add_argument(
        "--actions-archive", help="an NSE corporate-action CSV (SYMBOL, PURPOSE, EX-DATE)"
    )
    p.add_argument("--skip-actions", action="store_true")
    p.add_argument("--skip-index", action="store_true")
    p.add_argument("--skip-validation", action="store_true")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    end = args.end or date.today()
    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import SessionLocal

    from ..models import HatCorporateAction, HatInstrument, HatMarket  # noqa: F401
    from . import calendar as hat_calendar
    from .bhavcopy import http_fetcher
    from .mcp_client import client_from_env

    logging.basicConfig(level=logging.INFO)
    store = DataStore(settings.data_root)
    with SessionLocal() as session:
        market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
        if market_id is None:
            sys.stderr.write("history_no_market\n")
            return 2
        calendar = hat_calendar.load_calendar(session)
        holidays = calendar.holidays() if calendar else frozenset()
    client = client_from_env(process_env())

    def instruments() -> list[tuple[object, str]]:
        # read when the actions are loaded, so securities registered by the build are included
        with SessionLocal() as session:
            return [
                (r.id, r.symbol)
                for r in session.execute(select(HatInstrument.id, HatInstrument.symbol))
            ]

    def sink(records: Sequence[ActionRecord]) -> LoadReport:
        with SessionLocal() as session:
            result = load_actions(session, records)
            session.commit()
            return result

    hooks = None
    if not args.skip_actions and (client is not None or args.actions_archive):
        hooks = _action_hooks(client, instruments, args.actions_archive, args.start, end, sink)
    report = run_history_build(
        store=store,
        fetch=http_fetcher(),
        catalog=DbCatalog(SessionLocal, market_id),
        start=args.start,
        end=end,
        holidays=holidays,
        resume=args.resume,
        pacing=Pacing(interval=args.interval),
        actions=hooks,
        index_source=mcp_index_source(client)
        if client is not None and not args.skip_index
        else None,
        validation=Validation(mcp_close_source(client), seed=args.seed)
        if client is not None and not args.skip_validation
        else None,
    )
    summary = _summary(report)
    sys.stdout.write(summary)
    save_report(settings.data_root / REPORT_DIR, summary, datetime.now(IST))
    return _exit_code(report)


def _exit_code(report: BuildReport) -> int:
    """Non-zero when anything needs the operator: a mismatch, an unplaced security, an unusable
    file, a day with no file that is not a holiday, or an action lookup that failed."""
    failed = report.validation is not None and report.validation.failed
    incomplete = report.unresolved or report.problems or report.days.gaps or report.days.unaccounted
    return 1 if failed or incomplete or report.action_symbols_failed else 0


def _action_hooks(
    client: McpClient | None,
    instruments: Callable[[], list[tuple[object, str]]],
    archive: str | None,
    start: date,
    end: date,
    sink: Callable[[Sequence[ActionRecord]], LoadReport],
) -> ActionHooks:
    """Actions from the MCP (first) and an operator-supplied NSE archive CSV (fallback); the
    loader keeps the first value per key. A failed or partial (warned) MCP lookup is counted."""
    from . import corporate_actions as ca

    def loader() -> tuple[list[ActionRecord], int]:
        known = instruments()
        records: list[ActionRecord] = []
        failed = 0
        for instrument_id, symbol in known if client is not None else []:
            try:
                page = client.corporate_actions(symbol, start, end)
            except McpError:
                failed += 1
                continue
            failed += page.incomplete
            records.extend(ca.from_mcp(instrument_id, page.actions).records)  # type: ignore[arg-type]
        if archive:
            by_symbol = {symbol: instrument_id for instrument_id, symbol in known}
            text = Path(archive).read_text(encoding="utf-8")
            records.extend(ca.from_archive_csv(text, lambda sym, _d: by_symbol.get(sym)).records)
        return records, failed

    return ActionHooks(loader, sink)


def _summary(report: BuildReport) -> str:
    lines = [
        f"downloaded {len(report.download.downloaded)}, already had "
        f"{len(report.download.already_have)}, no file {len(report.download.candidates)}, "
        f"errors {len(report.download.errors)}",
        f"rows written {report.rows_written} in {report.files_written} files",
        f"holiday candidates explained {len(report.days.holidays)}, "
        f"gaps (not on the holiday list) {len(report.days.gaps)}",
        f"unaccounted days {len(report.days.unaccounted)}, "
        f"action lookups failed or partial {report.action_symbols_failed}",
        f"unresolved securities {len(report.unresolved)}, problem days {len(report.problems)}",
        f"left out {dict(report.left_out)}",
    ]
    if report.validation is not None:
        lines.append(
            f"validation: {len(report.validation.samples)} samples, "
            f"{len(report.validation.mismatches)} mismatches, "
            f"{len(report.validation.unchecked)} unchecked, row counts for "
            f"{len(report.validation.row_counts)} days"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
