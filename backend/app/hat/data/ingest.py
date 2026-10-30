"""The nightly ingest: probe, fetch, row checks, corporate-action flagging, raw write, index
snapshot and breadth (HAT-DATA-040..048, 050, 051; HAT-OPS-022).

`ingest_body` is the body of the `ingest.nightly` job for `run_job`. From 18:00 IST it probes
`get_bulk_quote(["RELIANCE"])` every 10 minutes until the returned date is the trade date, then
fetches the active EQ/BE universe in batches of 50, checks every row, flags the symbols whose
prices suggest a corporate action, looks those up (and only those) and writes the day's raw
partition once. At 22:00 without a matching probe it runs the bhavcopy fallback of PR 5 instead.
The index levels and the advance/decline record are stored on both paths; the levels are NSE's
daily index file of the session when it can be had, the MCP snapshot only when not (the file is
what NSE publishes: a snapshot's NIFTY 50 high of 2025-09-18 was 0.40 off it). A run that cannot
store the NIFTY 500 and India VIX levels does not succeed. Then the NIFTY 500 TRI close of the
session (and of any recent bar day still without one) is fetched from niftyindices; a day it
lacks is recorded in the report and left missing, never carried, and does not fail the run (only
the backtest benchmark reads the TRI).

The body is idempotent (a crash between the body and its recording re-runs it): a raw partition
that already exists is never rewritten, so a re-run reads it and carries on with the rest, and
every other write is an upsert. Outcome events and the data-health state are written by the
job wiring (`daily.nightly.ingest_outcome_recorder`) in the transaction that records the run's
final status (HAT-DATA-045), so ingest itself imports nothing from `risk` or `events`.

Nothing here opens a connection: the MCP client, the bhavcopy fetcher and the sleep are
injected. Stable error codes only ever leave this module (HAT-DATA-035).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

from ..clock import Clock
from ..ids import uuid7
from ..money import MONEY_SCALE, exact
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory, error_code
from ..time import IST
from . import corporate_actions as ca
from .bhavcopy import (
    SOURCE_BHAVCOPY,
    BuildState,
    Downloader,
    Fetcher,
    ParsedDay,
    archive_name,
    parse_bhavcopy,
)
from .breadth import breadth_from_mcp, derive_breadth, write_breadth
from .index_history import INDEX_NAMES, IndexSource, nightly_tri, parse_index_archive_csv
from .mcp_client import McpClient, McpError, parse_data_date
from .pacing import Pacer
from .rowcheck import Candidate, row_ok
from .store import DATASET_BARS, BarRow, DataStore, IndexRow, atomic_replace
from .universe import Member, active_members

logger = logging.getLogger(__name__)

JOB_INGEST = "ingest.nightly"
SOURCE_MCP = "mcp"
INDEX_FROM_CSV = "nse_index_csv"  # the levels came from NSE's daily index file

PROBE_START = clock_time(18, 0)
CUTOFF = clock_time(22, 0)
PROBE_INTERVAL = timedelta(minutes=10)
BIG_MOVE = Decimal("0.20")
REQUIRED_INDICES = ("NIFTY 500", "INDIA VIX")

# Stable codes (never raw text): what a failed run records and alerts with.
CODE_NO_DATA = "ingest_no_data"
CODE_FETCH_FAILED = "ingest_fetch_failed"
CODE_NO_UNIVERSE = "ingest_no_universe"
CODE_NO_ROWS = "ingest_no_rows"
CODE_INDEX_MISSING = "ingest_index_missing"

DayFallback = Callable[[date], "ParsedDay | None"]
IndexCsv = Callable[[date], "str | None"]
SymbolSource = Callable[[date], Collection[str]]


# --- the universe (HAT-DATA-041) --------------------------------------------------------------


def request_symbols(members: Iterable[Member]) -> list[str]:
    """The symbols one night's fetch asks for: every member's, each once (HAT-DATA-041)."""
    return sorted({m.symbol for m in members})


def by_symbol(members: Iterable[Member]) -> dict[str, Member]:
    """Symbol to member (an active symbol names one ISIN, so one member; the lowest id would win
    a tie)."""
    chosen: dict[str, Member] = {}
    for m in sorted(members, key=lambda m: str(m.instrument_id)):
        chosen.setdefault(m.symbol, m)
    return chosen


# --- row checks (HAT-DATA-042) ----------------------------------------------------------------


def _volume(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def parse_candidate(raw: Mapping[str, object]) -> Candidate:
    """A bulk-quote row as a `Candidate`; a field that cannot be read is None."""
    return Candidate(
        parse_data_date(raw.get("date")),
        exact(raw.get("open"), floats=True, places=MONEY_SCALE),
        exact(raw.get("high"), floats=True, places=MONEY_SCALE),
        exact(raw.get("low"), floats=True, places=MONEY_SCALE),
        exact(raw.get("close"), floats=True, places=MONEY_SCALE),
        exact(raw.get("prev_close"), floats=True, places=MONEY_SCALE),
        _volume(raw.get("volume")),
    )


def check_quote(raw: Mapping[str, object], trade_date: date) -> Candidate | None:
    """The accepted candidate for `raw`, or None when any row check fails."""
    candidate = parse_candidate(raw)
    return candidate if row_ok(candidate, trade_date) else None


# --- corporate-action flagging (HAT-DATA-050) -------------------------------------------------


def moved_over_limit(close: Decimal | None, prev_close: Decimal | None) -> bool:
    """True when `close` is more than 20% away from a usable `prev_close` (exact arithmetic)."""
    if close is None or prev_close is None or prev_close <= 0:
        return False
    return abs(close - prev_close) > BIG_MOVE * prev_close


def flag_symbol(
    stored_close: Decimal | None, prev_close: Decimal | None, close: Decimal, in_rss: bool
) -> bool:
    """A symbol is flagged iff the fetched `prev_close` differs from our stored close, or the day's
    move is over 20%, or it is in the corporate-action RSS. A missing value on one side of the
    comparison counts as different."""
    if in_rss or prev_close != stored_close:
        return True
    return (
        prev_close is not None
        and prev_close > 0
        and abs(close - prev_close) > BIG_MOVE * prev_close
    )


def previous_closes(store: DataStore, trade_date: date) -> dict[str, Decimal] | None:
    """Instrument id to the close of the latest stored day before `trade_date`; None when no
    earlier day is stored (nothing to compare with: the first night after a build)."""
    earlier = [d for d in store.days(DATASET_BARS) if d < trade_date]
    if not earlier:
        return None
    frame = store.read_bars(earlier[-1])
    return {
        r["instrument_id"]: r["close"] for r in frame.select("instrument_id", "close").to_dicts()
    }


def flagged_instruments(
    accepted: Mapping[str, Candidate],
    members: Mapping[str, Member],
    stored: Mapping[str, Decimal] | None,
    rss: Collection[str],
) -> list[str]:
    """The symbols to look corporate actions up for, sorted. With nothing stored to compare
    with, the `prev_close` test is skipped (the stored close is taken to be the fetched one)."""
    flagged = []
    for symbol, candidate in sorted(accepted.items()):
        member = members.get(symbol)
        if member is None or candidate.close is None:
            continue
        compare = candidate.prev_close if stored is None else stored.get(str(member.instrument_id))
        if flag_symbol(compare, candidate.prev_close, candidate.close, symbol in rss):
            flagged.append(symbol)
    return flagged


# --- the probe (HAT-DATA-040) -----------------------------------------------------------------


def _at(trade_date: date, at: clock_time) -> datetime:
    return datetime.combine(trade_date, at, tzinfo=IST)


@dataclass
class ProbeOutcome:
    ready: bool
    probes: list[datetime] = field(default_factory=list)  # when each probe started


def wait_for_data(
    probe: Callable[[], date | None],
    trade_date: date,
    clock: Clock,
    sleep: Callable[[float], None],
) -> ProbeOutcome:
    """Probe until the answer is `trade_date`, never before 18:00 and never at or after 22:00,
    each probe at least 10 minutes after the previous one started. The outcome is `ready` once
    a probe matched and not ready when 22:00 is reached first (the caller runs the fallback)."""
    cutoff = _at(trade_date, CUTOFF)
    earliest = _at(trade_date, PROBE_START)
    outcome = ProbeOutcome(False)
    while True:
        due = earliest if not outcome.probes else outcome.probes[-1] + PROBE_INTERVAL
        due = max(due, earliest)
        now = clock.now()
        if max(now, due) >= cutoff:
            return outcome
        if due > now:
            sleep((due - now).total_seconds())
        started = clock.now()
        if started >= cutoff:
            return outcome
        outcome.probes.append(started)
        if probe() == trade_date:
            outcome.ready = True
            return outcome


# --- the day's report -------------------------------------------------------------------------


@dataclass
class IngestReport:
    trade_date: date
    source: str = ""
    universe: int = 0
    requested: int = 0
    stored: int = 0
    violations: int = 0  # rows that failed the row checks, with rows for another date
    mismatched: int = 0  # rows the server returned for another date
    missing: int = 0  # requested symbols with no row
    flagged: int = 0
    lookups_failed: int = 0
    actions_created: int = 0
    probes: int = 0
    index_rows: int = 0
    index_source: str = ""  # which source supplied the levels: `INDEX_FROM_CSV` or `SOURCE_MCP`
    breadth_source: str = ""
    tri_stored: int = 0  # NIFTY 500 TRI closes written (the session's and earlier missing ones)
    tri_missing: list[date] = field(default_factory=list)  # bar days still without a TRI close
    tri_failed: str = ""  # the TRI source's stable code when it failed
    reused: bool = False  # the raw partition already existed (a re-run)

    def payload(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "source": self.source,
            "universe": self.universe,
            "requested": self.requested,
            "stored": self.stored,
            "violations": self.violations,
            "mismatched": self.mismatched,
            "missing": self.missing,
            "flagged": self.flagged,
            "lookups_failed": self.lookups_failed,
            "actions_created": self.actions_created,
            "probes": self.probes,
            "index_rows": self.index_rows,
            "index_source": self.index_source,
            "breadth_source": self.breadth_source,
            "tri_stored": self.tri_stored,
            "tri_missing": [d.isoformat() for d in self.tri_missing],
            "tri_failed": self.tri_failed,
        }


@dataclass
class IngestDeps:
    """Everything the body reaches outside itself, injected (tests give fakes)."""

    client: McpClient
    store: DataStore
    session_factory: SessionFactory
    clock: Clock
    sleep: Callable[[float], None] = time.sleep
    fallback: DayFallback | None = None
    index_csv: IndexCsv | None = None
    tri: IndexSource | None = None  # niftyindices' TRI series (`tri_source`); None: not fetched
    rss_symbols: SymbolSource = lambda _day: ()
    notifier: Notifier = field(default_factory=LogNotifier)


# --- writing ----------------------------------------------------------------------------------


def _bar(
    member: Member,
    c: Candidate,
    source: str,
    run_id: uuid.UUID,
    traded_value: Decimal | None = None,
    series: str | None = None,
) -> BarRow:
    """The member's bar; `series` is the bhavcopy row's own (the member's may be up to a week old,
    from before an EQ/BE move), else the member's."""
    assert c.open is not None
    assert c.high is not None
    assert c.low is not None
    assert c.close is not None
    assert c.volume is not None
    return BarRow(
        member.instrument_id,
        member.isin,
        member.symbol,
        series or member.series,
        c.open,
        c.high,
        c.low,
        c.close,
        c.prev_close,
        c.volume,
        traded_value,
        source,
        run_id,
    )


