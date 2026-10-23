"""The nightly and housekeeping job bodies of the daily cycle (PR 17).

Each body is the PR's job logic called with its real inputs: the ingest polls the NSE MCP from
18:00 and falls back to the bhavcopy at 22:00 (PR 7), then `quality.gate` → `ca.adjust` →
`features` → `regime` → `allocate` → `scan` → `watchlist.digest` run one after the other through
the scheduler's `after` chain (HAT-OPS-021), each for the same trade date. `snapshot.bots` follows
`allocate` (the drawdown kills read it, and the 15:40 report reads the latest snapshot), and the
gaps between the 15:25 exits and the official closes are recorded once the ingest has the day's
closes (HAT-OPS-034). Housekeeping (calendar, reference data, bands, event partitions, the chat
log purge) runs on its own schedule.

The bodies take everything outside themselves through `NightlyDeps`, so the simulated day runs
them over fixture data, a recorded MCP transport and fake text sources.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..data import ingest as nightly_ingest
from ..data.bhavcopy import Fetcher
from ..data.calendar import session_calendar
from ..data.corporate_actions import lookup_actions, store_actions, stored_factors
from ..data.features import features_job
from ..data.mcp_client import McpClient
from ..data.quality import JOB_QUALITY, excluded_for, gate_body
from ..data.quarantine import open_quarantines
from ..data.reference_jobs import (
    CircuitSource,
    TextSource,
    bands_nightly,
    calendar_refresh,
    reference_weekly,
)
from ..data.renames import renames_from_bars
from ..data.store import ActionFactor, DataStore
from ..data.symbols import symbol_master
from ..engine.journal import ALERT_JOURNAL_MISMATCH, mismatches
from ..engine.live_trades import record_close_gaps
from ..engine.portfolio.allocation_service import policy_at
from ..engine.portfolio.jobs import ALLOCATE_JOB, REGIME_JOB, allocate_job, regime_job
from ..engine.rescale import (
    ALERT_ACTION_UNCHECKED,
    flag_unhandled,
    held_and_watchlisted,
    rescale_open_positions,
)
from ..engine.scan import ScanInputs
from ..engine.strategy.config import parse_config
from ..engine.watchlist import scan_and_publish
from ..events import record
from ..models import HatBot, HatJobRun, HatStrategyVersion
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobError, JobResult, OnFinish, SessionFactory
from ..ops.partitions import events_partitions
from ..ops.retention import purge_job
from ..ops.scheduler import Days, JobBody, ScheduledJob
from ..risk.controls import (
    ALERT_FALLBACK_MARKS,
    ALERT_KILL_DRAWDOWN,
    evaluate_drawdown_kills,
    snapshot_bots,
    sync_health,
)
from ..time import utc_now
from .market import official_closes, scan_inputs

__all__ = [
    "BANDS_NIGHTLY",
    "CALENDAR_REFRESH",
    "CA_ADJUST",
    "CA_RESCALE",
    "CLOSE_GAPS",
    "EVENTS_PARTITIONS",
    "FEATURES",
    "INGEST_NIGHTLY",
    "JOURNAL_CHECK",
    "NIGHTLY_CHAIN",
    "NightlyDeps",
    "REFERENCE_WEEKLY",
    "RETENTION_PURGE",
    "SCAN",
    "SNAPSHOT_BOTS",
    "TextSources",
    "WATCHLIST_DIGEST",
    "next_session",
    "nightly_jobs",
]

INGEST_NIGHTLY = nightly_ingest.JOB_INGEST
CA_ADJUST = "ca.adjust"
CA_RESCALE = "ca.rescale"
FEATURES = "features"
SCAN = "scan"
WATCHLIST_DIGEST = "watchlist.digest"
SNAPSHOT_BOTS = "snapshot.bots"
CLOSE_GAPS = "exits.close_gaps"
CALENDAR_REFRESH = "calendar.refresh"
REFERENCE_WEEKLY = "reference.weekly"
BANDS_NIGHTLY = "bands.nightly"
EVENTS_PARTITIONS = "events.partitions"
RETENTION_PURGE = "retention.purge"
JOURNAL_CHECK = "journal.check"

logger = logging.getLogger(__name__)

INGEST_AT = time(18, 0)  # the first probe (HAT-OPS-022); the body waits until 22:00 at most
# `snapshot.bots` waits on no job (PR 33): the drawdown kills are measured every trading night,
# on the latest allocation on or before the day, after the chain has normally finished. It
# carries past midnight as a chained job does (`carry`), so a restart before 08:30 still runs it.
SNAPSHOT_AT = time(23, 30)
CALENDAR_AT = time(7, 0)  # daily: a changed holiday list is picked up the same morning
BANDS_AT = time(7, 45)  # before the pre-open: the bands in force today
RESCALE_AT = time(8, 40)  # before `live.prepare` and the 09:00 pre-open (HAT-EXEC-141)
REFERENCE_AT = time(9, 0)
REFERENCE_WEEKDAY = 5  # Saturday: the market is shut and the instrument master is quiet
JOURNAL_AT = time(23, 15)  # after the session's last state change and the 23:00 GTT verify
JOURNAL_ALERTS = 10  # alerts per run at most; the first carries the count
PARTITIONS_AT = time(0, 30)
PURGE_AT = time(1, 0)
INGEST_RETRY = timedelta(minutes=10)
RESCALE_RETRY = timedelta(minutes=5)  # three tries before the 09:00 pre-open


@dataclass(frozen=True)
class TextSources:
    """Where the reference files come from: NSE downloads in production, fixtures in checks."""

    equity_list: TextSource
    holidays: TextSource
    price_bands: Callable[[date], TextSource]
    industry_list: TextSource | None = None
    circuit_limits: CircuitSource | None = None


@dataclass
class NightlyDeps:
    store: DataStore
    session_factory: SessionFactory
    clock: Clock
    user_id: uuid.UUID
    mcp: Callable[[], McpClient | None]  # a fresh client per run, so its call count is the run's
    bhavcopy: Callable[[], Fetcher]  # the 22:00 fallback's downloader
    sources: TextSources
    total_equity: Callable[[date], Decimal]  # [OWNER-PENDING] HAT-PORT-052: broker funds
    sleep: Callable[[float], None]  # the ingest's wait; the runtime's raises when HAT stops
    notifier: Notifier = field(default_factory=LogNotifier)
    digest: Callable[[uuid.UUID, date], None] | None = None  # Reports.send_watchlist_digest
    on_loaded: Callable[[], None] = lambda: None  # new daily bars: drop the cached daily view


def next_session(session: Session, day: date) -> date | None:
    """The first trading day of the calendar after `day`; None when the calendar holds none."""
    calendar = session_calendar(session)
    return None if calendar is None else calendar.next_trading_day(day)


def _mcp(deps: NightlyDeps) -> McpClient:
    client = deps.mcp()
    if client is None:
        raise JobError("mcp_not_configured")
    return client


def _active_bots(session: Session) -> list[HatBot]:
    return list(
        session.scalars(
            select(HatBot)
            .where(HatBot.status == "active", HatBot.live_version_id.is_not(None))
            .order_by(HatBot.id)
        )
    )


def _index_of(session: Session, bot: HatBot) -> str | None:
    version = session.get(HatStrategyVersion, bot.live_version_id)
    return None if version is None else parse_config(version.config).regime.index


def ingest_outcome_recorder(
    holder: Mapping[str, nightly_ingest.IngestReport], trade_date: date, clock: Clock | None = None
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


class Nightly:
    """The job bodies over one `NightlyDeps`. Each takes the trade date the run is kept under."""

    def __init__(self, deps: NightlyDeps) -> None:
        self.deps = deps
        self._ingest_reports: dict[date, dict[str, nightly_ingest.IngestReport]] = {}

    def _now(self) -> datetime:
        return utc_now(self.deps.clock)

    # --- the pipeline -----------------------------------------------------------------------

    def ingest(self, day: date) -> JobResult:
        deps = self.deps
        holder = self._ingest_reports.setdefault(day, {})
        body = nightly_ingest.ingest_body(
            nightly_ingest.IngestDeps(
                client=_mcp(deps),
                store=deps.store,
                session_factory=deps.session_factory,
                clock=deps.clock,
                sleep=deps.sleep,
                fallback=lambda d: nightly_ingest.bhavcopy_day(deps.store, deps.bhavcopy(), d),
                notifier=deps.notifier,
            ),
            day,
            holder,
        )
        result = body()
        deps.on_loaded()
        return result

    def ingest_finished(self, day: date) -> OnFinish:
        """`ingest.completed` or `ingest.failed`, in the transaction that records the run."""
        return ingest_outcome_recorder(
            self._ingest_reports.setdefault(day, {}), day, self.deps.clock
        )

    def ca_adjust(self, day: date) -> JobResult:
        """Corporate actions adjust prices at read time (`bars_daily` over the stored factors).
        First the next session's actions of the held and watchlisted instruments are looked up
        and stored (HAT-EXEC-140), so the features, the scan and the morning's `ca.rescale` see
        them; a failed lookup is alerted and never fails the step. Then the actions of every
        quarantined instrument on its `since` date are asked again (HAT-DATA-062, PR 33): a
        split whose lookup failed on the day releases its quarantine at the next gate once
        stored. A failure there is only logged: the quarantine was alerted already and stands.
        Then the factors are checked to load into the adjusted view before the features read
        it."""
        try:
            self._next_session_actions(day)
        except Exception:
            # Fail closed without stopping the chain: tomorrow's features and scan still run, and
            # the alert says the next session's actions are unknown.
            logger.exception("next-session corporate-action lookup failed")
            self.deps.notifier.alert(ALERT_ACTION_UNCHECKED, day, {"symbol": "*"})
        try:
            self._quarantine_actions()
        except Exception:
            logger.exception("quarantine corporate-action lookup failed")
        with self.deps.session_factory() as session:
            factors = stored_factors(session)
        with self.deps.store.connect(factors):
            logger.info("adjusted view loads with %d factors", len(factors))
        return JobResult(rows=len(factors))

    def _next_session_actions(self, day: date) -> None:
        with self.deps.session_factory() as session:
            target = next_session(session, day)
            instruments = held_and_watchlisted(session, day)
        if target is None or not instruments:
            return
        found = lookup_actions(_mcp(self.deps), instruments, target, target)
        created = store_actions(self.deps.session_factory, found.records)
        logger.info(
            "next-session actions: %d stored, %d lookups failed", created, len(found.failed)
        )
        for symbol in found.failed:
            self.deps.notifier.alert(ALERT_ACTION_UNCHECKED, day, {"symbol": symbol})

    def _quarantine_actions(self) -> None:
        with self.deps.session_factory() as session:
            rows = open_quarantines(session)
        if not rows:
            return
        client = _mcp(self.deps)
        for since in sorted({r.since for r in rows}):
            instruments = {r.symbol: r.instrument_id for r in rows if r.since == since}
            found = lookup_actions(client, instruments, since, since)
            logger.info(
                "quarantine actions of %s: %d stored, %d lookups failed",
                since,
                store_actions(self.deps.session_factory, found.records),
                len(found.failed),
            )

    def rescale(self, day: date) -> JobResult:
        """`ca.rescale` (before the open): every open position spanning a stored split or bonus
        is rescaled (HAT-EXEC-141), then every action the rescale cannot handle is flagged once
        per position (`corporate_action.unhandled`, HAT-EXEC-144). Fails when a position could
        not be rescaled or the flags not written, so the job is retried; the position stays
        manage-only and is alerted meanwhile."""
        now = self._now()
        applied, failed = rescale_open_positions(
            self.deps.session_factory, day, now, self.deps.notifier
        )
        with self.deps.session_factory() as session:
            flag_unhandled(session, day, now)
            session.commit()
        if failed:
            raise JobError("rescale_failed")
        return JobResult(rows=applied)

    def features(self, day: date) -> JobResult:
        def actions() -> list[ActionFactor]:
            with self.deps.session_factory() as session:
                return stored_factors(session)

        return features_job(self.deps.store, actions, through=day)()

    def regime(self, day: date) -> JobResult:
        with self.deps.session_factory() as session:
            result = regime_job(session, self.deps.store, day)
            session.commit()
        return result

    def allocate(self, day: date) -> JobResult:
        with self.deps.session_factory() as session:
            result = allocate_job(
                session,
                day,
                user_id=self.deps.user_id,
                total_equity=lambda: self.deps.total_equity(day),
            )
            session.commit()
        return result

    def snapshot(self, day: date) -> JobResult:
        """Each bot's equity (its open positions marked at the day's official closes), peak and
        open risk for `day`, then the drawdown kills on them. A holding with no close of `day`
        is marked at its stop (`kill.mark_of`) and alerted by count; each scope tripped is
        alerted."""
        closes = official_closes(self.deps.store, day)
        with self.deps.session_factory() as session:
            result, fallbacks = snapshot_bots(
                session, day, lambda instrument_id: (closes.get(instrument_id), True, None)
            )
            tripped = evaluate_drawdown_kills(
                session, policy_at(session, self.deps.user_id, day), self._now()
            )
            session.commit()
        if fallbacks:
            self.deps.notifier.alert(ALERT_FALLBACK_MARKS, day, {"holdings": fallbacks})
        for scope in tripped:
            self.deps.notifier.alert(
                ALERT_KILL_DRAWDOWN,
                day,
                {"scope": scope, "basis": "close", "fallback_marks": fallbacks},
            )
        return result

    def scan(self, day: date) -> JobResult:
        """Each active bot's watchlist for the next session from the bars of `day`, without the
        instruments the quality gate excluded (HAT-DATA-064)."""
        rows = 0
        with self.deps.session_factory() as session:
            target = next_session(session, day)
            if target is None:
                raise JobError("scan_no_next_session")
            excluded = excluded_for(session, day)
            inputs: dict[str, ScanInputs] = {}
            for bot in _active_bots(session):
                index = _index_of(session, bot)
                if index is None:
                    continue
                if index not in inputs:
                    found = scan_inputs(self.deps.store, session, day, index)
                    if excluded and found.bars.height:
                        found = _without(found, excluded)
                    inputs[index] = found
                published = scan_and_publish(session, bot.id, inputs[index], target, self._now())
                session.commit()
                rows += published.rows
        return JobResult(rows=rows)

    def watchlist_digest(self, day: date) -> JobResult:
        send = self.deps.digest
        with self.deps.session_factory() as session:
            target = next_session(session, day)
            bots = [b.id for b in _active_bots(session)]
        if target is None:
            raise JobError("scan_no_next_session")
        if send is not None:
            for bot_id in bots:
                send(bot_id, target)
        return JobResult(rows=len(bots))

    def close_gaps(self, day: date) -> JobResult:
        closes = official_closes(self.deps.store, day)
        with self.deps.session_factory() as session:
            recorded = record_close_gaps(session, day, closes, self._now())
            session.commit()
        return JobResult(rows=recorded)

    def journal_check(self, day: date) -> JobResult:
        """`journal.check` (HAT-EXEC-042, PR 36): every trade not yet ended is folded from its
        events and each one whose stored state differs (or whose events do not fold) is alerted
        `journal.mismatch`, at most `JOURNAL_ALERTS` a night, each carrying the count. The check
        runs twice in fresh transactions and only a mismatch both runs saw counts, so a trade
        that moved between the reads of one run is not alerted. Nothing is repaired: the owner
        decides which of the log and the row is right."""
        with self.deps.session_factory() as session:
            first = {(m[0], m[1]) for m in mismatches(session)}
            session.rollback()
            found = [m for m in mismatches(session) if (m[0], m[1]) in first]
        for signal_id, kind, stored, replayed in found[:JOURNAL_ALERTS]:
            self.deps.notifier.alert(
                ALERT_JOURNAL_MISMATCH,
                day,
                {
                    "signal": str(signal_id),
                    "kind": kind,
                    "stored": stored,
                    "replayed": replayed,
                    "count": len(found),
                },
            )
        return JobResult(rows=len(found))

    # --- housekeeping -----------------------------------------------------------------------

    def reference(self, day: date) -> JobResult:
        deps = self.deps
        return reference_weekly(
            client=_mcp(deps),
            equity_list=deps.sources.equity_list,
            industry_list=deps.sources.industry_list,
            session_factory=deps.session_factory,
            master=symbol_master,
            today=day,
            renames=lambda: renames_from_bars(deps.store),
        )()

    def bands(self, day: date) -> JobResult:
        deps = self.deps
        return bands_nightly(
            price_band_file=deps.sources.price_bands(day),
            circuit_limits=deps.sources.circuit_limits,
            session_factory=deps.session_factory,
            effective=day,
        )()

    def partitions(self, day: date) -> JobResult:
        with self.deps.session_factory() as session:
            bind = session.get_bind()
        return events_partitions(bind, day, self.deps.notifier)

    def purge(self, day: date) -> JobResult:
        with self.deps.session_factory() as session:
            return purge_job(session, day)


def _without(inputs: ScanInputs, excluded: set[str]) -> ScanInputs:
    keep = ~pl.col("instrument_id").cast(pl.String).is_in(sorted(excluded))
    return copy.replace(inputs, bars=inputs.bars.filter(keep))


def _built(
    make: Callable[..., Callable[[], JobResult]],
    day_arg: str | None,
    day: date,
    /,
    **kwargs: object,
) -> JobResult:
    """Run the body `make` builds from `kwargs`, given the run's trade date as `day_arg` (not at
    all when it is None)."""
    return make(**kwargs, **({} if day_arg is None else {day_arg: day}))()


def _quality_gate(n: Nightly) -> JobBody:
    deps = n.deps
    return partial(
        _built,
        gate_body,
        "trade_date",
        session_factory=deps.session_factory,
        store=deps.store,
        notifier=deps.notifier,
        clock=deps.clock,
    )


# The pipeline of HAT-OPS-021 after `ingest.nightly`, in order, each with how its body is got from
# the `Nightly`: each job starts once the one before it succeeded. `ca.adjust` is not in it
# (PR 33): it runs after the ingest whatever the gate decides, so the next session's corporate
# actions of held instruments are always looked up, and `features` waits for both it and the gate
# (`_ALSO_AFTER`).
_CHAIN: tuple[tuple[str, Callable[[Nightly], JobBody]], ...] = (
    (JOB_QUALITY, _quality_gate),
    (FEATURES, lambda n: n.features),
    (REGIME_JOB, lambda n: n.regime),
    (ALLOCATE_JOB, lambda n: n.allocate),
    (SCAN, lambda n: n.scan),
    (WATCHLIST_DIGEST, lambda n: n.watchlist_digest),
)
_ALSO_AFTER: Mapping[str, tuple[str, ...]] = {FEATURES: (CA_ADJUST,)}
NIGHTLY_CHAIN = (INGEST_NIGHTLY, *(name for name, _ in _CHAIN))


def nightly_jobs(deps: NightlyDeps) -> tuple[ScheduledJob, ...]:
    """The nightly pipeline and the housekeeping jobs at their times (IST)."""
    n = Nightly(deps)
    pipeline = tuple(
        ScheduledJob(name, INGEST_AT, body(n), after=(before, *_ALSO_AFTER.get(name, ())))
        for before, (name, body) in zip(NIGHTLY_CHAIN[:-1], _CHAIN, strict=True)
    )
    return (
        ScheduledJob(
            INGEST_NIGHTLY,
            INGEST_AT,
            n.ingest,
            retry_after=INGEST_RETRY,
            on_finish=n.ingest_finished,
        ),
        ScheduledJob(CA_ADJUST, INGEST_AT, n.ca_adjust, after=(INGEST_NIGHTLY,)),
        *pipeline,
        ScheduledJob(SNAPSHOT_BOTS, SNAPSHOT_AT, n.snapshot, carry=True),
        ScheduledJob(CLOSE_GAPS, INGEST_AT, n.close_gaps, after=(INGEST_NIGHTLY,)),
        ScheduledJob(JOURNAL_CHECK, JOURNAL_AT, n.journal_check),
        ScheduledJob(
            CALENDAR_REFRESH,
            CALENDAR_AT,
            partial(
                _built,
                calendar_refresh,
                None,
                holiday_list=deps.sources.holidays,
                session_factory=deps.session_factory,
            ),
            days=Days.EVERY,
        ),
        ScheduledJob(
            REFERENCE_WEEKLY,
            REFERENCE_AT,
            n.reference,
            days=Days.EVERY,
            weekday=REFERENCE_WEEKDAY,
        ),
        ScheduledJob(BANDS_NIGHTLY, BANDS_AT, n.bands),
        ScheduledJob(CA_RESCALE, RESCALE_AT, n.rescale, retry_after=RESCALE_RETRY),
        ScheduledJob(EVENTS_PARTITIONS, PARTITIONS_AT, n.partitions, days=Days.EVERY),
        ScheduledJob(RETENTION_PURGE, PURGE_AT, n.purge, days=Days.EVERY),
    )
