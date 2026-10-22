"""One trading desk per active broker account (PR 17; 07 §2-§6, 10-ops §1 HAT-OPS-003).

A desk holds the account's one broker session (`SessionRegistry`: one owner per account, and
every broker call of the desk goes through it), its `ExecutionGuard` (no entry is placed before
the trade date reconciled; stops and sales never wait, D118), the reconciliation, the execution
router (signing approval cards with the Telegram signer), the stop desk, the fill sync and the
`TradeManager`. The live exits, the close job, the GTT jobs and the Telegram flatten all use the
same `TradeManager`, whose per-position sale lock serialises their sales.

`sync_due` reads an account's fills every periodic step while one of its real entries works or a
stop's answer is not in yet, and once a minute otherwise, so an entry fill gets its broker stop
within one step (HAT-EXEC-060).

`recover` is the restart order of HAT-OPS-003: reconcile each account and read its fills (which
re-protects a fill without a working stop and re-sends a lost stop by tag). The auto entries a
crash left in Triggered are placed again by `resume_entries`, only once the live feed is fresh
and the account has reconciled (idempotent by tag; refused when the conditions no longer hold,
and an entry of an earlier session ends Rejected).
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import Clock
from ..engine.trades import TokenIssuer
from ..execution.broker import Capabilities, HatBroker, SessionStatus
from ..execution.fills import FillSync, SaleBand, watch_closely
from ..execution.guard import ExecutionGuard
from ..execution.manage import TradeManager, ViewSource
from ..execution.protect import StopDesk
from ..execution.reconcile import ReconciliationService, notifier_sink
from ..execution.router import ExecutionRouter, PlacementCheck, unplaced_entry
from ..execution.session import (
    SessionOwner,
    SessionRegistry,
    SessionWatch,
    broker_health_failing,
    record_session,
    refresh_capabilities,
)
from ..models import HatApproval, HatBot, HatBrokerAccount, HatPosition, HatSignal
from ..notify import LogNotifier, Notifier
from ..ops.jobs import JobResult, SessionFactory
from ..ops.scheduler import ScheduledJob
from ..ops.trade_jobs import job_name, trade_jobs
from ..risk.controls import sync_health
from ..risk.sessions import SessionCalendar
from ..time import trade_date_of, utc_now

__all__ = [
    "END_SESSION",
    "END_SESSION_AT",
    "RECONCILE_FAILED",
    "AccountDesk",
    "Desks",
    "account_jobs",
]

logger = logging.getLogger(__name__)

END_SESSION = "trade.end_session"
END_SESSION_AT = time(15, 31)  # once the DAY entries can no longer fill
RECONCILE_FAILED = "reconcile.failed"
BROKER_HEALTH = "broker_health"
ACTIVE = "active"
# The fill sync's cadence when nothing is watched closely (10-ops §3); the runtime's `sync` step
# runs every pass and leaves the cadence to `Desks.sync_due`.
FILL_SYNC_EVERY = timedelta(minutes=1)

BrokerFor = Callable[[HatBrokerAccount], HatBroker | None]
TickFor = Callable[[Session, HatPosition], Decimal | None]
# (session, bot, instrument, now): the placement check, the instrument's live price in it
CheckFor = Callable[[Session, uuid.UUID, uuid.UUID, datetime], PlacementCheck]


class OwnedBroker:
    """An account's broker whose every call is made through the account's session owner
    (trading-safety rule 9): the router, the stop desk, the fill sync, the reconciliation and the
    jobs run on different threads and never call one account's broker at once."""

    def __init__(self, owner: SessionOwner, broker: HatBroker) -> None:
        self._owner = owner
        self._broker = broker

    def capabilities(self) -> Capabilities:
        """A local value, no broker call: not serialised, so a slow call in flight never holds
        up a placement's capability check (HAT-EXEC-012)."""
        return self._broker.capabilities()

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._broker, name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        def call(*args: object, **kwargs: object) -> object:
            return self._owner.call(lambda: attribute(*args, **kwargs))

        return call