def write_raw_once(store: DataStore, day: date, rows: Sequence[BarRow]) -> bool:
    """Write the day's raw partition unless one exists; True when this call wrote it. A committed
    partition is never rewritten (HAT-DATA-043)."""
    if store.partition_files(DATASET_BARS, day) or not rows:
        return False
    store.write_bars(day, rows)
    return True


def index_rows_from_mcp(rows: Iterable[Mapping[str, object]]) -> list[IndexRow]:
    """`get_index_snapshot` rows as `IndexRow`s (names upper-cased as the history stores them);
    a row with an unreadable value is left out, and the first row of a repeated name wins."""
    found: dict[str, IndexRow] = {}
    for raw in rows:
        name = raw.get("indexName")
        values = [
            exact(raw.get(k), floats=True, places=MONEY_SCALE)
            for k in ("open", "high", "low", "close")
        ]
        if not isinstance(name, str) or not name.strip() or any(v is None for v in values):
            continue
        o, h, low, c = values
        assert o is not None
        assert h is not None
        assert low is not None
        assert c is not None
        found.setdefault(name.strip().upper(), IndexRow(name.strip().upper(), o, h, low, c))
    return list(found.values())


def has_required_indices(names: Iterable[str]) -> bool:
    return set(REQUIRED_INDICES) <= {n.upper() for n in names}


