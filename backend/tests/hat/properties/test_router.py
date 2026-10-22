"""PBT exec.router.* (HAT-EXEC-010, -012, -020, -023, -030..032, HAT-BRK-110, HAT-GOV-084): the
execution router of `app.hat.execution.router` on SQLite, with the real risk checks, the real
Fyers adapter and `FakeBroker` beneath it. Nothing reaches a real broker."""

from __future__ import annotations

import threading
import time as time_module
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from hat.fakes.broker import FakeBroker, accepted, rejected
from hat_engine_support import AT, TRADE_DAY, USER, World, request_for, seed_engine_world
from hat_exec_support import SYMBOLS, live_session, seed_live_sessions
from hat_risk_support import CALENDAR
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.daily.accounts import Desks
from app.hat.daily.live import stale_feed
from app.hat.daily.production import one_login_each
from app.hat.data.staleness import StalenessMonitor
from app.hat.engine import exits, lifecycle, trades
from app.hat.engine.portfolio import lifecycle as sleeves
from app.hat.errors import HatConfigError
from app.hat.execution import modes
from app.hat.execution.broker import BrokerUnavailableError, OrderType, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.reconcile import ReconciliationService, notifier_sink
from app.hat.execution.router import (
    ALERT_ENTRY_NOT_PLACED,
    ALERT_ORDER_REJECTED,
    SESSION_NOT_LIVE,
    SIGNAL_EXPIRED,
    ExecutionRouter,
    PlacementCheck,
)
from app.hat.execution.submit import ENTRY, OrderRequest, reserve_order
from app.hat.execution.tags import encode_tag, is_hat_tag
from app.hat.models import (
    HatApproval,
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatEvent,
    HatInstrument,
    HatMarket,
    HatModeChange,
    HatOrder,
    HatPosition,
    HatSignal,
)
from app.hat.notify import RecordingNotifier
from app.hat.risk import controls
from app.hat.risk.modes import ModeChangeError
from app.hat.risk.sessions import SessionState
from app.hat.time import IST

D = Decimal
MODE = st.sampled_from(modes.MODES)
LIVE = PlacementCheck(CALENDAR, feed_stale=False, quality_failed=False, gate_on=True)
_TICKERS = tuple(SYMBOLS)


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
                capabilities={"gtt_delivery": gtt},
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
                    symbol=_TICKERS[n],
                    name=_TICKERS[n],
                    face_value=D(1),
                    active=True,
                )
            )
        seed_live_sessions(session, self.account_id)
        session.commit()
        self.fake = fake if fake is not None else FakeBroker()
        broker = FyersHatBroker(self.fake, instruments=lambda: list(SYMBOLS.items()))  # type: ignore[arg-type]
        live_session(SimpleNamespace(broker=broker))  # type: ignore[arg-type]
        self.broker = broker
        self.guard = ExecutionGuard(reconciled=reconciled)
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


def _rejected_reason(session: Session, signal_id: uuid.UUID) -> str:
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal_id, HatEvent.event_type == "signal.rejected"
        )
    ).all()
    assert len(payloads) == 1
    return payloads[0]["reason"]


def _triggered_pct(session: Session, signal_id: uuid.UUID) -> Decimal:
    payload = session.scalar(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal_id, HatEvent.event_type == "signal.triggered"
        )
    )
    return D(payload["max_chase_pct"])


LIVE_CASE = ("live", LIVE)
CONDITIONS = st.one_of(
    st.just(LIVE_CASE),
    st.sampled_from(
        (
            ("market_closed", replace(LIVE, calendar=None)),
            ("stale_feed", replace(LIVE, feed_stale=True)),
            ("stale_feed", replace(LIVE, feed_stale=None)),
            ("quality_failed", replace(LIVE, quality_failed=True)),
            ("regime_gate", replace(LIVE, gate_on=False)),
        )
    ),
)


