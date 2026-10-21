"""Idempotent job runs keyed by (job, trade_date) (HAT-OPS-010, HAT-OPS-011, HAT-SCH-072).

`run_job` is the only way a HAT job body executes. The unique `hat_job_run (job, trade_date)`
row is the lock: a body runs only after its caller has claimed the row, so across repeated and
concurrent calls (and restarts) the body succeeds at most once per key. A failed or interrupted
run can be claimed again. The model of this protocol is `verification/tla/JobScheduler.tla`.

Each step below is one database transaction, which is the atomic step the model uses.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..models import HatJobRun
from ..time import as_utc, utc_now

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
# Runs inside the transaction that records a run's final status, before it commits, so an effect
# it writes (an event, a kill state) commits or rolls back together with the status
# (HAT-DATA-045). It is called only when this call is the one recording the final status.
OnFinish = Callable[[Session, HatJobRun], None]

DEFAULT_ERROR_CODE = "job_failed"
INTERRUPTED_ERROR_CODE = "interrupted"
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class JobError(Exception):
    """Raised by a job body to fail with a stable error code (never raw provider text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if _ERROR_CODE.fullmatch(code) else DEFAULT_ERROR_CODE


@dataclass(frozen=True)
class JobResult:
    """What a body reports: external calls made and rows written."""

    calls: int = 0
    rows: int = 0


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RUNNING = "running"  # another caller holds the claim; this call did not run the body


@dataclass(frozen=True)
class JobOutcome:
    """The stored result of the (job, trade_date) run this call observed."""

    job: str
    trade_date: date
    status: RunStatus
    calls: int
    rows: int
    error: str | None
    executed: bool  # True only when this call ran the body


def _default_session_factory() -> SessionFactory:
    from app.database import SessionLocal  # deferred: importing it connects the engine

    return SessionLocal


def error_code(exc: BaseException) -> str:
    """A stable code for `exc`: its own for `JobError`, else a generic one. Never exc text."""
    return exc.code if isinstance(exc, JobError) else DEFAULT_ERROR_CODE


def _outcome(row: HatJobRun, *, executed: bool) -> JobOutcome:
    return JobOutcome(
        job=row.job,
        trade_date=row.trade_date,
        status=RunStatus(row.status),
        calls=row.calls,
        rows=row.rows,
        error=row.error,
        executed=executed,
    )


def _claim(
    session_factory: SessionFactory, job: str, trade_date: date, now: datetime
) -> tuple[UUID | None, JobOutcome | None]:
    """Atomically take the (job, trade_date) run: insert it, or flip a failed run to running.

    Returns (run id, None) when this call now owns the run, else (None, the stored outcome).
    """
    with session_factory() as session:
        run = HatJobRun(job=job, trade_date=trade_date, started_at=now, status="running")
        session.add(run)
        try:
            session.commit()
            return run.id, None
        except IntegrityError:
            session.rollback()
        retry = session.execute(
            update(HatJobRun)
            .where(
                HatJobRun.job == job,
                HatJobRun.trade_date == trade_date,
                HatJobRun.status == "failed",
            )
            .values(
                status="running",
                started_at=now,
                finished_at=None,
                error=None,
                calls=0,
                rows=0,
            )
        )
        session.commit()
        existing = session.execute(
            select(HatJobRun).where(HatJobRun.job == job, HatJobRun.trade_date == trade_date)
        ).scalar_one()
        if retry.rowcount == 1:
            return existing.id, None
        return None, _outcome(existing, executed=False)


def _finish(
    session_factory: SessionFactory,
    run_id: UUID,
    *,
    status: str,
    result: JobResult,
    error: str | None,
    now: datetime,
    on_finish: OnFinish | None = None,
) -> JobOutcome:
    """Record the end of a run this call owns (only while it is still `running`)."""
    with session_factory() as session:
        row = session.get(HatJobRun, run_id)
        if row is None:
            raise RuntimeError("job run row vanished while its body ran")
        if row.status != "running":
            # A restart reaped this run while the body was executing; the stored row wins.
            logger.warning("job run %s was no longer running when it finished", run_id)
            return _outcome(row, executed=True)
        started = row.started_at

        def record_end(target: HatJobRun) -> None:
            target.status = status
            target.calls = result.calls
            target.rows = result.rows
            target.error = error
            target.finished_at = max(as_utc(now), as_utc(started))

        record_end(row)
        if on_finish is not None:
            try:
                session.flush()
                on_finish(session, row)
            except Exception:
                # The hook's writes and the status go together or not at all; a hook that fails
                # must not leave the run `running`, so the status is recorded without it.
                logger.exception("job finish hook failed", extra={"run": str(run_id)})
                session.rollback()
                row = session.get(HatJobRun, run_id)
                if row is None:
                    raise RuntimeError("job run row vanished while its body ran") from None
                record_end(row)
        session.commit()
        return _outcome(row, executed=True)


def run_job(
    name: str,
    trade_date: date,
    fn: Callable[[], JobResult | None],
    *,
    session_factory: SessionFactory | None = None,
    on_finish: OnFinish | None = None,
) -> JobOutcome:
    """Run `fn` for (name, trade_date) at most once to success.

    After a success, later calls return the stored outcome without running `fn`. After a
    failure or an interruption, a call runs `fn` again. While another call holds the run, this
    call returns `RunStatus.RUNNING` without running `fn`. A body that raises is recorded as
    failed with an error code; the exception text goes to the server log only. `on_finish`, when
    given, runs in the transaction that records the final status (see `OnFinish`).
    """
    factory = session_factory or _default_session_factory()
    run_id, stored = _claim(factory, name, trade_date, utc_now())
    if run_id is None:
        assert stored is not None  # _claim returns an id or an outcome
        return stored
    try:
        result = fn() or JobResult()
    except Exception as exc:
        logger.exception("job failed", extra={"job": name, "trade_date": str(trade_date)})
        return _finish(
            factory,
            run_id,
            status="failed",
            result=JobResult(),
            error=error_code(exc),
            now=utc_now(),
            on_finish=on_finish,
        )
    return _finish(
        factory,
        run_id,
        status="succeeded",
        result=result,
        error=None,
        now=utc_now(),
        on_finish=on_finish,
    )


def recover_interrupted(session_factory: SessionFactory | None = None) -> int:
    """Mark every run still `running` as failed (`interrupted`); call once at process start,
    before any job runs, when no run of the previous process can still be executing.

    Returns how many runs were recovered.
    """
    factory = session_factory or _default_session_factory()
    now = utc_now()
    with factory() as session:
        rows = session.scalars(select(HatJobRun).where(HatJobRun.status == "running")).all()
        for row in rows:
            row.status = "failed"
            row.error = INTERRUPTED_ERROR_CODE
            row.finished_at = max(now, as_utc(row.started_at))
        session.commit()
        return len(rows)


def session_factory_for(bind: object) -> sessionmaker[Session]:
    """A session factory on `bind` (an Engine), for callers and tests that own their engine."""
    return sessionmaker(bind=bind)  # type: ignore[arg-type]