# --- the body ---------------------------------------------------------------------------------


def bhavcopy_day(store: DataStore, fetch: Fetcher, trade_date: date) -> ParsedDay | None:
    """Download (once, kept untouched in `raw/`) and parse the day's bhavcopy; None when NSE has
    no file or it cannot be read. This is the PR 5 downloader and parser, unchanged."""
    state = BuildState(store.root / "build" / "ingest_state.json")
    Downloader(store, fetch, state, Pacer()).run([trade_date], resume=False)
    name = archive_name(trade_date)
    if not store.raw_exists(name):
        return None
    try:
        return parse_bhavcopy(trade_date, store.read_raw(name))
    except Exception:  # an unreadable file is "no data"; its text is not kept
        logger.warning("bhavcopy unreadable", extra={"trade_date": trade_date.isoformat()})
        return None


@dataclass
class BhavCandidates:
    """Accepted candidates by symbol, with each row's traded value and own series, and how many
    rows were violations (the parser already left out every row that fails `row_ok`, counted
    as `invalid`)."""

    accepted: dict[str, Candidate] = field(default_factory=dict)
    traded: dict[str, Decimal | None] = field(default_factory=dict)
    series: dict[str, str] = field(default_factory=dict)
    violations: int = 0


def _bhav_candidates(parsed: ParsedDay, members: Iterable[Member]) -> BhavCandidates:
    """The day's rows keyed to the members by ISIN, each carrying the series its row names (as
    the history build writes it), not the member's from the last weekly refresh."""
    keyed = {m.isin: m for m in members}
    out = BhavCandidates(violations=parsed.invalid + parsed.wrong_date)
    for row in parsed.rows:
        # by ISIN: a stock that moved series since the weekly refresh keeps its bar (audit D5)
        member = keyed.get(row.isin)
        if member is None or member.symbol in out.accepted:
            continue
        out.accepted[member.symbol] = Candidate(
            row.trade_date, row.open, row.high, row.low, row.close, row.prev_close, row.volume
        )
        out.traded[member.symbol] = row.traded_value
        out.series[member.symbol] = row.series
    return out