@settings(max_examples=30, deadline=None)
@given(MODE, st.booleans(), CONDITIONS)
def test_routes_by_mode(mode: str, gtt: bool, conditions: tuple[str, PlacementCheck]) -> None:
    """HAT-EXEC-010, -012, -020, -022: one accepted trigger, routed by the bot's effective mode
    (confirm and auto fall back to shadow without GTT for delivery). Shadow opens a virtual
    position and calls no broker; confirm writes the approval card expiring 5 minutes later and
    calls no broker; auto sends exactly one LIMIT BUY CNC order, HT-tagged, for the risk-sized
    quantity at the engine's entry price, after the session's reconciliation, when the live
    conditions read at placement allow it, and otherwise ends Rejected with the reason and
    sends nothing. A routed signal without a position holds a slot valued at its planned entry
    price. Harm: a shadow or unapproved signal places a real order, auto buys after the market
    closed or the feed went stale, or the open risk of a pending entry is understated."""
    reason, check = conditions
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=mode, gtt=gtt, reconciled=False)
        # the trigger's level sits below the planned entry price
        outcome = rig.route(check=check, trigger_level=D("99.5"))
        effective = mode if gtt else modes.SHADOW
        signal = rig.signal(outcome.signal_id)
        assert outcome.mode == signal.mode == effective
        assert signal.qty > 0
        assert trades.replay_state(session, signal.id) == signal.state
        if effective == modes.SHADOW:
            assert rig.fake.calls == []
            [position] = rig.positions(signal.id)
            assert position.is_virtual
            assert signal.state == lifecycle.OPEN
        elif effective == modes.CONFIRM:
            assert rig.fake.calls == []
            assert signal.state == lifecycle.AWAITING_APPROVAL
            approval = session.get(HatApproval, signal.id)
            assert approval is not None
            assert approval.decision is None
            assert approval.expires_at.replace(tzinfo=UTC) - approval.sent_at.replace(
                tzinfo=UTC
            ) == timedelta(minutes=5)
            assert rig.positions(signal.id) == []
            [slot] = trades.bot_snapshot(session, rig.world.bot_id, TRADE_DAY)[0]
            assert slot.avg_entry == trades.entry_price_of(session, signal)
            assert slot.avg_entry == D(100)
        elif reason != "live":
            assert rig.places() == []
            assert signal.state == lifecycle.REJECTED
            assert _rejected_reason(session, signal.id) == reason
            assert ALERT_ENTRY_NOT_PLACED in rig.codes()
        else:
            [order] = rig.places()
            first_place = next(
                i for i, (name, _) in enumerate(rig.fake.calls) if name == "place_order"
            )
            assert rig.fake.calls_named("fetch_orderbook"), "reconciled before the first order"
            assert all(name != "place_order" for name, _ in rig.fake.calls[:first_place])
            assert first_place > 0
            assert order["side"] == "BUY"
            assert order["type"] == OrderType.LIMIT.value
            assert order["product"] == "CNC"
            assert order["quantity"] == signal.qty
            assert D(str(order["price"])) == trades.entry_price_of(session, signal)
            assert is_hat_tag(order["order_tag"])
            assert signal.state == lifecycle.ENTRY_PLACED
            assert rig.positions(signal.id) == []