@dataclass
class AccountDesk:
    account_id: uuid.UUID
    owner: SessionOwner
    broker: HatBroker  # an `OwnedBroker`
    guard: ExecutionGuard
    reconciler: ReconciliationService
    router: ExecutionRouter
    manager: TradeManager
    notifier: Notifier = field(default_factory=LogNotifier)
    watch: SessionWatch = field(default_factory=SessionWatch)
    failed_alerted_on: date | None = None  # the trade date `reconcile.failed` was raised for
    synced_at: datetime | None = None  # the last fill sync that read the broker's books

    def check_session(self, session: Session, now: datetime) -> bool:
        """Write today's `hat_broker_session` from one read of the broker's status (the router
        refuses an entry without a live one), raise `broker.session_lost` once an outage has
        lasted two minutes (HAT-OPS-031), and trip or clear the account's bots' `broker_health`
        kill (HAT-RISK-063; a check that failed counts as not live). True when the session is
        live."""
        live = False
        try:
            status = self.broker.session_status()
            live = status is SessionStatus.LIVE
            step = self.watch.observe(now, live)
            record_session(
                session,
                self.broker,
                self.account_id,
                trade_date_of(now),
                lost_alert=step.alert,
                now=now,
                status=status,
            )
        except Exception:
            session.rollback()
            logger.exception("session check of account %s failed", self.account_id)
            live = False
        self._sync_broker_health(session, now, live)
        return live

    def _sync_broker_health(self, session: Session, now: datetime, live: bool) -> None:
        """Each of the account's bots is manage-only while its session is down between 09:05
        and the close, and clears by itself at the next check that is not (HAT-RISK-063). Per
        bot, not the account switch: one account's live session never clears another's trip."""
        failing = broker_health_failing(now, live)
        try:
            bots = list(
                session.scalars(
                    select(HatBot.id)
                    .where(HatBot.broker_account_id == self.account_id)
                    .order_by(HatBot.id)
                )
            )
            for bot_id in bots:
                sync_health(
                    session,
                    scope="bot",
                    bot_id=bot_id,
                    failing=failing,
                    reason=BROKER_HEALTH,
                    now=now,
                )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("broker health of account %s not updated", self.account_id)

    def reconcile(self, session: Session, now: datetime) -> bool:
        """Reconcile now; True when the account's trade date is reconciled (its entries may be
        placed). Until the first success of a trade date its entries wait (HAT-BRK-110), and a
        failure then raises one `reconcile.failed` health alert for the date; a failed routine
        re-run after a success leaves the gate open. Stops and sales never wait (D118)."""
        try:
            self.reconciler.run(session, self.account_id)
        except Exception:
            logger.exception("reconciliation of account %s failed", self.account_id)
            day = trade_date_of(now)
            if not self.guard.reconciled_today() and self.failed_alerted_on != day:
                self.failed_alerted_on = day
                self.notifier.alert(RECONCILE_FAILED, day, {"account_id": str(self.account_id)})
            return False
        return self.guard.reconciled_today()


