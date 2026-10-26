"""The HAT runtime the lifespan starts when `HAT_ENABLED` is true (PR 17).

With HAT disabled `HatRuntime.start` starts nothing (HAT-OPS-001). Enabled, it refuses a database
that is not PostgreSQL (HAT-OPS-002) and a bad Telegram configuration (HAT-TG-040) before any
task exists, then takes the runtime lock (`ops/runtime_lock.py`: one runtime per database; a
second one refuses to start) and starts in the restart-safe order of HAT-OPS-003:

1. reap the job runs a previous process left running, and read the trading calendar;
2. build the assembly (`daily/assembly.py`): one desk per active broker account, reconciled
   before anything is placed, its fills read (a fill without a working stop is protected) and
   the auto entries left in Triggered noted: the periodic housekeeping places them once the feed
   is fresh (no tick since the start reads as stale) and the account has reconciled;
3. on a trading day from 08:45, restore the day's stored bars (`live.restore`, a failure alerted
   as `job.failed` and shown on the dashboard's job list), then arm the day (watchlist
   candidates, subscriptions, keys already fired) even when the restore failed (`after_restore`);
4. attach the live data to the feed and start the feed;
5. start the scheduler with every job of the daily cycle, the periodic loops (`daily/periodic.py`:
   exits, housekeeping, announcements) and the Telegram poller and relay.

`stop` ends them in reverse, each step even if another failed, and releases the lock last. A
start that fails stops through its caller (the lifespan's shutdown); a cancelled one (or an
interrupt) stops itself, and a lock its worker thread takes after that is released at once.
While it runs, the housekeeping checks the lock's connection every minute (`watch_lock`); a
runtime whose lock another runtime took writes no order from then on (`Assembly.halt`).
`assembly` is None until the assembly is built; a start that failed before then stops only the
feed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

from ..clock import SYSTEM_CLOCK, Clock
from ..logs import configure_hat_logging
from ..ops.jobs import (
    JobError,
    JobResult,
    RunStatus,
    SessionFactory,
    recover_interrupted,
    reopen,
    run_job,
)
from ..ops.runtime_lock import RuntimeLock, RuntimeLockHeldError
from ..settings import HatSettings, require_postgres
from ..telegram.service import run_relay
from ..time import IST, trade_date_of, utc_now
from .assembly import Assembly, Feed, RuntimeParts, TradingCalendar, build
from .intraday import LIVE_PREPARE, PREPARE_AT
from .periodic import Periodic, watch_sessions

__all__ = ["LIVE_RESTORE", "LOCK_LOST", "HatRuntime", "RestoreDecision", "after_restore"]

logger = logging.getLogger(__name__)

STOPPING = "hat_stopping"
LIVE_RESTORE = "live.restore"  # a restart's restore of the day's bars, in the job ledger
RESTORE_FAILED = "restore_failed"
LOCK_LOST = "runtime.lock_lost"  # the alert: the runtime lock is gone and cannot be taken again


@dataclass(frozen=True)
class RestoreDecision:
    alert: bool  # send `job.failed` for `live.restore`
    arm: bool  # arm the day now, before the feed starts


def after_restore(status: RunStatus) -> RestoreDecision:
    """What a restart does after restoring the day's bars. A failed restore is alerted, and
    the day is armed anyway: without the stored bars the bar builder treats the restart as a
    late subscription and fails closed (no symbol is watched from the open, so no opening range
    and no day low, and the day volume counts only from the restart, which can only lower the
    volume pace), so the arming gives what a morning `live.prepare` gives with fewer triggers,
    never a trigger on missing prices. Arming here, before the feed starts, also keeps
    `live.prepare` off the live feed."""
    return RestoreDecision(alert=status is RunStatus.FAILED, arm=True)


class HatRuntime:
    def __init__(
        self,
        settings: HatSettings,
        session_factory: SessionFactory,
        *,
        dialect_name: str,
        parts: Callable[[], RuntimeParts],
        runtime_lock: Callable[[], RuntimeLock],
        clock: Clock = SYSTEM_CLOCK,
        data_session_factory: SessionFactory | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        # the nightly data jobs' sessions, the only ones that may write hat_live
        self._data_session_factory = data_session_factory or session_factory
        self._dialect_name = dialect_name
        self._parts = parts
        self._runtime_lock = runtime_lock
        self._lock: RuntimeLock | None = None  # held from start to the end of stop
        self._lock_guard = threading.Lock()  # the lock's worker thread against stop
        self._lock_dropped = False  # stopped: a lock taken from now on is released at once
        self._lock_alerted = False
        self.clock = clock
        self._halt = threading.Event()  # long job bodies (the ingest's wait) end on it
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self.calendar = TradingCalendar(session_factory)
        self._feed: Feed | None = None  # stopped on stop once the parts exist
        self.assembly: Assembly | None = None
        self.started = False

    def _now(self) -> datetime:
        return utc_now(self.clock)

    def _sleep(self, seconds: float) -> None:
        """The ingest's wait: ends with a `JobError` as soon as HAT stops."""
        if self._halt.wait(seconds):
            raise JobError(STOPPING)

    # --- start ----------------------------------------------------------------------------

    async def start(self) -> bool:
        """Start HAT; False (and nothing started) when HAT is disabled. Raises `HatConfigError`
        for a non-Postgres database, a proxy that would carry Fyers traffic (`fyers_proxy_set`)
        or a bad Telegram configuration, and `RuntimeLockHeldError` when another runtime runs on
        this database, before any task exists."""
        if not self.settings.enabled:
            logger.info("HAT_ENABLED is false: no HAT task is started")
            return False
        require_postgres(self._dialect_name)
        try:
            await self._start()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                # cancelled (or interrupted): the caller's `except Exception` does not stop it,
                # so stop here, shielded from a second cancel
                try:
                    await asyncio.shield(asyncio.ensure_future(self.stop()))
                finally:
                    # a second cancel ends this wait while the stop runs on detached, and the
                    # loop may close before it reaches the lock: release it now (one connection
                    # closed; after a finished stop a no-op, and a lock the worker thread takes
                    # later is released by it)
                    self._drop_lock()
            raise
        self.started = True
        logger.info("HAT runtime started")
        return True

    async def _start(self) -> None:
        # before anything reads or reaps: a second runtime must not touch the first one's jobs
        await asyncio.to_thread(self._take_lock)
        # reads the Telegram settings (a bad one refuses the start) and builds the HTTP clients
        parts = await asyncio.to_thread(self._parts)
        self._feed = parts.feed
        configure_hat_logging()
        reaped = await asyncio.to_thread(recover_interrupted, self._session_factory)
        if reaped:
            logger.warning("recovered %d interrupted job runs", reaped)
        await asyncio.to_thread(self.calendar.read)
        # the desks read the accounts and build the brokers: blocking, so off the loop
        assembly = await asyncio.to_thread(
            build,
            parts,
            self.settings,
            self._session_factory,
            self.clock,
            asyncio.get_running_loop(),
            self.calendar,
            sleep=self._sleep,
            data_session_factory=self._data_session_factory,
        )
        self.assembly = assembly
        await self._recover(assembly)
        assembly.live.attach(parts.feed)
        parts.feed.start(asyncio.get_running_loop(), prevalidated_token=parts.prevalidated_token)
        self._start_tasks(assembly)

    # --- the runtime lock (blocking: each runs off the loop) -------------------------------

    def _take_lock(self) -> None:
        """Take the runtime lock; once stopped (a cancelled start), release it at once."""
        lock: RuntimeLock | None = self._runtime_lock()
        with self._lock_guard:
            if not self._lock_dropped:
                self._lock, lock = lock, None
        if lock is not None:
            lock.release()

    def _drop_lock(self) -> None:
        with self._lock_guard:
            self._lock_dropped = True
            lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    def watch_lock(self, now: datetime) -> None:
        """The housekeeping's check of the lock: when its connection is gone (a Postgres restart,
        a terminated backend) take it again on a new one. When that fails, set `lock_lost`
        (no new entry until a restart) and alert `runtime.lock_lost` once. When it fails because
        another runtime holds the lock (`RuntimeLockHeldError`), that runtime owns the accounts:
        halt this one (`Assembly.halt`: no job, no periodic order step, the desks released, so it
        sends no stop or sale beside the other, audit A3 F8) and stop watching. Any other failure
        (the database still unreachable, so nothing here can reserve an order either) is tried
        again at the next check."""
        assembly = self.assembly
        if assembly is not None and assembly.halted.is_set():
            return  # halted: never take the lock back from the runtime that holds it
        with self._lock_guard:
            lock = self._lock
        if lock is None or lock.alive():
            return
        lock.release()
        try:
            self._take_lock()
        except Exception as exc:
            reason, held = type(exc).__name__, isinstance(exc, RuntimeLockHeldError)
        else:
            logger.warning("HAT runtime lock connection was lost; the lock is taken again")
            return
        if assembly is not None:
            assembly.lock_lost.set()
            if held:
                assembly.halt()
        logger.error(
            "HAT runtime lock lost and not taken again (%s): no new entry%s",
            reason,
            ", no order (another runtime holds it)" if held else "",
        )
        if assembly is None or (self._lock_alerted and not held):
            return
        self._lock_alerted = True
        try:
            assembly.notifier.alert(
                LOCK_LOST, trade_date_of(now), {"reason": reason, "halted": held}
            )
        except Exception:
            logger.exception("alert %s not sent", LOCK_LOST)

    async def _recover(self, assembly: Assembly) -> None:
        """HAT-OPS-003: reconcile and read fills (the entries left in Triggered wait for a fresh
        feed: `resume_entries`), then arm a day already under way (a restart after 08:45 on a
        trading day, moved with a session that opens at another time): restore the day's bars,
        then arm it, even when the restore failed (`after_restore`)."""
        now = self._now()
        await asyncio.to_thread(watch_sessions, assembly, now)
        await asyncio.to_thread(assembly.desks.recover, now)
        today = trade_date_of(now)
        prepare_at = datetime.combine(today, PREPARE_AT, tzinfo=IST)
        if self.calendar.trading(today) and now >= prepare_at + assembly.intraday.open_shift(today):
            # before the feed starts (the bar builder is the loop thread's once it does): the
            # bars stored before the restart give back the opening ranges and day volumes
            status, error = await asyncio.to_thread(self._restore, assembly, today)
            decision = after_restore(status)
            if decision.alert:
                await asyncio.to_thread(
                    assembly.scheduler.alert_failed, LIVE_RESTORE, today, error or RESTORE_FAILED
                )
            if decision.arm:
                await asyncio.to_thread(self._arm, assembly, today)

    def _restore(self, assembly: Assembly, today: date) -> tuple[RunStatus, str | None]:
        """Restore the day's stored bars into the bar builder, through the job ledger (reopened
        first: each process restores into its own builder), so a failure is on the dashboard's
        job list as `live.restore` failed."""
        sf = self._session_factory
        try:
            reopen(LIVE_RESTORE, today, sf)
            outcome = run_job(
                LIVE_RESTORE,
                today,
                lambda: JobResult(rows=assembly.live.restore(today)),
                session_factory=sf,
            )
        except Exception:  # the ledger itself failed (the database)
            logger.exception("restoring the day's bars on restart failed")
            return RunStatus.FAILED, None
        if outcome.status is RunStatus.FAILED:
            logger.error("restoring the day's bars on restart failed: %s", outcome.error)
        return outcome.status, outcome.error

    def _arm(self, assembly: Assembly, today: date) -> None:
        """Arm a day under way once, through the job ledger, so the scheduler finds
        `live.prepare` succeeded and does not run it again on the live feed (wiping the prices
        and swapping the trigger monitor). A run an earlier process recorded armed only that
        process, so it is reopened and run again here. A failed run is the scheduler's to
        retry after the job's `retry_after`, as a scheduled failure is, and alerted as one;
        until then the day stays unarmed (no entries), exits still run."""
        sf = self._session_factory
        try:
            reopen(LIVE_PREPARE, today, sf)
            outcome = run_job(
                LIVE_PREPARE, today, lambda: assembly.intraday.prepare(today), session_factory=sf
            )
        except Exception:
            logger.exception("arming the day on restart failed")
            return
        if outcome.status is RunStatus.FAILED:
            logger.warning("arming the day on restart failed: %s", outcome.error)
            assembly.scheduler.note_failed(LIVE_PREPARE, today, self._now())
            assembly.scheduler.alert_failed(LIVE_PREPARE, today, outcome.error)

    def _start_tasks(self, assembly: Assembly) -> None:
        assembly.scheduler.start()
        loop = asyncio.get_running_loop()
        for work in (
            assembly.live.run(),
            *Periodic(assembly, self.clock, watch_lock=self.watch_lock).loops(self._stop),
            assembly.telegram.run(self._stop),
            run_relay(assembly.relay, self._stop),
        ):
            self._tasks.append(loop.create_task(work))

    # --- stop -----------------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop in reverse order; each step runs even if an earlier one failed."""
        if self.assembly is not None:
            self.assembly.scheduler.begin_stop()  # the halt fails the ingest's wait: not an alert
        self._halt.set()
        self._stop.set()
        steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("tasks", self._stop_tasks),
            ("scheduler", self._stop_scheduler),
            ("feed", self._stop_feed),
            ("desks", self._release),
            ("lock", self._release_lock),
        ]
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                logger.warning("HAT stop step %s failed: %s", name, type(exc).__name__)
        self.started = False

    async def _stop_tasks(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _stop_scheduler(self) -> None:
        if self.assembly is not None:
            await self.assembly.scheduler.stop()

    async def _stop_feed(self) -> None:
        """Detach, release the subscriptions and stop the feed; after a start that failed part
        way the feed is still stopped."""
        if self._feed is None:
            return
        if self.assembly is not None:
            for name, step in (
                ("live", self.assembly.live.detach),
                ("subscriptions", self.assembly.subscriptions.release_all),
            ):
                try:
                    step()
                except Exception as exc:
                    logger.warning("HAT stop step %s failed: %s", name, type(exc).__name__)
        await self._feed.stop()

    async def _release_lock(self) -> None:
        await asyncio.to_thread(self._drop_lock)

    async def _release(self) -> None:
        if self.assembly is not None:
            await asyncio.to_thread(self.assembly.desks.release)  # closes broker sessions