@settings(max_examples=60, deadline=None)
@given(
    st.integers(min_value=-100, max_value=1500),
    st.integers(min_value=0, max_value=420),
    CONDITIONS,
)
@example(chase_bp=1000, after_s=60, conditions=LIVE_CASE)  # 10% past the level, inside the window
@example(chase_bp=0, after_s=299, conditions=LIVE_CASE)  # the last second of the window
def test_take_rechecks_max_chase(
    chase_bp: int, after_s: int, conditions: tuple[str, PlacementCheck]
) -> None:
    """HAT-EXEC-030, -031, -022, HAT-TG-043: a Take before expiry places the real order exactly
    when the live conditions allow an entry (market hours, a fresh feed, a passed quality gate,
    the regime gate on) and its price is within the trigger's max-chase percentage; otherwise it
    records the decision, ends the signal Rejected with the reason and places nothing; a Take at
    or after expiry is refused, the signal expires and continues as its one veto trade. Harm: an
    approval taken after the price ran away, the feed went stale or the card expired buys at a
    price or a moment the plan never allowed."""
    reason, check = conditions
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.CONFIRM)
        outcome = rig.route()
        signal = rig.signal(outcome.signal_id)
        level = D(signal.trigger_price)
        price = level * (10_000 + chase_bp) / 10_000
        pct = _triggered_pct(session, signal.id)
        now = AT + timedelta(seconds=after_s)
        decision = rig.router.take(
            session,
            signal.id,
            price=price,
            locked_upper_circuit=False,
            now=now,
            chat_id=7,
            check=check,
        )
        signal = rig.signal(signal.id)
        approval = session.get(HatApproval, signal.id)
        assert approval is not None
        if after_s >= 300:
            assert decision.outcome == "expired"
            assert not decision.place
            assert rig.places() == []
            assert signal.state == lifecycle.EXPIRED
            [veto] = rig.positions(signal.id)
            assert veto.is_virtual
        elif reason != "live":
            assert decision.outcome == reason
            assert not decision.place
            assert rig.places() == []
            assert signal.state == lifecycle.REJECTED
            assert approval.decision == "take"
        elif price * 100 <= level * (100 + pct):
            assert decision.place
            assert len(rig.places()) == 1
            assert signal.state == lifecycle.ENTRY_PLACED
            assert approval.decision == "take"
            assert D(approval.price_at_decision) == price
        else:
            assert decision.outcome == "max_chase"
            assert not decision.place
            assert rig.places() == []
            assert signal.state == lifecycle.REJECTED
            assert approval.decision == "take"
        assert trades.replay_state(session, signal.id) == signal.state
        again = rig.router.take(
            session,
            signal.id,
            price=price,
            locked_upper_circuit=False,
            now=now,
            chat_id=7,
            check=LIVE,
        )
        assert not again.place  # one decision per card
        assert len(rig.places()) <= 1


ACTIONS = st.lists(
    st.tuples(st.sampled_from(("skip", "sweep", "take")), st.integers(0, 600)),
    min_size=1,
    max_size=6,
)


