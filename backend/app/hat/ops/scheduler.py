"""The in-process asyncio scheduler (T7). Created only when HAT is enabled (HAT-OPS-013).

PR 2 builds the loop and the start gate; the daily-cycle jobs are registered by PR 17. The loop
calls `run_job`, which is idempotent, so a tick that fires twice cannot run a body twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..logs import configure_hat_logging
from ..settings import HatSettings, require_postgres
from ..time import IST, trade_date_of, utc_now
from .jobs import JobResult, RunStatus, SessionFactory, recover_interrupted, run_job

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
RETRY_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class ScheduledJob:
    """A job that becomes due at `at` (IST) on each trade date."""

    name: str
    at: time
    fn: Callable[[], JobResult | None]


class HatScheduler:
    def __init__(
        self,
        jobs: Sequence[ScheduledJob],
        session_factory: SessionFactory | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._jobs = tuple(jobs)
        self._session_factory = session_factory
        self._now = now
        self._attempted: dict[tuple[str, date], datetime] = {}
        self._task: asyncio.Task[None] | None = None

    def due_jobs(self, now: datetime) -> list[tuple[ScheduledJob, date]]:
        """Jobs whose time has passed today and that have not just been attempted."""
        today = trade_date_of(now)
        local = now.astimezone(IST).time()
        due = []
        for job in self._jobs:
            last = self._attempted.get((job.name, today))
            if local >= job.at and (last is None or now - last >= RETRY_AFTER):
                due.append((job, today))
        return due

    async def tick(self) -> None:
        now = self._now()
        for job, trade_date in self.due_jobs(now):
            outcome = await asyncio.to_thread(
                run_job, job.name, trade_date, job.fn, session_factory=self._session_factory
            )
            if outcome.status is RunStatus.SUCCEEDED:
                self._attempted[(job.name, trade_date)] = now + timedelta(days=1)
            else:
                self._attempted[(job.name, trade_date)] = now

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def start_scheduler(
    settings: HatSettings,
    *,
    dialect_name: str,
    jobs: Sequence[ScheduledJob] = (),
    session_factory: SessionFactory | None = None,
) -> HatScheduler | None:
    """Start the scheduler, or return None and start nothing when HAT is disabled.

    With HAT enabled on a non-Postgres database this raises `HatConfigError` before any task
    exists (HAT-SCH-105). Call from a running event loop.
    """
    if not settings.enabled:
        return None
    require_postgres(dialect_name)
    configure_hat_logging()
    recovered = recover_interrupted(session_factory)
    if recovered:
        logger.warning("recovered %d interrupted job runs", recovered)
    scheduler = HatScheduler(jobs, session_factory)
    scheduler.start()
    return scheduler