class Desks:
    """The desks of the active broker accounts, built once at start."""

    def __init__(
        self,
        session_factory: SessionFactory,
        broker_for: BrokerFor,
        notifier: Notifier,
        *,
        issuer: TokenIssuer | None,
        calendar: Callable[[], SessionCalendar | None],
        tick_for: TickFor,
        today: Callable[[], date] | None = None,
        band: SaleBand | None = None,
        resend: bool = False,
        page_after: int = 3,
    ) -> None:
        self._session_factory = session_factory
        # PR 48: the day's lower band for sales, and `HAT_MPP_RESEND` with its page threshold
        self._band = band
        self._resend = resend
        self._page_after = page_after
        self._today = today  # the guards' trade date (the wall clock's when None)
        self._registry = SessionRegistry()
        self.by_account: dict[uuid.UUID, AccountDesk] = {}
        # the entries a crash left unplaced (auto in Triggered, a Take not yet placed) and those
        # whose placement raised, per account, until `resume_entries` (HAT-OPS-003, PR 52)
        self._resume: dict[uuid.UUID, list[uuid.UUID]] = {}
        with session_factory() as session:
            accounts = list(
                session.scalars(
                    select(HatBrokerAccount)
                    .where(HatBrokerAccount.status == ACTIVE)
                    .order_by(HatBrokerAccount.id)
                )
            )
            for account in accounts:
                broker = broker_for(account)
                if broker is None:
                    continue
                # HAT-BRK-092: the stored capabilities are the adapter's from the start, before
                # any routing reads them (the session checks keep them so)
                refresh_capabilities(session, broker, account.id)
                self.by_account[account.id] = self._desk(
                    account.id, broker, notifier, issuer, calendar(), tick_for
                )

    def _desk(
        self,
        account_id: uuid.UUID,
        broker: HatBroker,
        notifier: Notifier,
        issuer: TokenIssuer | None,
        calendar: SessionCalendar | None,
        tick_for: TickFor,
    ) -> AccountDesk:
        owner = self._registry.acquire(account_id)
        broker = cast(HatBroker, OwnedBroker(owner, broker))
        guard = ExecutionGuard() if self._today is None else ExecutionGuard(today=self._today)
        reconciler = ReconciliationService(broker, guard, notifier_sink(notifier), notifier)
        stops = StopDesk(broker, guard, tick_for=tick_for, calendar=calendar, notifier=notifier)
        fills = FillSync(
            broker,
            stops,
            notifier,
            band=self._band,
            resend=self._resend,
            page_after=self._page_after,
        )
        manager = TradeManager(
            broker,
            guard,
            stops,
            fills,
            calendar=calendar,
            notifier=notifier,
            band=self._band,
            resend=self._resend,
        )
        router = ExecutionRouter(broker, guard, reconciler, notifier, issuer=issuer)
        return AccountDesk(
            account_id,
            owner,
            broker,
            guard,
            reconciler,
            router,
            manager,
            notifier,
        )

    def router(self, account_id: uuid.UUID) -> ExecutionRouter | None:
        desk = self.by_account.get(account_id)
        return None if desk is None else desk.router

    def managers(self) -> list[tuple[uuid.UUID, TradeManager]]:
        return [(d.account_id, d.manager) for d in self.by_account.values()]

    def managed(self) -> set[uuid.UUID]:
        return set(self.by_account)

    def use_calendar(self, calendar: SessionCalendar | None) -> None:
        """Hand every desk the calendar the runtime has just read: a desk built before the
        calendar existed, or across a new year, would otherwise never see a session open."""
        for desk in self.by_account.values():
            desk.manager.use_calendar(calendar)

    def release(self) -> None:
        for desk in self.by_account.values():
            desk.owner.release()

    # --- restart recovery (HAT-OPS-003) ------------------------------------------------------

    def recover(self, now: datetime) -> None:
        """Reconcile every account and read its fills (the fill sync re-protects a fill without a
        working stop whether or not the reconciliation succeeded, D118); remember the entries a
        crash left unplaced (an auto signal in Triggered, a Take not yet placed: PR 52) for
        `resume_entries`. An account that did not
        reconcile places no entry until a later reconciliation succeeds. One account's failure is
        logged and never stops another's recovery or the start: its fills are read again by the
        next periodic sync."""
        for account_id, desk in self.by_account.items():
            try:
                with self._session_factory() as session:
                    self._resume[account_id] = _left_unplaced(session, account_id)
                    desk.reconcile(session, now)
                    if desk.manager.sync(session, account_id, now) is not None:
                        desk.synced_at = now
            except Exception:
                logger.exception("recovery of account %s failed", account_id)

    def resume_entries(
        self, now: datetime, check: CheckFor, feed_stale: Callable[[], bool]
    ) -> None:
        """Place again the entries `recover` found and those whose placement raised since (PR
        52), once the live feed is fresh (no feed heard since the restart reads as stale:
        HAT-DATA-116) and the account has reconciled. Each is placed once, under the live
        conditions read now; the router ends one of an earlier session Rejected."""
        for account_id, desk in self.by_account.items():
            stranded = desk.router.take_stranded()
            if stranded:
                waiting = self._resume.setdefault(account_id, [])
                waiting.extend(s for s in stranded if s not in waiting)
        if not self._resume or feed_stale():
            return
        for account_id in list(self._resume):
            desk = self.by_account[account_id]
            if not desk.guard.reconciled_today():
                continue
            with self._session_factory() as session:
                for signal_id in self._resume.pop(account_id):
                    self._resume_one(session, desk, signal_id, now, check)

    @staticmethod
    def _resume_one(
        session: Session, desk: AccountDesk, signal_id: uuid.UUID, now: datetime, check: CheckFor
    ) -> None:
        try:
            signal = session.get(HatSignal, signal_id)
            if signal is not None:
                bot_check = check(session, signal.bot_id, signal.instrument_id, now)
                desk.router.place_entry(session, signal_id, now=now, check=bot_check)
        except Exception:
            session.rollback()
            desk.router.remember_stranded(signal_id)  # tried again at the next resume
            logger.exception("resuming the entry of signal %s failed", signal_id)

    # --- the periodic steps -------------------------------------------------------------------

    def sync_due(self, now: datetime) -> None:
        """The periodic fill sync: each account `watch_closely` names at every step, every other
        account once a minute (HAT-EXEC-060). A sync that could not read the books is tried
        again at the next step; one that failed otherwise waits its minute; one account's failure
        never holds up another's."""
        for account_id, desk in self.by_account.items():
            try:
                with self._session_factory() as session:
                    last = desk.synced_at
                    quiet = last is not None and now - last < FILL_SYNC_EVERY
                    if quiet and not watch_closely(session, account_id, now):
                        continue
                    if desk.manager.sync(session, account_id, now) is not None:
                        desk.synced_at = now
            except Exception:
                desk.synced_at = now
                logger.exception("fill sync of account %s failed", account_id)

    def expire_all(self, now: datetime) -> int:
        """The approval expiry sweep (`ApprovalsDecided`); one sweep serves every account."""
        desk = next(iter(self.by_account.values()), None)
        if desk is None:
            return 0
        with self._session_factory() as session:
            return desk.router.expire(session, now)

    def watch_sessions(self, now: datetime) -> None:
        for desk in self.by_account.values():
            with self._session_factory() as session:
                desk.check_session(session, now)

    def reconcile_all(self, now: datetime) -> None:
        for desk in self.by_account.values():
            with self._session_factory() as session:
                desk.reconcile(session, now)