def _lookup_actions(
    deps: IngestDeps, trade_date: date, flagged: Sequence[str], members: Mapping[str, Member]
) -> tuple[int, int]:
    """`get_corporate_actions` for the flagged symbols only; (created, failed). A lookup that
    failed and an action whose factor conflicts with the stored one both count as failed (D220:
    the stored factor stays and the conflict is alerted)."""
    found = ca.lookup_actions(
        deps.client, {s: members[s].instrument_id for s in flagged}, trade_date, trade_date
    )
    stored = ca.store_actions(deps.session_factory, found.records, (deps.notifier, trade_date))
    return stored.created, len(found.failed) + len(stored.conflicts)


def _store_indices(deps: IngestDeps, trade_date: date, report: IngestReport) -> None:
    """Store the day's index levels, NSE's file first (only its rows dated the session:
    `index_history.dated_on`); the run fails when NIFTY 500 or India VIX is missing. The report
    names the source that supplied them (`index_source`)."""

    def from_csv() -> list[IndexRow]:
        text = deps.index_csv(trade_date) if deps.index_csv is not None else None
        if not text:
            return []
        return [p.row for p in parse_index_archive_csv(text, trade_date, INDEX_NAMES)]

    def from_mcp() -> list[IndexRow]:
        try:
            snap = deps.client.index_snapshot(trade_date)
        except McpError:
            return []
        return index_rows_from_mcp(snap.rows) if snap.data_date == trade_date else []

    for name, source in ((INDEX_FROM_CSV, from_csv), (SOURCE_MCP, from_mcp)):
        rows = source()
        if rows and has_required_indices(r.index_name for r in rows):
            deps.store.upsert_index(trade_date, rows)
            report.index_rows = len(rows)
            report.index_source = name
            return
    raise JobError(CODE_INDEX_MISSING)