@settings(max_examples=30, deadline=None)
@given(ACTIONS)
def test_skip_and_expiry_become_one_veto_trade(actions: list[tuple[str, int]]) -> None:
    """HAT-EXEC-032, HAT-TG-045: whatever mix of Skips, expiry sweeps and late Takes arrives, a
    confirm signal that was not taken ends Cancelled (skip) or Expired, with exactly one virtual
    veto trade at the card's quantity and stop, no broker call, and a replay of the signal's and
    the veto trade's events that gives their stored states. Harm: the veto score counts a skip
    twice or not at all, or a skip reaches the broker."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.CONFIRM)
        signal_id = rig.route().signal_id
        taken = False
        for action, after_s in sorted(actions, key=lambda a: a[1]):
            now = AT + timedelta(seconds=after_s)
            if action == "skip":
                rig.router.skip(session, signal_id, now=now, chat_id=7)
            elif action == "sweep":
                rig.router.expire(session, now)
            elif after_s >= 300:
                # a late Take only: an early one is the Take property's case
                taken = (
                    rig.router.take(
                        session,
                        signal_id,
                        price=D(100),
                        locked_upper_circuit=False,
                        now=now,
                        chat_id=7,
                        check=LIVE,
                    ).place
                    or taken
                )
        rig.router.expire(session, AT + timedelta(minutes=10))
        signal = rig.signal(signal_id)
        assert not taken
        assert rig.places() == []
        assert signal.state in (lifecycle.CANCELLED, lifecycle.EXPIRED)
        [veto] = rig.positions(signal_id)
        assert veto.is_virtual
        assert veto.qty_open == signal.qty
        assert D(veto.initial_stop) == D(signal.stop_price)
        assert trades.replay_state(session, signal_id) == signal.state
        assert trades.replay_veto_state(session, signal_id) == lifecycle.OPEN
        # The veto trade is scored only: it holds none of the bot's slots, and its stop-out is
        # not one of the bot's.
        assert trades.bot_snapshot(session, rig.world.bot_id, TRADE_DAY)[0] == []
        view = exits.ExitView(price=D(1), days_held=0, sma={10: D(100)})
        trades.evaluate_position(session, veto.id, view, "close", AT + timedelta(minutes=20))
        session.commit()
        assert trades.replay_veto_state(session, signal_id) == lifecycle.CLOSED
        assert trades.bot_snapshot(session, rig.world.bot_id, TRADE_DAY) == ([], 0, 0)
        assert rig.places() == []


@settings(max_examples=20, deadline=None)
@given(st.lists(st.booleans(), min_size=1, max_size=4))
def test_rejected_entry_is_retried_once(replies: list[bool]) -> None:
    """HAT-EXEC-023: an auto entry the broker rejects is alerted and retried once under the next
    tag; a second rejection ends the signal Rejected with one `entry.not_placed` alert, and no
    third order is sent. Harm: a rejected order is re-sent without limit, or a failure is
    silent."""
    fake = FakeBroker()
    fake.script(
        "place_order",
        *(
            accepted(f"B-{n}") if ok else rejected("Insufficient funds")
            for n, ok in enumerate(replies)
        ),
    )
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, fake=fake)
        signal = rig.signal(rig.route().signal_id)
        sent = rig.places()
        # an unscripted call is accepted
        first_ok = next((n for n, ok in enumerate([*replies, True][:2]) if ok), None)
        assert len(sent) == (first_ok + 1 if first_ok is not None else 2)
        assert len({order["order_tag"] for order in sent}) == len(sent)
        rejections = rig.codes().count(ALERT_ORDER_REJECTED)
        assert rejections == (first_ok if first_ok is not None else 2)
        if first_ok is None:
            assert signal.state == lifecycle.REJECTED
            assert rig.codes().count(ALERT_ENTRY_NOT_PLACED) == 1
        else:
            assert signal.state == lifecycle.ENTRY_PLACED
            assert ALERT_ENTRY_NOT_PLACED not in rig.codes()
        assert trades.replay_state(session, signal.id) == signal.state
        rig.router.place_entry(session, signal.id, now=AT, check=LIVE)  # a repeat sends nothing new
        assert len(rig.places()) == len(sent)


@settings(max_examples=20, deadline=None)
@given(st.booleans(), st.booleans())
def test_reconcile_before_first_order(foreign_ht: bool, unreadable: bool) -> None:
    """HAT-BRK-110: the session's first real order waits for a successful reconciliation; a
    broker that cannot be read, or a book with an HT order HAT does not know, places nothing and
    ends the entry Rejected with an alert. Harm: an order goes out on top of a book HAT has not
    matched, doubling a position after a restart."""
    fake = FakeBroker()
    if foreign_ht:
        fake.orders = [
            {
                "id": "B-X",
                "symbol": "NSE:SBIN-EQ",
                "qty": 1,
                "filledQty": 0,
                "status": 6,
                "side": 1,
                "type": 1,
                "productType": "CNC",
                "orderTag": encode_tag(uuid.uuid4(), ENTRY, 0),
                "limitPrice": 100.0,
            }
        ]
    if unreadable:
        fake.orders = ConnectionError("down")
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, fake=fake, reconciled=False)
        signal = rig.signal(rig.route().signal_id)
        # a book HAT cannot read, or an HT order it does not know, blocks the account
        blocked = unreadable or foreign_ht
        if blocked:
            assert rig.places() == []
            assert signal.state == lifecycle.REJECTED
            assert ALERT_ENTRY_NOT_PLACED in rig.codes()
        else:
            assert len(rig.places()) == 1
            assert rig.guard.reconciled
            assert signal.state == lifecycle.ENTRY_PLACED


class GuardSpy:
    """The adapter, recording what the order gate answers while each book read is in flight
    (another thread's order would read the same gate at that moment)."""

    def __init__(self, broker: FyersHatBroker, guard: ExecutionGuard, bot_id: uuid.UUID) -> None:
        self._broker = broker
        self._guard = guard
        self._bot_id = bot_id
        self.seen: list[str | None] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._broker, name)

    def _read(self, name: str) -> object:
        self.seen.append(self._guard.refusal(self._bot_id, uuid.uuid4(), is_entry=False))
        return getattr(self._broker, name)()

    def orders(self) -> object:
        return self._read("orders")

    def positions(self) -> object:
        return self._read("positions")

    def holdings(self) -> object:
        return self._read("holdings")


