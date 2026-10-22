"""One trading desk per active broker account (PR 17; 07 §2-§6, 10-ops §1 HAT-OPS-003).

A desk holds the account's one broker session (`SessionRegistry`: one owner per account, and
every broker call of the desk goes through it), its `ExecutionGuard` (nothing is placed before
the session reconciled), the reconciliation, the execution router (signing approval cards with
the Telegram signer), the stop desk, the fill sync and the `TradeManager`. The live exits, the
close job, the GTT jobs and the Telegram flatten all use the same `TradeManager`, whose
per-position sale lock serialises their sales.

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
from ..engine.lifecycle import TRIGGERED
from ..engine.trades import TokenIssuer
from ..execution.broker import HatBroker, SessionStatus
from ..execution.fills import FillSync
from ..execution.guard import ExecutionGuard
from ..execution.manage import TradeManager, ViewSource
from ..execution.modes import AUTO
from ..execution.protect import StopDesk
from ..execution.reconcile import ReconciliationService, notifier_sink
from ..execution.router import ExecutionRouter, PlacementCheck
from ..execution.session import SessionOwner, SessionRegistry, SessionWatch, record_session
from ..models import HatBot, HatBrokerAccount, HatPosition, HatSignal
from ..notify import Notifier
from ..ops.jobs import JobResult, SessionFactory
from ..ops.scheduler import ScheduledJob
from ..ops.trade_jobs import job_name, trade_jobs
from ..risk.sessions import SessionCalendar
from ..time import trade_date_of, utc_now

__all__ = [
    "END_SESSION",
    "END_SESSION_AT",
    "AccountDesk",
    "Desks",
    "account_jobs",
]

logger = logging.getLogger(__name__)

END_SESSION = "trade.end_session"
END_SESSION_AT = time(15, 31)  # once the DAY entries can no longer fill
ACTIVE = "active"

BrokerFor = Callable[[HatBrokerAccount], HatBroker | None]
TickFor = Callable[[Session, HatPosition], Decimal | None]
CheckFor = Callable[[Session, uuid.UUID, datetime], PlacementCheck]


class OwnedBroker:
    """An account's broker whose every call is made through the account's session owner
    (trading-safety rule 9): the router, the stop desk, the fill sync, the reconciliation and the
    jobs run on different threads and never call one account's broker at once."""

    def __init__(self, owner: SessionOwner, broker: HatBroker) -> None:
        self._owner = owner
        self._broker = broker

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
    watch: SessionWatch = field(default_factory=SessionWatch)
    reconciled_on: date | None = None  # the trade date of the last successful reconciliation

    def check_session(self, session: Session, now: datetime) -> bool:
        """Write today's `hat_broker_session` from one read of the broker's status (the router
        refuses an entry without a live one) and raise `broker.session_lost` once an outage has
        lasted two minutes (HAT-OPS-031). True when the session is live."""
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
            return False
        return live

    def reconcile(self, session: Session, now: datetime) -> bool:
        """Reconcile now; True when the account may place orders. The first reconciliation of
        the process or of a trade date blocks every order until it succeeds (HAT-BRK-110: before
        the first order of each session); a routine re-run leaves the gate open (`run` never
        closes it under orders on other threads)."""
        day = trade_date_of(now)
        if self.reconciled_on != day:
            self.guard.begin_session()
        try:
            self.reconciler.run(session, self.account_id)
        except Exception:
            logger.exception("reconciliation of account %s failed", self.account_id)
            return False
        self.reconciled_on = day
        return self.guard.reconciled


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
    ) -> None:
        self._session_factory = session_factory
        self._registry = SessionRegistry()
        self.by_account: dict[uuid.UUID, AccountDesk] = {}
        # the auto entries a crash left in Triggered, per account, until `resume_entries`
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
        guard = ExecutionGuard()
        reconciler = ReconciliationService(broker, guard, notifier_sink(notifier))
        stops = StopDesk(broker, guard, tick_for=tick_for, calendar=calendar, notifier=notifier)
        fills = FillSync(broker, stops, notifier)
        manager = TradeManager(
            broker,
            guard,
            reconciler,
            stops,
            fills,
            calendar=calendar,
            notifier=notifier,
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
        """Reconcile every account and read its fills; remember the auto entries a crash left
        in Triggered for `resume_entries`. An account that did not reconcile places nothing
        until a later reconciliation succeeds."""
        for account_id, desk in self.by_account.items():
            with self._session_factory() as session:
                self._resume[account_id] = _left_triggered(session, account_id)
                if desk.reconcile(session, now):
                    desk.manager.sync(session, account_id, now)

    def resume_entries(
        self, now: datetime, check: CheckFor, feed_stale: Callable[[], bool]
    ) -> None:
        """Place again the entries `recover` found, once the live feed is fresh (no feed heard
        since the restart reads as stale: HAT-DATA-116) and the account has reconciled. Each is
        placed once, under the live conditions read now; the router ends one of an earlier
        session Rejected."""
        if not self._resume or feed_stale():
            return
        for account_id in list(self._resume):
            desk = self.by_account[account_id]
            if not desk.guard.reconciled:
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
                bot_check = check(session, signal.bot_id, now)
                desk.router.place_entry(session, signal_id, now=now, check=bot_check)
        except Exception:
            session.rollback()
            logger.exception("resuming the entry of signal %s failed", signal_id)

    # --- the periodic steps -------------------------------------------------------------------

    def sync_all(self, now: datetime) -> None:
        """Read each account's fills (every new fill is protected at once)."""
        for account_id, desk in self.by_account.items():
            with self._session_factory() as session:
                desk.manager.sync(session, account_id, now)

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


def _left_triggered(session: Session, account_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(HatSignal.id)
            .join(HatBot, HatBot.id == HatSignal.bot_id)
            .where(
                HatBot.broker_account_id == account_id,
                HatSignal.state == TRIGGERED,
                HatSignal.mode == AUTO,
            )
            .order_by(HatSignal.id)
        )
    )


def account_jobs(
    desks: Desks, session_factory: SessionFactory, views: ViewSource, clock: Clock
) -> tuple[ScheduledJob, ...]:
    """Each account's `exits.close`, `gtt.swap`, `gtt.verify` and `trade.end_session` jobs; the
    names carry the account id, so two accounts never share a `hat_job_run` row."""
    jobs: list[ScheduledJob] = []
    for account_id, manager in desks.managers():
        jobs.extend(trade_jobs(manager, session_factory, account_id, views, clock.now))

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