def _store_breadth(deps: IngestDeps, trade_date: date, report: IngestReport) -> None:
    row = None
    try:
        snap = deps.client.market_breadth(trade_date)
        if snap.data_date == trade_date:
            row = breadth_from_mcp(snap.fields)
    except McpError:
        row = None
    if row is None:
        frame = deps.store.read_bars(trade_date).select("prev_close", "close", "volume")
        row = derive_breadth((r["prev_close"], r["close"], r["volume"]) for r in frame.to_dicts())
    write_breadth(deps.store, trade_date, row)
    report.breadth_source = row["source"]


def _store_tri(deps: IngestDeps, trade_date: date, report: IngestReport) -> None:
    """The TRI step (`nightly_tri`): never fails the night; what it could not store is reported."""
    if deps.tri is None:
        return
    tri = nightly_tri(deps.store, deps.tri, trade_date, apply=True)
    report.tri_stored = sum(len(points) for points in tri.found.values())
    report.tri_missing = [d for days in tri.left.values() for d in days]
    report.tri_failed = next(iter(tri.failed.values()), "")
    if report.tri_missing:
        logger.warning("NIFTY 500 TRI missing for %s", report.tri_missing)


def run_ingest(deps: IngestDeps, trade_date: date, run_id: uuid.UUID) -> IngestReport:
    """One night's ingest. Raises `JobError` with a stable code when the day cannot be completed."""
    report = IngestReport(trade_date)
    with deps.session_factory() as session:
        members = active_members(session)
    if not members:
        raise JobError(CODE_NO_UNIVERSE)
    report.universe = len(members)
    symbols = request_symbols(members)
    report.requested = len(symbols)
    if deps.store.partition_files(DATASET_BARS, trade_date):
        _reuse_partition(deps, trade_date, report)
    else:
        _fetch_and_write(deps, trade_date, members, symbols, run_id, report)
    _store_indices(deps, trade_date, report)
    _store_breadth(deps, trade_date, report)
    _store_tri(deps, trade_date, report)
    return report


def _reuse_partition(deps: IngestDeps, trade_date: date, report: IngestReport) -> None:
    """A retry after the day's raw partition was written: report what it holds."""
    report.reused = True
    bars = deps.store.read_bars(trade_date)
    report.source = str(bars["source"][0])
    report.stored = bars.height
    _restore_checks(deps.store, trade_date, report)