@settings(max_examples=20, deadline=None)
@given(st.integers(min_value=1, max_value=3), st.booleans())
def test_routine_reconcile_keeps_gate(runs: int, unreadable: bool) -> None:
    """HAT-BRK-110, `OrderReconciliation` (`reconciled`: a reconciliation succeeded in this
    process lifetime): once the session reconciled, the periodic re-runs, readable or not, never
    close the order gate while they read the book, and a run that cannot read the book leaves it
    open. Harm: an entry or a sale on another thread lands inside the five-minutely read, is
    refused `not_reconciled`, and the entry is lost or the position is left without its stop."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO)
        spy = GuardSpy(rig.broker, rig.guard, rig.world.bot_id)
        reconciler = ReconciliationService(spy, rig.guard)  # type: ignore[arg-type]
        if unreadable:
            rig.fake.orders = ConnectionError("down")
        for _ in range(runs):
            try:
                reconciler.run(session, rig.account_id)
            except BrokerUnavailableError:
                pass
        assert spy.seen
        assert set(spy.seen) == {None}
        assert rig.guard.reconciled


class _NoHealth:
    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None:
        del now, silent_for

    def feed_recovered(self, now: datetime) -> None:
        del now


@settings(max_examples=30, deadline=None)
@given(
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.integers(min_value=0, max_value=29),
)
def test_resumed_entry_waits_for_feed(earlier: bool, late: bool, heard: bool, ago: int) -> None:
    """HAT-OPS-003, HAT-DATA-116 (audit H1, M2): after a restart, an auto entry a crash left in
    Triggered is placed only once the live feed has been heard from (within 30 s) and its account
    has reconciled (at the start or `late`, by a later reconciliation), once; one of an earlier
    session is never placed and ends Rejected `signal_expired`. Harm: a restart during a feed
    outage buys on no market data, or buys today at a price, size and risk decision fixed on an
    earlier day."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        with mock.patch.object(ExecutionRouter, "place_entry", return_value=None):
            signal_id = rig.route().signal_id  # the process dies before the entry is sent
        assert rig.signal(signal_id).state == lifecycle.TRIGGERED
        day = CALENDAR.next_trading_day(TRADE_DAY) if earlier else TRADE_DAY
        assert day is not None
        now = datetime.combine(day, AT.astimezone(IST).time(), tzinfo=IST)
        monitor = StalenessMonitor(_NoHealth(), lambda t: CALENDAR.state(t) is SessionState.OPEN)

        def check(_s: Session, _bot: uuid.UUID, at: datetime) -> PlacementCheck:
            return replace(LIVE, feed_stale=stale_feed(monitor, at))

        books = rig.fake.orders
        if late:
            rig.fake.orders = ConnectionError("down")  # the start's reconciliation fails
        desks = Desks(
            sessionmaker(engine),
            lambda _account: rig.broker,
            RecordingNotifier(),
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
        )
        desks.recover(now)
        desks.resume_entries(now, check, lambda: stale_feed(monitor, now))
        assert rig.places() == []  # no tick heard since the start: the feed reads as stale
        if heard:
            monitor.on_tick(now - timedelta(seconds=ago))
        rig.fake.orders = books
        if late:
            desks.resume_entries(now, check, lambda: stale_feed(monitor, now))
            assert rig.places() == []  # the account has not reconciled yet
            desks.reconcile_all(now)
        for _ in range(2):  # a second pass places nothing more
            desks.resume_entries(now, check, lambda: stale_feed(monitor, now))
        signal = rig.signal(signal_id)
        desks.release()
        if not heard:
            assert rig.places() == []
            assert signal.state == lifecycle.TRIGGERED
        elif earlier:
            assert rig.places() == []
            assert signal.state == lifecycle.REJECTED
            assert _rejected_reason(session, signal_id) == SIGNAL_EXPIRED
        else:
            assert len(rig.places()) == 1
            assert signal.state == lifecycle.ENTRY_PLACED


