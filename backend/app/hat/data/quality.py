"""The nightly data-quality gate (HAT-DATA-060..065, HAT-DEC-I14).

Four checks decide whether a day's data may be scanned: `coverage` (at least 99% of the active
universe has a bar), `ohlc` (no fetched row broke a row check), `big_moves` (every move over 20%
is explained by a stored corporate action or excluded from the day's scans) and `index_snapshot`
(NIFTY 500 and India VIX are stored). The checks are pure functions; `gate_body` is the
`quality.gate` job body for `run_job`. It writes one `hat_quality_check` row per check and, in
the same transaction, either `quality.failed` and the data-health kill (bots take no entry; exits
and stops go on) or, on a pass, the clearing of that kill (HAT-RISK-063). A failed gate fails
its job, so the steps that follow it do not run (HAT-OPS-021), and raises one alert through the
`Notifier` protocol.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..events import record
from ..models import HatCorporateAction, HatEvent, HatJobRun, HatQualityCheck
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobError, JobResult, SessionFactory
from ..risk.controls import sync_health
from ..time import utc_now
from .ingest import JOB_INGEST, active_universe, has_required_indices, moved_over_limit
from .store import DATASET_BARS, DataStore

JOB_QUALITY = "quality.gate"
COVERAGE_PERCENT = 99  # [A] HAT-DATA-060: at least 99% of the active universe
CHECK_COVERAGE = "coverage"
CHECK_OHLC = "ohlc"
CHECK_BIG_MOVES = "big_moves"
CHECK_INDEX = "index_snapshot"
CHECK_NAMES = (CHECK_COVERAGE, CHECK_OHLC, CHECK_BIG_MOVES, CHECK_INDEX)
STATUS_PASS = "PASS"  # noqa: S105
STATUS_FAIL = "FAIL"

CODE_FAILED = "quality_failed"
CODE_NO_INGEST = "quality_no_ingest"
CODE_NO_RUN = "quality_no_run"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return STATUS_PASS if self.passed else STATUS_FAIL


@dataclass(frozen=True)
class GateResult:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_names(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


def coverage_check(universe: int, stored: int) -> Check:
    """FAIL iff stored / universe < 0.99, by exact integer arithmetic (HAT-DATA-060). An empty
    universe cannot be covered."""
    passed = universe >= 1 and stored * 100 >= universe * COVERAGE_PERCENT
    return Check(CHECK_COVERAGE, passed, {"universe": universe, "stored": stored})


def ohlc_check(violations: int) -> Check:
    """FAIL iff at least one fetched row failed the row checks (HAT-DATA-061)."""
    return Check(CHECK_OHLC, violations == 0, {"violations": violations})


def big_move_keys(rows: Iterable[tuple[object, Decimal | None, Decimal | None]]) -> list[object]:
    """Keys of the (key, close, prev_close) rows that moved more than 20% in the day. A row with
    no usable `prev_close` has no move to judge."""
    return [key for key, close, prev in rows if moved_over_limit(close, prev)]


def big_moves_check(
    moved: Collection[object], explained: Collection[object], excluded: Collection[object]
) -> Check:
    """FAIL iff some instrument that moved over 20% has no corporate action on the trade date and
    is not on the day's exclusion list (HAT-DATA-062)."""
    unexplained = sorted((k for k in moved if k not in explained and k not in excluded), key=str)
    detail = {
        "moved": len(moved),
        "unexplained": [str(k) for k in unexplained],
        "excluded": sorted(str(k) for k in excluded if k in moved),
    }
    return Check(CHECK_BIG_MOVES, not unexplained, detail)


def index_snapshot_check(index_names: Iterable[str]) -> Check:
    """FAIL iff the day's index rows lack NIFTY 500 or India VIX (HAT-DATA-063)."""
    names = sorted({n.upper() for n in index_names})
    return Check(CHECK_INDEX, has_required_indices(names), {"indices": len(names)})


def scan_universe(universe: Collection[object], excluded: Collection[object]) -> list[object]:
    """The instruments a scan may use on the day: the universe less the exclusion list."""
    return [i for i in universe if i not in excluded]


def evaluate(
    *,
    universe: int,
    stored: int,
    violations: int,
    moved: Collection[object],
    explained: Collection[object],
    excluded: Collection[object],
    index_names: Iterable[str],
) -> GateResult:
    """All four checks, always in the order of `CHECK_NAMES`: a missing check is never a pass."""
    return GateResult(
        (
            coverage_check(universe, stored),
            ohlc_check(violations),
            big_moves_check(moved, explained, excluded),
            index_snapshot_check(index_names),
        )
    )


