"""The in-process asyncio scheduler (T7). `HatRuntime` creates it, only when HAT is enabled
(HAT-OPS-013).

PR 2 built the loop; PR 17 registers the daily cycle (`app/hat/daily/assembly.py:daily_jobs`)
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
`JobScheduler!ExitsCloseInWindow`), and a failed run is tried again `retry_after` after the
failed run ended, which fits inside the window.

A chained job (`after`, no window) of the night before still starts after IST midnight, under
that night's trade date, until `CARRY_UNTIL`: a nightly chain whose head finished late (the
ingest succeeded at 23:58) completes for its own date instead of being dropped.

P5 (audit R1): every job has a `deadline`, the time after `at` by which its run must have
succeeded (`daily/assembly.py:JOB_DEADLINES` is the one table of them). The owner gets one alert
per job and trade date (`overdue`, `_alert`): `job.failed` when a run fails, `job.missed` when the
deadline passes with no success and nothing running (never started, or failed and not run again),
`job.running_long` when a body is still running at its deadline. A job that never started
because a job it waits on (`after`) has not succeeded is not alerted: that job is. The first of
these is the only one: the run row's `alerted_at` keeps it across retries and restarts, and the
scheduler's memory covers a job that has no row yet (so a restart may repeat that one). An alert
never raises into the loop.

A8 (audit R5): each tick also calls the runtime's `watch` (`daily/periodic.py:Heartbeats.check`,
`periodic.stalled` for a periodic loop that stopped beating); it never raises into the loop.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from sqlalchemy import select, update

from ..clock import SYSTEM_CLOCK, Clock
from ..models import HatJobRun
from ..notify import LogNotifier, Notifier
from ..time import IST, trade_date_of, utc_now
from .jobs import JobResult, OnFinish, RunStatus, SessionFactory, run_job

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
RETRY_AFTER = timedelta(minutes=10)
# Running bodies get this long to end on stop; the compose `stop_grace_period` is 30 s and the
# feed and the desks stop after the scheduler.
STOP_GRACE_SECONDS = 25.0
CARRY_UNTIL = time(8, 30)  # before `live.prepare` (08:45) reads the night's watchlist
ONE_DAY = timedelta(days=1)
DEFAULT_DEADLINE = timedelta(hours=1)
JOB_FAILED = "job.failed"
JOB_MISSED = "job.missed"
JOB_RUNNING_LONG = "job.running_long"

JobBody = Callable[[date], JobResult | None]
TradingDay = Callable[[date], bool]
Watch = Callable[[datetime], object]


class Days(StrEnum):
    """Which dates a job runs on."""

    TRADING = "trading"  # trading days of hat_trading_calendar only (HAT-OPS-012)
    EVERY = "every"  # every date: housekeeping that does not depend on the market


@dataclass(frozen=True)
class ScheduledJob:
    """A job that becomes due at `at` (IST) on each of its dates, and is no longer started from
    `until` (IST) on when it has a window. `fn` gets the trade date the run is kept under.

    `after` names jobs that must have succeeded for the same trade date first; `weekday`
    (Monday 0) limits the job to one day of the week; `on_finish` is passed to `run_job`;
    `carry` lets a job with no `after` (and no window) carry past midnight as a chained one does
    (`carries_over`; `snapshot.bots`, PR 33); `deadline` is how long after `at` the run has to
    have succeeded before the owner is told (`overdue`)."""

    name: str
    at: time
    fn: JobBody
    until: time | None = None
    retry_after: timedelta = RETRY_AFTER
    after: tuple[str, ...] = ()
    days: Days = Days.TRADING
    weekday: int | None = None
    on_finish: Callable[[date], OnFinish] | None = None
    carry: bool = False
    deadline: timedelta = DEFAULT_DEADLINE


def runs_on(job: ScheduledJob, day: date, trading: TradingDay) -> bool:
    """Whether `job` has a run on `day`: its weekday (if any) and, for a trade-date job, a
    trading day of the calendar. A date the calendar does not know is not a trading day."""
    if job.weekday is not None and day.weekday() != job.weekday:
        return False
    return job.days is Days.EVERY or trading(day)


def carries_over(job: ScheduledJob, day: date, now: datetime) -> bool:
    """Whether `now` is still in the carry-over of `day` for `job`: a chained job (or one marked
    `carry`) without a window, on the next IST date before `CARRY_UNTIL`."""
    local = now.astimezone(IST)
    return (
        (bool(job.after) or job.carry)
        and job.until is None
        and local.date() == day + ONE_DAY
        and local.time() < CARRY_UNTIL
    )


def due(
    job: ScheduledJob,
    now: datetime,
    *,
    trading: TradingDay,
    succeeded: Collection[str],
    attempted: datetime | None,
    day: date | None = None,
) -> bool:
    """Whether the scheduler starts `job` for `day` (default today) at `now`: a date it runs on;
    today at or after its time and before the end of its window, or the night before's carry-over
    (`carries_over`); every `after` job succeeded for `day`; and not attempted within its
    `retry_after`."""
    today = trade_date_of(now)
    day = today if day is None else day
    if job.name in succeeded or not runs_on(job, day, trading):
        return False
    if day == today:
        local = now.astimezone(IST).time()
        if local < job.at or (job.until is not None and local >= job.until):
            return False
    elif not carries_over(job, day, now):
        return False
    if any(name not in succeeded for name in job.after):
        return False
    return attempted is None or now - attempted >= job.retry_after


def overdue(
    job: ScheduledJob,
    day: date,
    now: datetime,
    *,
    trading: TradingDay,
    succeeded: bool,
    running: bool,
) -> str | None:
    """`job.missed` or `job.running_long` when `job`'s run of `day` is past its deadline (`at`
    on `day` plus `deadline`, IST) without a success; None when it is not due on `day`, not yet
    late or succeeded. Running at the deadline is `job.running_long` (a hung body), anything
    else (never started, failed and waiting for a retry) is `job.missed`."""
    if succeeded or not runs_on(job, day, trading):
        return None
    if now < datetime.combine(day, job.at, tzinfo=IST) + job.deadline:
        return None
    return JOB_RUNNING_LONG if running else JOB_MISSED


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
        clock: Clock = SYSTEM_CLOCK,
        *,
        trading: TradingDay | None = None,
        notifier: Notifier | None = None,
        watch: Watch | None = None,
    ) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate scheduled job name")
        self._jobs = tuple(jobs)
        self._session_factory = session_factory
        self._clock = clock
        self._trading: TradingDay = trading or (lambda _day: True)
        self._notifier: Notifier = notifier or LogNotifier()
        self._watch = watch
        self._alerted: set[tuple[str, date]] = set()
        self._attempted: dict[tuple[str, date], datetime] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._running_day: dict[str, date] = {}
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        # One thread per job, apart from the loop's default pool that other work shares.
        self._threads = ThreadPoolExecutor(
            max_workers=max(1, len(self._jobs)), thread_name_prefix="hat-job"
        )

    def _factory(self) -> SessionFactory:
        if self._session_factory is None:
            from app.database import HatSessionLocal  # deferred: importing it connects the engine

            self._session_factory = HatSessionLocal
        return self._session_factory

    def due_jobs(
        self, now: datetime, succeeded: Mapping[date, Collection[str]]
    ) -> list[tuple[ScheduledJob, date]]:
        """The jobs to start at `now` that are not already running, each with its trade date:
        today's, then the night before's carry-over. `succeeded` holds the jobs that succeeded
        for each of those dates."""
        today = trade_date_of(now)
        found: list[tuple[ScheduledJob, date]] = []
        for day in (today, today - ONE_DAY):
            for job in self._jobs:
                if job.name in self._running or any(job is j for j, _ in found):
                    continue
                if due(
                    job,
                    now,
                    trading=self._trading,
                    succeeded=succeeded.get(day, ()),
                    attempted=self._attempted.get((job.name, day)),
                    day=day,
                ):
                    found.append((job, day))
        return found

    async def _run(self, job: ScheduledJob, trade_date: date, started: datetime) -> None:
        succeeded = False
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
            succeeded = outcome.status is RunStatus.SUCCEEDED
            if outcome.status is RunStatus.FAILED and not self._stopping:
                await asyncio.to_thread(
                    self._alert, job.name, trade_date, JOB_FAILED, outcome.error or "job_failed"
                )
        except Exception:
            logger.exception("scheduled job could not run", extra={"job": job.name})
        finally:
            # the retry waits `retry_after` from the end of a failed run, not from its start
            ended = started + ONE_DAY if succeeded else utc_now(self._clock)
            self._attempted[(job.name, trade_date)] = ended
            self._running.pop(job.name, None)
            self._running_day.pop(job.name, None)

    async def tick(self) -> list[asyncio.Task[None]]:
        """Start every due job as its own task; return the tasks started."""
        now = utc_now(self._clock)
        today = trade_date_of(now)
        days = (today, today - ONE_DAY)
        succeeded = {
            day: await asyncio.to_thread(succeeded_on, self._factory(), day) for day in days
        }
        started = []
        for job, trade_date in self.due_jobs(now, succeeded):
            self._attempted[(job.name, trade_date)] = now
            task = asyncio.get_running_loop().create_task(self._run(job, trade_date, now))
            self._running[job.name] = task
            self._running_day[job.name] = trade_date
            started.append(task)
        await asyncio.to_thread(self._sweep, now, succeeded)
        if self._watch is not None:
            try:
                await asyncio.to_thread(self._watch, now)
            except Exception:
                logger.exception("scheduler watch failed")
        return started

    def note_failed(self, name: str, day: date, ended: datetime) -> None:
        """A run of `name` for `day` failed outside the scheduler at `ended` (the restart's
        arming of the day): its retry waits the job's `retry_after`, as a scheduled failure's."""
        self._attempted[(name, day)] = ended

    def alert_failed(self, name: str, day: date, error: str | None) -> None:
        """`job.failed` for a run of `name` that failed outside the scheduler (the restart's
        restore of the day's bars, or its arming of the day), once, as for a scheduled run."""
        self._alert(name, day, JOB_FAILED, error)

    def _sweep(self, now: datetime, succeeded: Mapping[date, Collection[str]]) -> None:
        """One alert for each job past its deadline today or for the night before (`overdue`)."""
        today = trade_date_of(now)
        self._alerted = {key for key in self._alerted if key[1] >= today - ONE_DAY}
        for day in (today, today - ONE_DAY):
            for job in self._jobs:
                at = timedelta(hours=job.at.hour, minutes=job.at.minute)
                if day != today and at + job.deadline < ONE_DAY:
                    continue  # a deadline before midnight was the night before's to sweep
                done = succeeded.get(day, ())
                code = overdue(
                    job,
                    day,
                    now,
                    trading=self._trading,
                    succeeded=job.name in done,
                    running=self._running_since(job, day),
                )
                if code == JOB_MISSED and any(name not in done for name in job.after):
                    continue  # it waits on a job that has not succeeded: that one is alerted
                if code is not None:
                    self._alert(job.name, day, code, None)

    def _running_since(self, job: ScheduledJob, day: date) -> bool:
        """Whether `job` runs for `day` and started before its deadline: one started after it
        (a late process) has missed it, not hung."""
        started = self._attempted.get((job.name, day))
        deadline = datetime.combine(day, job.at, tzinfo=IST) + job.deadline
        return self._running_day.get(job.name) == day and started is not None and started < deadline

    def _alert(self, name: str, day: date, code: str, error: str | None) -> None:
        """Send `code` for (name, day) unless it was alerted before: in this process, or on the
        run row (`alerted_at`, claimed by one conditional update, so a restart or a second
        failure of the retry sends nothing more). Never raises."""
        key = (name, day)
        if key in self._alerted:
            return
        try:
            with self._factory()() as session:
                run = select(HatJobRun.id).where(HatJobRun.job == name, HatJobRun.trade_date == day)
                if session.scalar(run) is not None:
                    claimed = session.execute(
                        update(HatJobRun)
                        .where(
                            HatJobRun.job == name,
                            HatJobRun.trade_date == day,
                            HatJobRun.alerted_at.is_(None),
                        )
                        .values(alerted_at=utc_now(self._clock))
                    )
                    session.commit()
                    if claimed.rowcount != 1:
                        self._alerted.add(key)
                        return  # an earlier process sent it
            self._alerted.add(key)
            detail = {"job": name} if error is None else {"job": name, "error": error}
            self._notifier.alert(code, day, detail)
        except Exception:
            logger.exception("job alert %s for %s not sent", code, name)

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

    def begin_stop(self) -> None:
        """From now on a failed run is not alerted: the stop ends long bodies by failing them."""
        self._stopping = True

    async def stop(self, grace: float = STOP_GRACE_SECONDS) -> list[str]:
        """Stop starting jobs and wait up to `grace` seconds for running ones (the runtime tells
        long bodies to end first). A body still running then is left to its thread and logged by
        name; the desks' sessions are released next, so it sends nothing more, and the next start
        reaps its run as interrupted (`recover_interrupted`). Returns the jobs left running."""
        self.begin_stop()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._running:
            await asyncio.wait(list(self._running.values()), timeout=grace)
        abandoned = sorted(name for name, run in self._running.items() if not run.done())
        for name in abandoned:
            logger.warning("job still running at stop: abandoned", extra={"job": name})
        self._threads.shutdown(wait=False)
        return abandoned
