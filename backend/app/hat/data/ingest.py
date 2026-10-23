"""The nightly ingest: probe, fetch, row checks, corporate-action flagging, raw write, index
snapshot and breadth (HAT-DATA-040..048, 050, 051; HAT-OPS-022).

`ingest_body` is the body of the `ingest.nightly` job for `run_job`. From 18:00 IST it probes
`get_bulk_quote(["RELIANCE"])` every 10 minutes until the returned date is the trade date, then
fetches the active EQ/BE universe in batches of 50, checks every row, flags the symbols whose
prices suggest a corporate action, looks those up (and only those) and writes the day's raw
partition once. At 22:00 without a matching probe it runs the bhavcopy fallback of PR 5 instead.
The index snapshot and the advance/decline record are stored on both paths; a run that cannot
store the NIFTY 500 and India VIX levels does not succeed.

The body is idempotent (a crash between the body and its recording re-runs it): a raw partition
that already exists is never rewritten, so a re-run reads it and carries on with the rest, and
every other write is an upsert. Outcome events and the data-health state are written by
`outcome_recorder` in the transaction that records the run's final status (HAT-DATA-045).

Nothing here opens a connection: the MCP client, the bhavcopy fetcher and the sleep are
injected. Stable error codes only ever leave this module (HAT-DATA-035).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Any

import pyarrow as pa
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..events import record
from ..ids import uuid7
from ..models import HatInstrument, HatJobRun, HatMarket
from ..money import MONEY_SCALE, exact
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobError, JobResult, OnFinish, SessionFactory, error_code
from ..risk.controls import sync_health
from ..time import IST, utc_now
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
from .history_build import INDEX_NAMES, parse_index_archive_csv
from .mcp_client import McpClient, McpError, parse_data_date
from .rowcheck import Candidate, row_ok
from .store import (
    DATASET_BARS,
    DATASET_MARKET_BREADTH,
    MARKET_BREADTH_SCHEMA,
    BarRow,
    DataStore,
    IndexRow,
)

logger = logging.getLogger(__name__)

JOB_INGEST = "ingest.nightly"
SOURCE_MCP = "mcp"
SOURCE_DERIVED = "derived"  # breadth counted from our own bars when the MCP has none

PROBE_START = clock_time(18, 0)
CUTOFF = clock_time(22, 0)
PROBE_INTERVAL = timedelta(minutes=10)
UNIVERSE_SERIES = ("EQ", "BE")
BIG_MOVE = Decimal("0.20")
REQUIRED_INDICES = ("NIFTY 500", "INDIA VIX")
BREADTH_PART = "breadth"

# Stable codes (never raw text): what a failed run records and alerts with.
CODE_NO_DATA = "ingest_no_data"
CODE_FETCH_FAILED = "ingest_fetch_failed"
CODE_NO_UNIVERSE = "ingest_no_universe"
CODE_NO_ROWS = "ingest_no_rows"
CODE_INDEX_MISSING = "ingest_index_missing"
CODE_NO_MARKET = "ingest_no_market"

DayFallback = Callable[[date], "ParsedDay | None"]
IndexCsv = Callable[[date], "str | None"]
SymbolSource = Callable[[date], Collection[str]]


# --- the universe (HAT-DATA-041) --------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """One active EQ/BE instrument of the universe."""

    instrument_id: uuid.UUID
    isin: str
    symbol: str
    series: str


def active_universe(session: Session) -> list[Member]:
    """The active NSE instruments with series EQ or BE, in a stable order."""
    market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
    if market_id is None:
        raise JobError(CODE_NO_MARKET)
    rows = session.execute(
        select(HatInstrument.id, HatInstrument.isin, HatInstrument.symbol, HatInstrument.series)
        .where(
            HatInstrument.market_id == market_id,
            HatInstrument.active.is_(True),
            HatInstrument.series.in_(UNIVERSE_SERIES),
        )
        .order_by(HatInstrument.symbol, HatInstrument.series, HatInstrument.id)
    ).all()
    return [Member(r[0], r[1], r[2], r[3]) for r in rows]


def request_symbols(members: Iterable[Member]) -> list[str]:
    """The symbols one night's fetch asks for: every member's, each once (HAT-DATA-041)."""
    return sorted({m.symbol for m in members})


def by_symbol(members: Iterable[Member]) -> dict[str, Member]:
    """Symbol to member; EQ wins over BE if two active members share a symbol."""
    chosen: dict[str, Member] = {}
    for m in sorted(members, key=lambda m: (m.series != "EQ", str(m.instrument_id))):
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
    breadth_source: str = ""
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
            "breadth_source": self.breadth_source,
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
    rss_symbols: SymbolSource = lambda _day: ()
    notifier: Notifier = field(default_factory=LogNotifier)


# --- writing ----------------------------------------------------------------------------------


def _bar(
    member: Member,
    c: Candidate,
    source: str,
    run_id: uuid.UUID,
    traded_value: Decimal | None = None,
) -> BarRow:
    assert c.open is not None
    assert c.high is not None
    assert c.low is not None
    assert c.close is not None
    assert c.volume is not None
    return BarRow(
        member.instrument_id,
        member.isin,
        member.symbol,
        member.series,
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


def derive_breadth(frame_rows: Iterable[tuple[Decimal | None, Decimal, int]]) -> dict[str, Any]:
    """Advances, declines and unchanged counted from (prev_close, close, volume) rows."""
    advances = declines = unchanged = volume = 0
    for prev, close, vol in frame_rows:
        volume += vol
        if prev is None or close == prev:
            unchanged += 1
        elif close > prev:
            advances += 1
        else:
            declines += 1
    total = advances + declines + unchanged
    return {
        "total_stocks": total,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "ad_ratio": (advances / declines) if declines else None,
        "volume": volume,
        "source": SOURCE_DERIVED,
    }


def _count(fields: Mapping[str, object], *names: str) -> int | None:
    for name in names:
        value = fields.get(name)
        if isinstance(value, bool):
            continue
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def breadth_from_mcp(fields: Mapping[str, object]) -> dict[str, Any] | None:
    """The `get_market_breadth` fields as a row, or None when a count is missing."""
    advances = _count(fields, "advances")
    declines = _count(fields, "declines")
    unchanged = _count(fields, "unchanged")
    if advances is None or declines is None or unchanged is None:
        return None
    total = _count(fields, "totalStocks", "total_stocks", "total")
    ratio = fields.get("adRatio", fields.get("ad_ratio"))
    if isinstance(ratio, int | float) and not isinstance(ratio, bool):
        ad_ratio: float | None = float(ratio)
    else:
        ad_ratio = advances / declines if declines else None
    return {
        "total_stocks": total if total is not None else advances + declines + unchanged,
        "advances": advances,
        "declines": declines,
        "unchanged": unchanged,
        "ad_ratio": ad_ratio,
        "volume": _count(fields, "total_volume", "totalVolume", "volume") or 0,
        "source": SOURCE_MCP,
    }


def write_breadth(store: DataStore, day: date, row: Mapping[str, Any]) -> None:
    table = pa.table(
        {k: [row[k]] for k in MARKET_BREADTH_SCHEMA.names}, schema=MARKET_BREADTH_SCHEMA
    )
    store.write_partition(DATASET_MARKET_BREADTH, day, BREADTH_PART, table)


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
    Downloader(store, fetch, state, interval=0.0).run([trade_date], resume=False)
    name = archive_name(trade_date)
    if not store.raw_exists(name):
        return None
    try:
        return parse_bhavcopy(trade_date, store.read_raw(name))
    except Exception:  # an unreadable file is "no data"; its text is not kept
        logger.warning("bhavcopy unreadable", extra={"trade_date": trade_date.isoformat()})
        return None


def _bhav_candidates(
    parsed: ParsedDay, members: Iterable[Member]
) -> tuple[dict[str, Candidate], dict[str, Decimal | None], int]:
    """Accepted candidates by symbol, their traded values, and how many rows were violations (the
    parser already left out every row that fails `row_ok`, counted as `invalid`)."""
    keyed = {(m.isin, m.series): m for m in members}
    accepted: dict[str, Candidate] = {}
    traded: dict[str, Decimal | None] = {}
    violations = parsed.invalid + parsed.wrong_date
    for row in parsed.rows:
        member = keyed.get((row.isin, row.series))
        if member is None:
            continue
        accepted.setdefault(
            member.symbol,
            Candidate(
                row.trade_date, row.open, row.high, row.low, row.close, row.prev_close, row.volume
            ),
        )
        traded.setdefault(member.symbol, row.traded_value)
    return accepted, traded, violations


def _lookup_actions(
    deps: IngestDeps, trade_date: date, flagged: Sequence[str], members: Mapping[str, Member]
) -> tuple[int, int]:
    """`get_corporate_actions` for the flagged symbols only; (created, failed lookups)."""
    created, failed = lookup_actions(
        deps.client,
        deps.session_factory,
        {s: members[s].instrument_id for s in flagged},
        trade_date,
        trade_date,
    )
    return created, len(failed)


def lookup_actions(
    client: McpClient,
    session_factory: SessionFactory,
    instruments: Mapping[str, object],
    from_date: date,
    to_date: date,
) -> tuple[int, list[str]]:
    """`get_corporate_actions` for each symbol (to its instrument id) over the dates, stored
    idempotently (HAT-DATA-052). Returns (actions created, symbols whose lookup failed, came
    back incomplete or held a row that could not be read: an unknown action is not "none")."""

    def one(symbol: str) -> Callable[[], tuple[str, ca.Normalised | None]]:
        def run() -> tuple[str, ca.Normalised | None]:
            try:
                page = client.corporate_actions(symbol, from_date, to_date)
            except McpError:
                return symbol, None
            if page.incomplete:
                return symbol, None
            return symbol, ca.from_mcp(instruments[symbol], page.actions)

        return run

    results = client.map_calls([one(s) for s in sorted(instruments)])
    records = [r for _, n in results if n is not None for r in n.records]
    failed = sorted(s for s, n in results if n is None or n.unparsed)
    created = 0
    if records:
        with session_factory() as session:
            created = ca.load_actions(session, records).created
            session.commit()
    return created, failed


def _store_indices(
    deps: IngestDeps, trade_date: date, report: IngestReport, *, csv_first: bool
) -> None:
    """Store the day's index levels; the run fails when NIFTY 500 or India VIX is missing."""

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

    sources = (from_csv, from_mcp) if csv_first else (from_mcp, from_csv)
    for source in sources:
        rows = source()
        if rows and has_required_indices(r.index_name for r in rows):
            deps.store.upsert_index(trade_date, rows)
            report.index_rows = len(rows)
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