# --- the database side ------------------------------------------------------------------------


def persist(
    session: Session, run_id: uuid.UUID, result: GateResult, trade_date: date, clock: Clock | None
) -> None:
    """Write the gate's rows and its consequences in the caller's transaction (HAT-DATA-064,
    065): one row per check, then `quality.failed` and the health kill on a FAIL, or the health
    kill's clearing on a pass."""
    session.execute(delete(HatQualityCheck).where(HatQualityCheck.job_run_id == run_id))
    for check in result.checks:
        session.add(
            HatQualityCheck(
                job_run_id=run_id, check_name=check.name, status=check.status, detail=check.detail
            )
        )
    session.flush()
    now = utc_now(clock) if clock is not None else utc_now()
    if result.passed:
        sync_health(
            session, scope="account", bot_id=None, failing=False, reason="data_health", now=now
        )
        return
    record(
        session,
        aggregate_type="ingest",
        aggregate_id=run_id,
        event_type="quality.failed",
        actor="scheduler",
        payload={"trade_date": trade_date.isoformat(), "failed": result.failed_names},
        occurred_at=now,
    )
    sync_health(session, scope="account", bot_id=None, failing=True, reason="data_health", now=now)


def _run_id(session: Session, job: str, trade_date: date) -> uuid.UUID | None:
    return session.scalar(
        select(HatJobRun.id).where(HatJobRun.job == job, HatJobRun.trade_date == trade_date)
    )


def ingest_summary(session: Session, trade_date: date) -> Mapping[str, Any] | None:
    """The payload of the `ingest.completed` event of the day's ingest run, or None."""
    run_id = _run_id(session, JOB_INGEST, trade_date)
    if run_id is None:
        return None
    event = session.scalar(
        select(HatEvent)
        .where(HatEvent.aggregate_id == run_id, HatEvent.event_type == "ingest.completed")
        .order_by(HatEvent.id.desc())
    )
    return event.payload if event is not None else None


def gate_inputs(
    session: Session, store: DataStore, trade_date: date, summary: Mapping[str, Any]
) -> dict[str, Any]:
    """What `evaluate` needs, read from the universe, the day's stored bars, the corporate
    actions and the index rows."""
    members = {str(m.instrument_id) for m in active_universe(session)}
    frame = store.read_bars(trade_date)
    rows = [
        (r["instrument_id"], r["close"], r["prev_close"])
        for r in frame.select("instrument_id", "close", "prev_close").to_dicts()
    ]
    explained = {
        str(i)
        for i in session.scalars(
            select(HatCorporateAction.instrument_id).where(HatCorporateAction.ex_date == trade_date)
        )
    }
    return {
        "universe": len(members),
        "stored": len({r[0] for r in rows} & members),
        "violations": int(summary.get("violations", 0)),
        "moved": set(big_move_keys(rows)),
        "explained": explained,
        "index_names": [r.index_name for r in store.read_index(trade_date)],
    }


def gate_body(
    *,
    session_factory: SessionFactory,
    store: DataStore,
    trade_date: date,
    notifier: Notifier | None = None,
    excluded: Collection[uuid.UUID | str] = (),
    clock: Clock | None = None,
) -> Callable[[], JobResult]:
    """The `quality.gate` body for `run_job`. It fails (so the next step does not run) when any
    check fails, after committing the check rows, the event and the health kill."""
    sink = notifier if notifier is not None else LogNotifier()
    skipped = {str(i) for i in excluded}

    def body() -> JobResult:
        with session_factory() as session:
            run_id = _run_id(session, JOB_QUALITY, trade_date)
            if run_id is None:
                raise JobError(CODE_NO_RUN)
            summary = ingest_summary(session, trade_date)
            if summary is None or not store.partition_files(DATASET_BARS, trade_date):
                raise JobError(CODE_NO_INGEST)
            result = evaluate(excluded=skipped, **gate_inputs(session, store, trade_date, summary))
            persist(session, run_id, result, trade_date, clock)
            session.commit()
        if not result.passed:
            sink.alert(CODE_FAILED, trade_date, {"failed": ",".join(result.failed_names)})
            raise JobError(CODE_FAILED)
        return JobResult(rows=len(result.checks))

    return body


def excluded_for(session: Session, trade_date: date) -> set[str]:
    """The instrument ids the latest gate of `trade_date` excluded from that day's scans."""
    run_id = _run_id(session, JOB_QUALITY, trade_date)
    if run_id is None:
        return set()
    row = session.get(HatQualityCheck, (run_id, CHECK_BIG_MOVES))
    return set(row.detail.get("excluded", [])) if row is not None else set()
