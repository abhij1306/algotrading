"""The trade-management jobs of the daily cycle (10-ops §3 job table: `exits.close` 15:20-15:25,
`gtt.swap` ~15:25, `gtt.verify` nightly; HAT-EXEC-061, -063, -074, HAT-OPS-024). PR 15 holds
their logic; PR 17 registers them with the scheduler for each broker account.

Each body runs one `TradeManager` step in its own session and fails with a fixed code when the
step is incomplete, so `run_job` records the failure and the scheduler tries again after the
job's `retry_after`: a minute for the two close jobs. A job's name carries its broker account
(`exits.close:<account id>`), since a run is kept once per (name, trade date) and each account
needs its own. `exits.close` has a window and is never
started from 15:25 on (`ScheduledJob.until`); its body also refuses to run outside it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta

from ..execution.manage import EXITS_CLOSE_FROM, EXITS_CLOSE_UNTIL, TradeManager, ViewSource
from ..execution.protect import SWAP_AT
from .jobs import JobError, JobResult, SessionFactory
from .scheduler import ScheduledJob

__all__ = [
    "CLOSE_RETRY",
    "EXITS_CLOSE",
    "GTT_SWAP",
    "GTT_SWAP_AT",
    "GTT_VERIFY",
    "GTT_VERIFY_AT",
    "job_name",
    "trade_jobs",
]

EXITS_CLOSE = "exits.close"
GTT_SWAP = "gtt.swap"
GTT_VERIFY = "gtt.verify"
GTT_SWAP_AT = SWAP_AT
GTT_VERIFY_AT = time(23, 0)  # nightly, after the ingest window's 22:00 fallback
CLOSE_RETRY = timedelta(minutes=1)

Clock = Callable[[], datetime]


def job_name(base: str, account_id: uuid.UUID) -> str:
    """The job's name for one broker account."""
    return f"{base}:{account_id}"


def trade_jobs(
    manager: TradeManager,
    session_factory: SessionFactory,
    account_id: uuid.UUID,
    views: ViewSource,
    clock: Clock,
) -> tuple[ScheduledJob, ...]:
    """The three jobs of one broker account, at their 10-ops §3 times."""

    def close() -> JobResult:
        with session_factory() as session:
            report = manager.close_exits(session, account_id, views, clock())
        if report.refused is not None:
            raise JobError(report.refused)
        if report.failed:
            raise JobError("exits_close_incomplete")
        return JobResult(calls=report.sales, rows=report.recorded)

    def swap() -> JobResult:
        with session_factory() as session:
            report = manager.swap_stops(session, account_id, clock())
        if report.failed:
            raise JobError("gtt_swap_incomplete")
        return JobResult(rows=report.done)

    def verify() -> JobResult:
        with session_factory() as session:
            report = manager.verify_stops(session, account_id, clock())
        if report.failed:
            raise JobError("gtt_verify_incomplete")
        return JobResult(rows=report.done)

    return (
        ScheduledJob(
            job_name(EXITS_CLOSE, account_id),
            EXITS_CLOSE_FROM,
            close,
            until=EXITS_CLOSE_UNTIL,
            retry_after=CLOSE_RETRY,
        ),
        ScheduledJob(job_name(GTT_SWAP, account_id), GTT_SWAP_AT, swap, retry_after=CLOSE_RETRY),
        ScheduledJob(job_name(GTT_VERIFY, account_id), GTT_VERIFY_AT, verify),
    )
