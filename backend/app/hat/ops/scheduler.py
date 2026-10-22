"""The in-process asyncio scheduler (T7). `HatRuntime` creates it, only when HAT is enabled
(HAT-OPS-013).

PR 2 built the loop; PR 17 registers the daily cycle (`app/hat/daily/runtime.py:daily_jobs`)
and adds what the cycle needs: the trade date passed to each body, the calendar (a trade-date
job starts only on a trading day of `hat_trading_calendar`, HAT-OPS-012), job order (a job with
`after` starts only once each of those jobs has succeeded for the same trade date, HAT-OPS-021,
HAT-PORT-053) and concurrent runs (the nightly ingest waits for its data for up to four hours, and
the other jobs must not wait behind it). Bodies run on the scheduler's own threads, one per
registered job: a job runs at most once at a time, so a body never waits for a thread, and a
restart at 15:19 that catches up the morning's jobs cannot hold `exits.close` past its window
(audit money-safety-engine-ops F12). The loop calls `run_job`, which is idempotent, so a tick
that fires twice cannot run a body twice.

A job with a window (`until`, PR 15c) is never started at or after its end: `exits.close` runs
between 15:20 and 15:25 IST or not at all that day (HAT-EXEC-074, TLA
`JobScheduler!ExitsCloseInWindow`), and a failed run is tried again after its own
`retry_after`, which fits inside the window.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import select

from ..models import HatJobRun
from ..time import IST, trade_date_of, utc_now
from .jobs import JobResult, OnFinish, RunStatus, SessionFactory, run_job

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
RETRY_AFTER = timedelta(minutes=10)
STOP_GRACE_SECONDS = 10.0

JobBody = Callable[[date], JobResult | None]
TradingDay = Callable[[date], bool]


class Days(StrEnum):
    """Which dates a job runs on."""

    TRADING = "trading"  # trading days of hat_trading_calendar only (HAT-OPS-012)
    EVERY = "every"  # every date: housekeeping that does not depend on the market


@dataclass(frozen=True)
class ScheduledJob:
    """A job that becomes due at `at` (IST) on each of its dates, and is no longer started from
    `until` (IST) on when it has a window. `fn` gets the trade date the run is kept under.

    `after` names jobs that must have succeeded for the same trade date first; `weekday`
    (Monday 0) limits the job to one day of the week; `on_finish` is passed to `run_job`."""

    name: str
    at: time
    fn: JobBody
    until: time | None = None
    retry_after: timedelta = RETRY_AFTER
    after: tuple[str, ...] = ()
    days: Days = Days.TRADING
    weekday: int | None = None
    on_finish: Callable[[date], OnFinish] | None = None


def runs_on(job: ScheduledJob, day: date, trading: TradingDay) -> bool:
    """Whether `job` has a run on `day`: its weekday (if any) and, for a trade-date job, a
    trading day of the calendar. A date the calendar does not know is not a trading day."""
    if job.weekday is not None and day.weekday() != job.weekday:
        return False
    return job.days is Days.EVERY or trading(day)


def due(
    job: ScheduledJob,
    now: datetime,
    *,
    trading: TradingDay,
    succeeded: Collection[str],
    attempted: datetime | None,
) -> bool:
    """Whether the scheduler starts `job` at `now`: a date it runs on, at or after its time and
    before the end of its window, every `after` job succeeded today, and not attempted within its
    `retry_after`."""
    today = trade_date_of(now)
    local = now.astimezone(IST).time()
    if job.name in succeeded or not runs_on(job, today, trading):
        return False
    if local < job.at or (job.until is not None and local >= job.until):
        return False
    if any(name not in succeeded for name in job.after):
        return False
    return attempted is None or now - attempted >= job.retry_after


def succeeded_on(session_factory: SessionFactory, day: date) -> set[str]:
    """The jobs with a succeeded run for `day`."""
    with session_factory() as session:
        return set(
            session.scalars(
                select(HatJobRun.job).where(
                    HatJobRun.trade_date == day, HatJobRun.status == RunStatus.SUCCEEDED.value
                )
            )
        )


class HatScheduler:
    def __init__(
        self,
        jobs: Sequence[ScheduledJob],
        session_factory: SessionFactory | None = None,
        now: Callable[[], datetime] = utc_now,
        *,
        trading: TradingDay | None = None,
    ) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate scheduled job name")
        self._jobs = tuple(jobs)
        self._session_factory = session_factory
        self._now = now
        self._trading: TradingDay = trading or (lambda _day: True)
        self._attempted: dict[tuple[str, date], datetime] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._task: asyncio.Task[None] | None = None
        # One thread per job, apart from the loop's default pool that other work shares.
        self._threads = ThreadPoolExecutor(
            max_workers=max(1, len(self._jobs)), thread_name_prefix="hat-job"
        )

    @property
    def jobs(self) -> tuple[ScheduledJob, ...]:
        return self._jobs

    def _factory(self) -> SessionFactory:
        if self._session_factory is None:
            from app.database import SessionLocal  # deferred: importing it connects the engine

            self._session_factory = SessionLocal
        return self._session_factory

    def due_jobs(
        self, now: datetime, succeeded: Collection[str] = ()
    ) -> list[tuple[ScheduledJob, date]]:
        """The jobs to start at `now` that are not already running."""
        today = trade_date_of(now)
        return [
            (job, today)
            for job in self._jobs
            if job.name not in self._running
            and due(
                job,
                now,
                trading=self._trading,
                succeeded=succeeded,
                attempted=self._attempted.get((job.name, today)),
            )
        ]

    async def _run(self, job: ScheduledJob, trade_date: date, started: datetime) -> None:
        try:
            outcome = await asyncio.get_running_loop().run_in_executor(
                self._threads,
                functools.partial(
                    run_job,
                    job.name,
                    trade_date,
                    lambda: job.fn(trade_date),
                    session_factory=self._factory(),
                    on_finish=job.on_finish(trade_date) if job.on_finish is not None else None,
                ),
            )
            if outcome.status is RunStatus.SUCCEEDED:
                self._attempted[(job.name, trade_date)] = started + timedelta(days=1)
        except Exception:
            logger.exception("scheduled job could not run", extra={"job": job.name})
        finally:
            self._running.pop(job.name, None)

    async def tick(self) -> list[asyncio.Task[None]]:
        """Start every due job as its own task; return the tasks started."""
        now = self._now()
        today = trade_date_of(now)
        succeeded = await asyncio.to_thread(succeeded_on, self._factory(), today)
        started = []
        for job, trade_date in self.due_jobs(now, succeeded):
            self._attempted[(job.name, trade_date)] = now
            task = asyncio.get_running_loop().create_task(self._run(job, trade_date, now))
            self._running[job.name] = task
            started.append(task)
        return started

    async def drain(self) -> None:
        """Wait for every running job (tests and shutdown)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

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
        """Stop starting jobs and wait a little for running ones (the runtime tells long bodies
        to end first). A body still running after the grace period is left to its thread; the
        next start reaps its run as interrupted (`recover_interrupted`)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._running:
            await asyncio.wait(list(self._running.values()), timeout=STOP_GRACE_SECONDS)
        self._threads.shutdown(wait=False)
