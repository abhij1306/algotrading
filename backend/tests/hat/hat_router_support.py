"""The execution router's test rig (`Rig`: one bot on one broker account, the real Fyers adapter
over `FakeBroker`, the real router) and the crash injection of PR 52: `crash_after` ends the
"process" right after its k-th step (a commit, or a broker call the broker took) the way a power
cut would, and `restart` builds a fresh process's desks on the same database and runs its
recovery. Shared by `properties/test_router.py` and the Postgres check
`db/test_db_crash_safe_routing.py`. Nothing reaches a real broker."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest import mock

from hat.fakes.broker import FakeBroker
from hat_engine_support import (
    AT,
    TRADE_DAY,
    USER,
    World,
    request_for,
    seed_engine_world,
    seed_label,
)
from hat_exec_support import (
    GTT_READY,
    SYMBOLS,
    confirm_gates,
    guard_on,
    live_session,
    seed_live_sessions,
)
from hat_risk_support import CALENDAR
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.daily.accounts import Desks
from app.hat.engine import trades
from app.hat.execution import modes
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.reconcile import ReconciliationService, notifier_sink
from app.hat.execution.router import ExecutionRouter, PlacementCheck
from app.hat.models import (
    HatBot,
    HatBrokerAccount,
    HatInstrument,
    HatMarket,
    HatPosition,
    HatSignal,
)
from app.hat.notify import RecordingNotifier
from app.hat.time import IST

D = Decimal

LIVE_LTP = D(10**6)  # a fresh live price above every stop drawn here (PR 51)
LIVE = PlacementCheck(CALENDAR, feed_stale=False, quality_failed=False, gate_on=True, ltp=LIVE_LTP)
TICKERS = tuple(SYMBOLS)


class Rig:
    """One bot on one broker account, the adapter over `FakeBroker`, and the router."""

    def __init__(
        self,
        session: Session,
        *,
        mode: str,
        gtt: bool = True,
        reconciled: bool = True,
        fake: FakeBroker | None = None,
        live_gtt: bool | None = None,  # the adapter's GTT now; `gtt` (the stored one) when None
        ddpi: bool = True,
        weights: bool = True,
        confidence: str | None = modes.HIGH,  # the label the evidence gives (D132, D149)
    ) -> None:
        self.session = session
        self.world: World = seed_engine_world(session, mode=mode, instruments=2)
        bot = session.get(HatBot, self.world.bot_id)
        assert bot is not None
        self.account_id = bot.broker_account_id
        market_id = session.scalar(select(HatMarket.id))
        session.add(
            HatBrokerAccount(
                id=self.account_id,
                user_id=USER,
                broker="fyers",
                account_ref="X0001",
                capabilities=replace(GTT_READY, gtt_delivery=gtt).to_json(),
                status="active",
            )
        )
        for n, instrument in enumerate(self.world.instruments):
            session.add(
                HatInstrument(
                    id=instrument,
                    market_id=market_id,
                    isin=f"INE{n:09d}",
                    series="EQ",
                    symbol=TICKERS[n],
                    name=TICKERS[n],
                    face_value=D(1),
                    active=True,
                )
            )
        seed_live_sessions(session, self.account_id)
        seed_label(session, self.world, confidence)
        session.flush()
        confirm_gates(session, self.account_id, USER, ddpi=ddpi, weights=weights)
        session.commit()
        self.fake = fake if fake is not None else FakeBroker()
        broker = FyersHatBroker(
            self.fake,  # type: ignore[arg-type]
            instruments=lambda: list(SYMBOLS.items()),
            capabilities=replace(GTT_READY, gtt_delivery=gtt if live_gtt is None else live_gtt),
        )
        live_session(SimpleNamespace(broker=broker))  # type: ignore[arg-type]
        self.broker = broker
        self.guard = guard_on(TRADE_DAY, reconciled=reconciled)
        self.notifier = RecordingNotifier()
        reconciler = ReconciliationService(broker, self.guard, notifier_sink(self.notifier))
        self.router = ExecutionRouter(broker, self.guard, reconciler, self.notifier)

    def route(
        self,
        instrument: int = 0,
        at: datetime = AT,
        check: PlacementCheck = LIVE,
        **ctx: object,
    ) -> trades.EntryOutcome:
        request = request_for(self.world, self.world.instruments[instrument], **ctx)
        return self.router.route(
            self.session,
            trades.EntryRequest(**{**vars(request), "triggered_at": at}),
            now=at,
            check=check,
        )

    def signal(self, signal_id: uuid.UUID) -> HatSignal:
        row = self.session.get(HatSignal, signal_id)
        assert row is not None
        self.session.refresh(row)
        return row

    def places(self) -> list[dict]:
        return self.fake.calls_named("place_order")

    def codes(self) -> list[str]:
        return [code for code, _, _ in self.notifier.alerts]

    def positions(self, signal_id: uuid.UUID) -> list[HatPosition]:
        return list(
            self.session.scalars(select(HatPosition).where(HatPosition.signal_id == signal_id))
        )


class Crash(BaseException):  # noqa: N818 - a power cut, not an error: no handler catches it
    """The process died here (a `BaseException`, so no `except Exception` in HAT handles it)."""


class PlacementError(Exception):
    """A placement that raised in a live process (the process goes on)."""


@contextmanager
def crash_after(
    k: int, fake: FakeBroker, error: type[BaseException] = Crash
) -> Iterator[list[str]]:
    """Raise `error` right after the k-th step from now: a commit (the commit has happened) or a
    `place_order` the broker took (its answer is lost). Yields the steps taken, by name."""
    steps: list[str] = []
    real_commit = Session.commit
    real_place = fake.place_order

    def step(name: str) -> None:
        steps.append(name)
        if len(steps) == k:
            raise error(name)

    def commit(self: Session) -> None:
        real_commit(self)
        step("commit")

    def place(order: dict[str, Any]) -> Any:  # noqa: ANN401 - the fake's reply
        reply = real_place(order)
        step("place_order")
        return reply

    fake.place_order = place  # type: ignore[method-assign]
    try:
        with mock.patch.object(Session, "commit", commit):
            yield steps
    finally:
        fake.place_order = real_place  # type: ignore[method-assign]


def live_check(_s: Session, _bot: uuid.UUID, _iid: uuid.UUID, _at: datetime) -> PlacementCheck:
    """The live conditions the resume reads: all allow an entry."""
    return LIVE


def desks_for(engine: Engine, rig: Rig, day: date = TRADE_DAY) -> Desks:
    """A process's desks over the rig's database and broker (the restart's, or the live one),
    their guards on trade date `day`."""
    return Desks(
        sessionmaker(engine),
        lambda _account: rig.broker,
        RecordingNotifier(),
        issuer=None,
        calendar=lambda: CALENDAR,
        tick_for=lambda _s, _p: None,
        today=lambda: day,
    )


def restart(
    engine: Engine, rig: Rig, now: datetime, desks: Desks | None = None, fail_first: int = 0
) -> Desks:
    """After a crash: a new process (fresh desks, unless the process lived: `desks`) recovers
    and resumes its entries twice (a second pass must place nothing more). With `fail_first`, a
    first resume pass raises `PlacementError` right after its `fail_first`-th step (the resume
    logs it and must try again at the next pass)."""
    if desks is None:
        desks = desks_for(engine, rig, now.astimezone(IST).date())
        desks.recover(now)
    if fail_first:
        with crash_after(fail_first, rig.fake, PlacementError):
            desks.resume_entries(now, live_check, lambda: False)
    for _ in range(2):
        desks.resume_entries(now, live_check, lambda: False)
    return desks


def at_day(day: date, at: datetime = AT) -> datetime:
    """`at`'s time of day on `day`."""
    return datetime.combine(day, at.astimezone(IST).time(), tzinfo=IST)


__all__ = [
    "AT",
    "CALENDAR",
    "LIVE",
    "LIVE_LTP",
    "SYMBOLS",
    "TRADE_DAY",
    "TICKERS",
    "Crash",
    "PlacementError",
    "Rig",
    "at_day",
    "crash_after",
    "desks_for",
    "live_check",
    "restart",
]