def _fetch_and_write(
    deps: IngestDeps,
    trade_date: date,
    members: list[Member],
    symbols: Sequence[str],
    run_id: uuid.UUID,
    report: IngestReport,
) -> None:
    """Fetch the day (MCP, else the bhavcopy fallback), look up flagged actions and write the raw
    partition, its row-check counts first."""
    by_sym = by_symbol(members)
    outcome = wait_for_data(lambda: _probe(deps, trade_date), trade_date, deps.clock, deps.sleep)
    report.probes = len(outcome.probes)
    bhav = BhavCandidates()
    if outcome.ready:
        report.source = SOURCE_MCP
        accepted = _fetch_mcp(deps, trade_date, symbols, report)
    else:
        report.source = SOURCE_BHAVCOPY
        parsed = deps.fallback(trade_date) if deps.fallback is not None else None
        if parsed is None or not parsed.rows:
            raise JobError(CODE_NO_DATA)
        bhav = _bhav_candidates(parsed, members)
        accepted, report.violations = bhav.accepted, bhav.violations
    if not accepted:
        raise JobError(CODE_NO_ROWS)
    report.stored = len(accepted)
    stored = previous_closes(deps.store, trade_date)
    flagged = flagged_instruments(accepted, by_sym, stored, deps.rss_symbols(trade_date))
    report.flagged = len(flagged)
    report.actions_created, report.lookups_failed = _lookup_actions(
        deps, trade_date, flagged, by_sym
    )
    rows = [
        _bar(by_sym[s], c, report.source, run_id, bhav.traded.get(s), bhav.series.get(s))
        for s, c in sorted(accepted.items())
        if s in by_sym
    ]
    if rows and not deps.store.partition_files(DATASET_BARS, trade_date):
        _keep_checks(deps.store, trade_date, report)  # first: ingest's partition has its counts
    write_raw_once(deps.store, trade_date, rows)


# The row-check counts of the run that wrote a day's raw partition. A retry that finds the
# partition (a run that failed later, on the indices say) reads them back, so the quality gate
# of the retry still sees the rows the first run dropped (PR 17).
CHECKS_FILE = "ingest-checks.json"
_CHECK_COUNTS = ("violations", "mismatched", "missing")


def _keep_checks(store: DataStore, trade_date: date, report: IngestReport) -> None:
    """Written before the partition and atomically (a temporary file renamed over), so a crash
    leaves either no partition or a partition with its counts."""
    folder = store.partition_dir(DATASET_BARS, trade_date)
    text = json.dumps({name: getattr(report, name) for name in _CHECK_COUNTS}, sort_keys=True)
    atomic_replace(folder / CHECKS_FILE, lambda tmp: tmp.write_text(text, encoding="utf-8"))


def _restore_checks(store: DataStore, trade_date: date, report: IngestReport) -> None:
    path = store.partition_dir(DATASET_BARS, trade_date) / CHECKS_FILE
    try:
        counts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Ingest writes the counts before its partition, so the partition came from another
        # writer (the history build): its rows were not checked here and the counts stay 0.
        logger.warning("ingest checks absent for %s; counts taken as 0", trade_date)
        return
    for name in _CHECK_COUNTS:
        value = counts.get(name) if isinstance(counts, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(report, name, value)


def _probe(deps: IngestDeps, trade_date: date) -> date | None:
    try:
        return deps.client.probe(trade_date)
    except McpError:
        return None  # a failed probe is "not yet": the next one is 10 minutes away


def _fetch_mcp(
    deps: IngestDeps, trade_date: date, symbols: Sequence[str], report: IngestReport
) -> dict[str, Candidate]:
    try:
        batch = deps.client.bulk_quotes(symbols, trade_date)
    except McpError as exc:
        raise JobError(CODE_FETCH_FAILED) from exc
    report.mismatched = len(batch.mismatched)
    report.missing = len(batch.missing)
    accepted: dict[str, Candidate] = {}
    violations = len(batch.mismatched)
    for raw in batch.rows:
        candidate = check_quote(raw, trade_date)
        if candidate is None:
            violations += 1
        else:
            accepted.setdefault(str(raw["symbol"]), candidate)
    report.violations = violations
    return accepted


def ingest_body(
    deps: IngestDeps, trade_date: date, holder: dict[str, IngestReport]
) -> Callable[[], JobResult]:
    """The `ingest.nightly` body for `run_job`. `holder["report"]` receives the report for the
    outcome recorder. A failure alerts once with its stable code, then re-raises."""

    def body() -> JobResult:
        run_id = uuid7()
        try:
            report = run_ingest(deps, trade_date, run_id)
        except Exception as exc:
            deps.notifier.alert(error_code(exc), trade_date, {})
            raise
        holder["report"] = report
        return JobResult(calls=deps.client.log.calls, rows=report.stored)

    return body
