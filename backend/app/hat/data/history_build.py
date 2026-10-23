"""The historical build from 22 Jun 2011 to today (HAT-DATA-090..097, HAT-DEC-I2).

`run_history_build` downloads every NSE bhavcopy zip of the period (about 4,000 at about one per
second), keeps them untouched under `HAT_DATA_ROOT/raw/`, writes `bars_daily_raw` partitions (a
row failing the HAT-DATA-042 row check is left out and counted), then runs the steps it is given:
corporate actions, index history (`index_history`) and validation (`history_validation`). The
history starts at `bhavcopy.FIRST_ISIN_DAY`: older files have no ISIN, so an earlier start is
moved up to it. With `resume` every day already downloaded or known to have no file is skipped,
so an interrupted run continues where it stopped; every step is idempotent (the same input
rewrites the same files). The command line is `history_cli`; nothing here reaches the network:
the sources are injected.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sqlalchemy import select

from ..ids import uuid7
from .bhavcopy import (
    FIRST_ISIN_DAY,
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
from .corporate_actions import ActionRecord, LoadReport
from .history_validation import Validation, ValidationReport, validate_build
from .index_history import INDEX_NAMES, IndexLoadReport, IndexSource, load_index_history
from .pacing import Pacer
from .store import DATASET_BARS, DataStore, PartitionReport, index_coverage, partition_report

# --- instruments ---------------------------------------------------------------------------------


class InstrumentCatalog(Protocol):
    """ISIN to instrument id, whatever the series (audit D5). `register` adds securities that are
    seen in history but not in the master, so a delisted stock keeps its bars."""

    def lookup(self, isin: str) -> object | None: ...

    def register(self, rows: Sequence[BhavRow]) -> None: ...


class DbCatalog:
    """The catalog over `hat_instrument` (NSE). Unknown securities are registered inactive."""

    def __init__(self, session_factory: Callable, market_id: object) -> None:
        from ..models import HatInstrument
        from .instruments import by_isin

        self._factory = session_factory
        self._market_id = market_id
        with session_factory() as session:
            self._ids = {
                isin: r.id
                for isin, r in by_isin(
                    session.scalars(
                        select(HatInstrument).where(HatInstrument.market_id == market_id)
                    )
                ).items()
            }

    def lookup(self, isin: str) -> object | None:
        return self._ids.get(isin)

    def register(self, rows: Sequence[BhavRow]) -> None:
        from .instruments import register_history_instrument

        with self._factory() as session:
            for row in rows:
                if row.isin in self._ids:
                    continue
                self._ids[row.isin] = register_history_instrument(
                    session,
                    self._market_id,  # type: ignore[arg-type]
                    isin=row.isin,
                    series=row.series,
                    symbol=row.symbol,
                    first_seen=row.trade_date,
                )
            session.commit()


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
    actions_unparsed: list[str] = field(default_factory=list)  # archive texts, HAT-DATA-055
    index: IndexLoadReport | None = None
    index_missing: dict[str, list[date]] = field(default_factory=dict)
    validation: ValidationReport | None = None


@dataclass(frozen=True)
class ActionHooks:
    """Where corporate actions come from and where they are stored. The loader returns the
    records, the failed or partial lookups and the archive texts it could not parse."""

    loader: Callable[[], tuple[Sequence[ActionRecord], int, Sequence[str]]]
    sink: Callable[[Sequence[ActionRecord]], LoadReport]


@dataclass(frozen=True)
class BuildPlan:
    """What the build covers: the store, the bhavcopy fetcher, the instrument catalog, the
    period, the explained holidays, and whether days already done are skipped."""

    store: DataStore
    fetch: Fetcher
    catalog: InstrumentCatalog
    start: date
    end: date
    holidays: Collection[date] = ()
    resume: bool = True


@dataclass(frozen=True)
class BuildSteps:
    """The pacer of the NSE archive requests (by default the downloader's one a second) and the
    optional steps after the bars: corporate actions, index history (with each index's first
    expected day) and validation; a step left None is skipped."""

    pacer: Pacer | None = None
    actions: ActionHooks | None = None
    index_source: IndexSource | None = None
    index_starts: dict[str, date] | None = None
    validation: Validation | None = None


def run_history_build(plan: BuildPlan, steps: BuildSteps | None = None) -> BuildReport:
    """Download, parse, write, load, validate. Safe to repeat: raw zips are never fetched twice,
    a day's file is rewritten from the same zip with the same run id, and actions and index rows
    are upserts. A `start` before `FIRST_ISIN_DAY` is moved up to it (no older file is usable)."""
    steps = steps or BuildSteps()
    store = plan.store
    start = max(plan.start, FIRST_ISIN_DAY)
    days = weekdays(start, plan.end)
    state = BuildState.load(store.root / "build" / "state.json")
    if state.run_id is None:
        state.run_id = str(uuid7())
        state.save()
    download = Downloader(store, plan.fetch, state, steps.pacer).run(days, resume=plan.resume)
    report = BuildReport(download, DayReconciliation(), PartitionReport())
    for day in days:
        _write_day(store, plan.catalog, state.run_id, day, plan.resume, report)
    have = set(store.days(DATASET_BARS))
    report.days = reconcile_days(days, have, state.candidates, plan.holidays)
    # every weekday that is not an explained holiday should have rows, so gaps show as missing
    trading_days = [d for d in days if d not in report.days.holidays]
    report.partitions = partition_report(store, DATASET_BARS, trading_days)
    if steps.actions is not None:
        records, failed, unparsed = steps.actions.loader()
        report.actions = steps.actions.sink(records)
        report.action_symbols_failed = failed
        report.actions_unparsed = list(unparsed)
    if steps.index_source is not None:
        report.index = load_index_history(store, steps.index_source, start, plan.end)
        # [A] every index is expected from the start of the build until Smallcap 250's own first
        # day is checked (HAT-DATA-095)
        starts = steps.index_starts or dict.fromkeys(INDEX_NAMES, start)
        report.index_missing = index_coverage(store, trading_days, starts)
    if steps.validation is not None:
        v = steps.validation
        report.validation = validate_build(
            store, v.close_source, sample_size=v.sample_size, seed=v.seed
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
    unknown = [r for r in parsed.rows if catalog.lookup(r.isin) is None]
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
    report.left_out["other_series"] += parsed.other_series