def run_ingest(deps: IngestDeps, trade_date: date, run_id: uuid.UUID) -> IngestReport:
    """One night's ingest. Raises `JobError` with a stable code when the day cannot be completed."""
    report = IngestReport(trade_date)
    with deps.session_factory() as session:
        members = active_universe(session)
    if not members:
        raise JobError(CODE_NO_UNIVERSE)
    report.universe = len(members)
    symbols = request_symbols(members)
    report.requested = len(symbols)
    csv_first = False
    if deps.store.partition_files(DATASET_BARS, trade_date):
        _reuse_partition(deps, trade_date, report)
    else:
        csv_first = _fetch_and_write(deps, trade_date, members, symbols, run_id, report)
    _store_indices(deps, trade_date, report, csv_first=csv_first)
    _store_breadth(deps, trade_date, report)
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
) -> bool:
    """Fetch the day (MCP, else the bhavcopy fallback), look up flagged actions and write the raw
    partition, its row-check counts first. True when the rows came from the bhavcopy CSV."""
    by_sym = by_symbol(members)
    outcome = wait_for_data(lambda: _probe(deps, trade_date), trade_date, deps.clock, deps.sleep)
    report.probes = len(outcome.probes)
    traded: dict[str, Decimal | None] = {}
    if outcome.ready:
        report.source = SOURCE_MCP
        accepted = _fetch_mcp(deps, trade_date, symbols, report)
    else:
        report.source = SOURCE_BHAVCOPY
        parsed = deps.fallback(trade_date) if deps.fallback is not None else None
        if parsed is None or not parsed.rows:
            raise JobError(CODE_NO_DATA)
        accepted, traded, report.violations = _bhav_candidates(parsed, members)
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
        _bar(by_sym[s], c, report.source, run_id, traded.get(s))
        for s, c in sorted(accepted.items())
        if s in by_sym
    ]
    if rows and not deps.store.partition_files(DATASET_BARS, trade_date):
        _keep_checks(deps.store, trade_date, report)  # first: ingest's partition has its counts
    write_raw_once(deps.store, trade_date, rows)
    return not outcome.ready