def _left_unplaced(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
    """The account's real entries committed but not placed: an auto signal in Triggered, or a
    confirm signal whose Take passed (its decision is `take` while it still awaits placement)."""
    return list(
        session.scalars(
            select(HatSignal.id)
            .join(HatBot, HatBot.id == HatSignal.bot_id)
            .outerjoin(HatApproval, HatApproval.signal_id == HatSignal.id)
            .where(
                HatBot.broker_account_id == account_id,
                unplaced_entry(),
            )
            .order_by(HatSignal.id)
        )
    )


def account_jobs(
    desks: Desks,
    session_factory: SessionFactory,
    views: ViewSource,
    clock: Clock,
    *,
    exdate_check: bool = False,
) -> tuple[ScheduledJob, ...]:
    """Each account's `exits.close`, `gtt.swap`, `gtt.verify` and `trade.end_session` jobs, and
    `gtt.ex_date` with `exdate_check` (PR 47); the names carry the account id, so two accounts
    never share a `hat_job_run` row."""
    jobs: list[ScheduledJob] = []
    for account_id, manager in desks.managers():
        jobs.extend(
            trade_jobs(
                manager, session_factory, account_id, views, clock.now, exdate_check=exdate_check
            )
        )

        def end(_day: date, m: TradeManager = manager, a: uuid.UUID = account_id) -> JobResult:
            with session_factory() as session:
                ended = m.end_session(session, a, utc_now(clock))
                session.commit()
            return JobResult(rows=ended)

        jobs.append(
            ScheduledJob(
                job_name(END_SESSION, account_id),
                END_SESSION_AT,
                end,
                retry_after=timedelta(minutes=5),
            )
        )
    return tuple(jobs)