class OverlapSpy:
    """The adapter, counting the broker calls in flight at once."""

    def __init__(self, broker: FyersHatBroker) -> None:
        self._broker = broker
        self._lock = threading.Lock()
        self._active = 0
        self.most = 0
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._broker, name)
        if not callable(attribute):
            return attribute

        def call(*args: object, **kwargs: object) -> object:
            with self._lock:
                self._active += 1
                self.most = max(self.most, self._active)
                self.calls.append(name)
            try:
                time_module.sleep(0.002)
                return attribute(*args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1

        return call


@settings(max_examples=5, deadline=None)
@given(st.integers(min_value=2, max_value=4))
def test_desk_calls_serialised(rounds: int) -> None:
    """Trading-safety rule 9: the reconciliation, the fill sync, the session watch and the funds
    reads of one account, run from different threads (the periodic loop, the scheduler's jobs,
    the trigger executor), never call its broker at once: every part of the desk goes through
    the account's session owner. Harm: a reconciliation reads the book half way through a send
    on another thread and acts on a book that is neither before nor after it."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        spy = OverlapSpy(rig.broker)
        desks = Desks(
            sessionmaker(engine),
            lambda _account: spy,  # type: ignore[arg-type,return-value]
            RecordingNotifier(),
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
        )
        desk = desks.by_account[rig.account_id]
        work = (
            lambda: desks.reconcile_all(AT),
            lambda: desks.sync_all(AT),
            lambda: desks.watch_sessions(AT),
            lambda: desk.broker.funds(),
        )

        def repeat(step: Callable[[], object]) -> None:
            for _ in range(rounds):
                step()

        threads = [threading.Thread(target=repeat, args=(step,)) for step in work]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        spy.calls.clear()
        desks.watch_sessions(AT)
        desks.release()
    assert spy.most == 1
    assert spy.calls == ["session_status"]  # the row and the outage watch see one read


@settings(max_examples=40, deadline=None)
@given(st.lists(st.sampled_from(("fyers", "other", "unknown")), min_size=1, max_size=5))
def test_one_login_per_broker(brokers: list[str]) -> None:
    """Trading-safety rule 9 (audit L2): the production adapters log in once per broker, so the
    desks get at most one adapter per broker; a second active account of a broker refuses the
    start, and a broker without an adapter gets no desk. Harm: two accounts trade and reconcile
    through one login, and the allocation counts the same funds twice."""
    built: list[str] = []
    broker_for = one_login_each(
        {name: lambda n=name: built.append(n) for name in ("fyers", "other")}
    )
    seen: set[str] = set()
    for name in brokers:
        account = HatBrokerAccount(id=uuid.uuid4(), broker=name)
        if name in seen and name != "unknown":
            with pytest.raises(HatConfigError):
                broker_for(account)
            break
        broker_for(account)
        seen.add(name)
    assert sorted(built) == sorted(seen - {"unknown"})


EVENING = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)  # 19:30 IST, the day before the session
PRE_OPEN = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)  # 08:30 IST, before the session opens


@settings(max_examples=20, deadline=None)
@given(MODE, MODE, st.integers(min_value=0, max_value=300))
def test_mode_change_takes_effect_next_session(start: str, target: str, minutes: int) -> None:
    """HAT-EXEC-011, HAT-GOV-084, HAT-DEC-I9: a mode change queued outside market hours routes
    nothing differently until the next session opens and everything after it; a change asked
    inside market hours is refused and writes nothing; the open applies it to the bot row once.
    Harm: a bot switches to real orders mid-session."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=start)
        bot = session.get(HatBot, rig.world.bot_id)
        assert bot is not None
        during = AT + timedelta(minutes=minutes % 240)
        refused = False
        try:
            modes.request_mode_change(
                session,
                bot_id=bot.id,
                to_mode=target,
                requested_at=during,
                calendar=CALENDAR,
                via="telegram",
                chat_id=7,
            )
        except ModeChangeError:
            refused = True
            session.rollback()
        assert refused
        assert session.scalar(select(func.count()).select_from(HatModeChange)) == 0
        before_open = EVENING + timedelta(minutes=minutes)
        modes.request_mode_change(
            session,
            bot_id=bot.id,
            to_mode=target,
            requested_at=before_open,
            calendar=CALENDAR,
            via="telegram",
            chat_id=7,
        )
        session.commit()
        assert modes.bot_mode(session, bot, before_open, CALENDAR) == start
        assert modes.bot_mode(session, bot, PRE_OPEN, CALENDAR) == start
        assert modes.bot_mode(session, bot, AT, CALENDAR) == target
        opened = CALENDAR.days[AT.date()].open_at
        assert opened is not None
        assert modes.apply_mode_changes(session, opened) == int(start != target)
        assert modes.apply_mode_changes(session, opened) == 0
        assert bot.execution_mode == target
        assert rig.route().mode == target
        if target == modes.SHADOW:
            assert rig.places() == []
        assert session.scalar(
            select(func.count()).select_from(HatOrder).where(HatOrder.is_virtual.is_(False))
        ) == len(rig.places())


PAUSED = "paused"
ACCOUNT_KILL = "drawdown_account"
UNCONFIRMED = "unconfirmed"
KILL_REASON = {
    ACCOUNT_KILL: "kill_account",
    "drawdown_bot": "kill_bot",
    "manual": "kill_bot",
    "data_health": "kill_health",
    PAUSED: "bot_paused",
}
KILLS = st.sampled_from(tuple(KILL_REASON))


@settings(max_examples=30, deadline=None)
@given(KILLS, st.sampled_from(("none", "pending", UNCONFIRMED)))
def test_refused_entry_is_not_sent(kill: str, reserved: str) -> None:
    """HAT-EXEC-020, 06 §6: an auto entry whose bot became manage-only after its risk check (a
    kill switch or a pause, in the gap between the signal's commit and the placement) sends
    nothing. A reserved order that never reached the broker is cancelled and the signal ends
    Rejected with the reason; one that may have reached it is only left to reconciliation: it
    is neither placed again nor rejected. Harm: a bot keeps buying after its kill switch, or a
    refusal re-sends an order the broker may already hold."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO)
        request = request_for(rig.world, rig.world.instruments[0])
        outcome = trades.open_entry(session, request, mode=modes.AUTO)
        session.commit()
        signal = rig.signal(outcome.signal_id)
        tag = None
        if reserved != "none":
            entry = OrderRequest(
                bot_id=signal.bot_id,
                signal_id=signal.id,
                broker_account_id=rig.account_id,
                purpose=ENTRY,
                side=Side.BUY,
                qty=signal.qty,
                order_type=OrderType.LIMIT,
                symbol=_TICKERS[0],
                price=trades.entry_price_of(session, signal),
            )
            tag = reserve_order(session, entry)
            if reserved == UNCONFIRMED:
                row = session.scalar(select(HatOrder).where(HatOrder.tag == tag))
                assert row is not None
                row.status, row.placed_at = UNCONFIRMED, AT
                session.commit()
        if kill == PAUSED:
            assert controls.pause_bot(session, rig.world.bot_id, AT, chat_id=7)
        else:
            scope = "account" if kill == ACCOUNT_KILL else "bot"
            bot_id = None if scope == "account" else rig.world.bot_id
            controls.trip_kill(session, scope=scope, bot_id=bot_id, reason=kill, now=AT)
        session.commit()
        rig.router.place_entry(session, signal.id, now=AT, check=LIVE)
        signal = rig.signal(signal.id)
        assert rig.places() == []
        if reserved == UNCONFIRMED:
            assert signal.state == lifecycle.TRIGGERED
            assert ALERT_ENTRY_NOT_PLACED not in rig.codes()
        else:
            assert signal.state == lifecycle.REJECTED
            assert _rejected_reason(session, signal.id) == KILL_REASON[kill]
            assert rig.codes().count(ALERT_ENTRY_NOT_PLACED) == 1
        if reserved == "pending":
            row = session.scalar(select(HatOrder).where(HatOrder.tag == tag))
            assert row is not None
            assert row.status == "cancelled"


@settings(max_examples=40, deadline=None)
@given(state=st.sampled_from(sleeves.STATES), mode=MODE, gtt=st.booleans())
def test_non_live_sleeve_routes_as_shadow(state: str, mode: str, gtt: bool) -> None:
    """HAT-PORT-063, HAT-EXEC-012: a bot whose sleeve is not Live (research, backtested, shadow,
    probation, retired) runs shadow whatever mode it asked for and whatever its account can do:
    `bot_mode` is shadow and the router places nothing at the broker. A Live sleeve runs the
    mode its account allows. Harm: a sleeve with no evidence, or one in probation, places real
    orders."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=mode, gtt=gtt)
        bot = session.get(HatBot, rig.world.bot_id)
        assert bot is not None
        bot.lifecycle_state = state
        session.commit()
        allowed = modes.effective_mode(
            mode, account_status="active", capabilities={"gtt_delivery": gtt}
        )
        expected = allowed if state == sleeves.LIVE else modes.SHADOW
        assert modes.bot_mode(session, bot, AT, CALENDAR) == expected
        outcome = rig.route()
        assert outcome.mode == expected
        if expected != modes.AUTO:
            assert rig.places() == []
            assert rig.fake.calls == []


@settings(max_examples=30, deadline=None)
@given(st.sampled_from(("missing", "expired", "unknown", "live")), st.integers(-3, 3))
def test_entry_needs_live_session(status: str, offset: int) -> None:
    """HAT-OPS-023: an accepted auto entry is placed only when its broker account has a live
    `hat_broker_session` for the trade date of the placement; otherwise nothing is sent and the
    signal ends Rejected with `broker_session_not_live`. A live session of another day does not
    count. Harm: an entry goes out on an account nobody logged in today, or whose session
    dropped."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO)
        session.execute(delete(HatBrokerSession))
        day = TRADE_DAY + timedelta(days=offset)
        if status != "missing":
            session.add(
                HatBrokerSession(broker_account_id=rig.account_id, trade_date=day, status=status)
            )
        session.commit()
        outcome = trades.open_entry(
            session, request_for(rig.world, rig.world.instruments[0]), mode=modes.AUTO
        )
        session.commit()
        rig.router.place_entry(session, outcome.signal_id, now=AT, check=LIVE)
        signal = rig.signal(outcome.signal_id)
        allowed = status == "live" and day == TRADE_DAY
        assert len(rig.places()) == int(allowed)
        if not allowed:
            assert _rejected_reason(session, signal.id) == SESSION_NOT_LIVE