# The row-check counts of the run that wrote a day's raw partition. A retry that finds the
# partition (a run that failed later, on the indices say) reads them back, so the quality gate
# of the retry still sees the rows the first run dropped (PR 17).
CHECKS_FILE = "ingest-checks.json"
_CHECK_COUNTS = ("violations", "mismatched", "missing")


def _keep_checks(store: DataStore, trade_date: date, report: IngestReport) -> None:
    """Written before the partition and atomically (a temporary file renamed over), so a crash
    leaves either no partition or a partition with its counts."""
    folder = store.partition_dir(DATASET_BARS, trade_date)
    folder.mkdir(parents=True, exist_ok=True)
    counts = {name: getattr(report, name) for name in _CHECK_COUNTS}
    temporary = folder / f".{CHECKS_FILE}.tmp"
    temporary.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
    os.replace(temporary, folder / CHECKS_FILE)


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


def outcome_recorder(
    holder: Mapping[str, IngestReport], trade_date: date, clock: Clock | None = None
) -> OnFinish:
    """The `on_finish` hook of `ingest.nightly`: in the transaction that records the run's final
    status it appends exactly one of `ingest.completed` and `ingest.failed`, and on a failure
    trips the data-health kill so bots take no entry (HAT-DATA-045, 047). A success never clears
    the health kill: only a passing quality gate does (HAT-DATA-065)."""

    def hook(session: Session, row: HatJobRun) -> None:
        now = utc_now(clock) if clock is not None else utc_now()
        ok = row.status == "succeeded"
        report = holder.get("report")
        payload: dict[str, Any] = report.payload() if report is not None else {}
        payload["trade_date"] = trade_date.isoformat()
        payload["calls"] = row.calls
        if not ok:
            payload["error"] = row.error
        record(
            session,
            aggregate_type="ingest",
            aggregate_id=row.id,
            event_type="ingest.completed" if ok else "ingest.failed",
            actor="scheduler",
            payload=payload,
            occurred_at=now,
        )
        if not ok:
            sync_health(
                session,
                scope="account",
                bot_id=None,
                failing=True,
                reason="data_health",
                now=now,
            )

    return hook
