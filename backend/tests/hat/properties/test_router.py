"""PBT exec.router.* (HAT-EXEC-010, -012, -020, -023, -030..032, HAT-BRK-110, HAT-GOV-084): the
execution router of `app.hat.execution.router` on SQLite, with the real risk checks, the real
Fyers adapter and `FakeBroker` beneath it. Nothing reaches a real broker."""

from __future__ import annotations

import threading
import time as time_module
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

import pytest
from hat.fakes.broker import FakeBroker, accepted, rejected, unconfirmed
from hat_engine_support import (
    AT,
    TRADE_DAY,
    USER,
    add_bot,
    request_for,
)
from hat_exec_support import (
    confirm_gates,
)
from hat_risk_support import CALENDAR
from hat_router_support import (
    LIVE,
    TICKERS,
    Crash,
    PlacementError,
    Rig,
    at_day,
    crash_after,
    desks_for,
    restart,
)
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.hat.daily.accounts import RECONCILE_FAILED, Desks
from app.hat.daily.live import stale_feed
from app.hat.daily.production import one_login_each
from app.hat.data.live import PRICE_OUT_OF_BAND, PRICE_STALE
from app.hat.data.staleness import StalenessMonitor
from app.hat.engine import exits, journal, lifecycle, snapshot, trades
from app.hat.engine.confidence import Confidence
from app.hat.engine.portfolio import lifecycle as sleeves
from app.hat.errors import HatConfigError
from app.hat.execution import mode_changes, modes
from app.hat.execution.broker import BrokerUnavailableError, OrderType, SessionStatus, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_broker import FyersRateLimitedError
from app.hat.execution.guard import ExecutionGuard
from app.hat.execution.protect import STOP_DAY, STOP_GTT, UNSURE, WORKING
from app.hat.execution.reconcile import ReconciliationService
from app.hat.execution.router import (
    ALERT_ENTRY_NOT_PLACED,
    ALERT_ENTRY_UNRESOLVED,
    ALERT_ORDER_REJECTED,
    MAX_ENTRY_REJECTIONS,
    PRICE_AT_OR_BELOW_STOP,
    SESSION_NOT_LIVE,
    SIGNAL_EXPIRED,
    ExecutionRouter,
    PlacementCheck,
)
from app.hat.execution.session import broker_health_failing
from app.hat.execution.submit import ENTRY, OrderRequest, reserve_order
from app.hat.execution.tags import encode_tag, is_hat_tag
from app.hat.models import (
    HatApproval,
    HatBot,
    HatBrokerAccount,
    HatBrokerSession,
    HatEvent,
    HatModeChange,
    HatOrder,
    HatPosition,
    HatSignal,
)
from app.hat.notify import RecordingNotifier
from app.hat.risk import controls, kill
from app.hat.risk.modes import ModeChangeError
from app.hat.risk.sessions import SessionState
from app.hat.time import IST, trade_date_of

D = Decimal
MODE = st.sampled_from(modes.MODES)


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
            # PR 51: the live price read at placement (the rig's stop is 95)
            ("price_unknown", replace(LIVE, ltp=None)),
            (PRICE_STALE, replace(LIVE, ltp=None, ltp_refusal=PRICE_STALE)),
            (PRICE_OUT_OF_BAND, replace(LIVE, ltp=None, ltp_refusal=PRICE_OUT_OF_BAND)),
            (PRICE_AT_OR_BELOW_STOP, replace(LIVE, ltp=D(95))),
            (PRICE_AT_OR_BELOW_STOP, replace(LIVE, ltp=D("61.4"))),
            ("live", replace(LIVE, ltp=D("95.05"))),
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
    price. The outcome `route` returns carries the signal's state after placement and the
    shadow position's id. Harm: a shadow or unapproved signal places a real order, auto buys
    after the market closed or the feed went stale, the open risk of a pending entry is
    understated, or the caller acts on a stale outcome."""
    reason, check = conditions
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=mode, gtt=gtt, reconciled=False)
        # the trigger's level sits below the planned entry price
        outcome = rig.route(check=check, trigger_level=D("99.5"))
        effective = mode if gtt else modes.SHADOW
        signal = rig.signal(outcome.signal_id)
        assert outcome.mode == signal.mode == effective
        assert outcome.state == signal.state  # the state after placement, not the trigger's
        assert signal.qty > 0
        assert journal.replay_state(session, signal.id) == signal.state
        if effective == modes.SHADOW:
            assert rig.fake.calls == []
            [position] = rig.positions(signal.id)
            assert position.is_virtual
            assert outcome.position_id == position.id
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
            [slot] = snapshot.bot_snapshot(session, rig.world.bot_id, TRADE_DAY)[0]
            assert slot.avg_entry == snapshot.entry_price_of(session, signal)
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
            assert D(str(order["price"])) == snapshot.entry_price_of(session, signal)
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
@example(chase_bp=0, after_s=60, conditions=(PRICE_AT_OR_BELOW_STOP, replace(LIVE, ltp=D(95))))
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
        assert journal.replay_state(session, signal.id) == signal.state
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
        assert journal.replay_state(session, signal_id) == signal.state
        assert journal.replay_veto_state(session, signal_id) == lifecycle.OPEN
        # The veto trade is scored only: it holds none of the bot's slots, and its stop-out is
        # not one of the bot's.
        assert snapshot.bot_snapshot(session, rig.world.bot_id, TRADE_DAY)[0] == []
        view = exits.ExitView(price=D(1), days_held=0, sma={10: D(100)})
        trades.evaluate_position(session, veto.id, view, "close", AT + timedelta(minutes=20))
        session.commit()
        assert journal.replay_veto_state(session, signal_id) == lifecycle.CLOSED
        assert snapshot.bot_snapshot(session, rig.world.bot_id, TRADE_DAY) == ([], 0, 0)
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
        assert journal.replay_state(session, signal.id) == signal.state
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
            assert rig.guard.reconciled_today()
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
        self.seen.append(self._guard.refusal(self._bot_id, uuid.uuid4(), is_entry=True))
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
    """HAT-BRK-110, `OrderReconciliation`: once the trade date reconciled, the periodic re-runs,
    readable or not, never close the entry gate while they read the book, and a run that cannot
    read the book leaves it open. Harm: an entry on another thread lands inside the five-minutely
    read, is refused `not_reconciled`, and is lost."""
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
        assert rig.guard.reconciled_today()


class InterleavedBook:
    """The adapter, running a whole second reconciliation (`then`) right after the first run's
    order-book read: two runs on different threads finishing out of order."""

    def __init__(self, broker: FyersHatBroker, then: Callable[[], None]) -> None:
        self._broker = broker
        self._then: Callable[[], None] | None = then

    def __getattr__(self, name: str) -> object:
        return getattr(self._broker, name)

    def orders(self) -> object:
        book = self._broker.orders()
        then, self._then = self._then, None
        if then is not None:
            then()
        return book


def _foreign_ht_row() -> dict[str, object]:
    return {
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


@settings(max_examples=10, deadline=None)
@given(st.booleans(), st.booleans())
def test_older_read_never_overrides_newer(first_unknown: bool, second_unknown: bool) -> None:
    """HAT-BRK-111: when a reconciliation that read the book earlier finishes after one that
    read it later, the later read's result stays in force: an older clean book never clears a
    newer mismatch, and an older mismatch never re-blocks after a newer clean book. Harm: the
    router's run and the periodic run race, and entries open over a book with an unknown HT
    order."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO)
        rig.fake.orders = [_foreign_ht_row()] if first_unknown else []

        def second_run() -> None:
            rig.fake.orders = [_foreign_ht_row()] if second_unknown else []
            with Session(session.get_bind()) as other:
                ReconciliationService(rig.broker, rig.guard).run(other, rig.account_id)

        first = ReconciliationService(InterleavedBook(rig.broker, second_run), rig.guard)  # type: ignore[arg-type]
        first.run(session, rig.account_id)
        refusal = rig.guard.refusal(rig.world.bot_id, rig.account_id, is_entry=True)
        assert (refusal == "reconcile_mismatch") == second_unknown
        assert refusal in (None, "reconcile_mismatch")


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=2), st.booleans()), min_size=1, max_size=6
    )
)
@example([(0, True), (1, False)])
@example([(0, False), (0, True)])
def test_entry_gate_keyed_by_trade_date(runs: list[tuple[int, bool]]) -> None:
    """HAT-BRK-110, -111, D118: an entry is accepted on a trade date only after a successful
    reconciliation of that date (a success on an earlier date opens nothing), and a failed run
    after it never closes the gate; a stop or a sale is never refused by the reconciliation
    state. Each step moves the clock on by 0..2 trade dates and runs a reconciliation that reads
    the book or cannot. Harm: yesterday's success opens today's entries over a book HAT has not
    matched, or an unreadable book keeps a stop or a sale from reaching the broker."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO)
        day = [TRADE_DAY]
        guard = ExecutionGuard(today=lambda: day[0])
        reconciler = ReconciliationService(rig.broker, guard)
        book = rig.fake.orders
        succeeded: set[object] = set()
        for skip, readable in runs:
            day[0] += timedelta(days=skip)
            rig.fake.orders = book if readable else ConnectionError("down")
            try:
                reconciler.run(session, rig.account_id)
                succeeded.add(day[0])
            except BrokerUnavailableError:
                pass
            entry = guard.refusal(rig.world.bot_id, rig.account_id, is_entry=True)
            assert (entry is None) == (day[0] in succeeded)
            assert guard.refusal(rig.world.bot_id, rig.account_id, is_entry=False) is None


@settings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=1), st.booleans()), min_size=1, max_size=6
    )
)
def test_failed_first_reconcile_alerts_once_per_date(runs: list[tuple[int, bool]]) -> None:
    """D118: a reconciliation that fails before the trade date's first success raises one
    `reconcile.failed` health alert for that date, however many runs fail; a failure after the
    date's success raises none. Harm: the owner never learns the account cannot open entries,
    or the chat is flooded every five minutes."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        day = [TRADE_DAY]
        notifier = RecordingNotifier()
        desks = Desks(
            sessionmaker(engine),
            lambda _account: rig.broker,
            notifier,
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
            today=lambda: day[0],
        )
        book = rig.fake.orders
        expected: set[object] = set()
        succeeded: set[object] = set()
        for skip, readable in runs:
            day[0] += timedelta(days=skip)
            rig.fake.orders = book if readable else ConnectionError("down")
            if readable:
                succeeded.add(day[0])
            elif day[0] not in succeeded:
                expected.add(day[0])
            desks.reconcile_all(datetime.combine(day[0], AT.astimezone(IST).time(), tzinfo=IST))
        desks.release()
        failed = [alert for alert in notifier.alerts if alert[0] == RECONCILE_FAILED]
        assert sorted(d for _, d, _ in failed) == sorted(expected)


class _NoHealth:
    def feed_stale(self, now: datetime, silent_for: timedelta | None) -> None:
        del now, silent_for

    def feed_recovered(self, now: datetime) -> None:
        del now


@settings(max_examples=20, deadline=None)
@given(landed=st.booleans(), repeats=st.integers(min_value=1, max_value=3))
@example(landed=True, repeats=1)
def test_unresolved_entry_is_alerted_not_resent(landed: bool, repeats: int) -> None:
    """HAT-BRK-102, -103: an auto entry whose answer was lost, landed behind a lagging order book
    or never landed, is placed once: every later attempt finds nothing by tag, sends nothing and
    raises one `entry.unresolved` alert, and the signal is not rejected while the order may be
    live. Harm: a lagging book doubles the position, or a stuck entry is silent until the
    close."""
    fake = FakeBroker()
    fake.script("place_order", unconfirmed(landed=landed))
    fake.lagging = 1 if landed else 0
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, fake=fake)
        signal = rig.signal(rig.route().signal_id)
        for _ in range(repeats):
            rig.router.place_entry(session, signal.id, now=AT, check=LIVE)
        assert len(rig.places()) == 1
        assert rig.codes().count(ALERT_ENTRY_UNRESOLVED) == repeats
        assert signal.state != lifecycle.REJECTED


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

        def check(_s: Session, _bot: uuid.UUID, _iid: uuid.UUID, at: datetime) -> PlacementCheck:
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
        reads = len(rig.fake.calls)
        desks.recover(now)
        # the restart runs the fill sync (which re-protects any fill) after the reconciliation
        # whether or not it succeeded: two order-book reads, the reconciliation's and the sync's
        assert [name for name, _ in rig.fake.calls[reads:]].count("fetch_orderbook") == 2
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
        if not callable(attribute) or name == "capabilities":  # a local value, not a call
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
            lambda: desks.sync_due(AT),
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


def _fill_entry(fake: FakeBroker, order: dict) -> None:
    """The broker fills the whole entry: its book row and its trade."""
    [row] = [r for r in fake.book if r["orderTag"] == order["order_tag"]]
    row.update(filledQty=row["qty"], status=2)
    fake.tradebook = [
        {
            "tradeNumber": "T-1",
            "orderNumber": row["id"],
            "orderTag": row["orderTag"],
            "symbol": row["symbol"],
            "side": 1,
            "tradedQty": row["qty"],
            "tradePrice": float(order["price"]),
        }
    ]


def _watched(session: Session, now: datetime) -> bool:
    """The reference for `fills.watch_closely`, read row by row: a real entry of a signal of
    this trade date the broker may still fill, or an unsure stop of an open real position."""
    session.expire_all()
    for order in session.scalars(select(HatOrder).where(HatOrder.is_virtual.is_(False))):
        if order.purpose == ENTRY and order.status in WORKING:
            signal = session.get(HatSignal, order.signal_id)
            if signal is not None and signal.trade_date == trade_date_of(now):
                return True
        if order.purpose in (STOP_DAY, STOP_GTT) and order.status in UNSURE:
            position = session.get(HatPosition, order.position_id)
            if position is not None and position.closed_at is None:
                return True
    return False


STOP_REPLIES = {
    "accepted": lambda: accepted("S-1"),
    "unconfirmed": unconfirmed,
    "rejected": lambda: rejected("RMS: price out of band"),
}


@settings(max_examples=30, deadline=None)
@given(
    st.lists(st.integers(min_value=1, max_value=15), min_size=2, max_size=30),
    st.integers(min_value=0, max_value=29),
    st.integers(min_value=0, max_value=29),
    st.booleans(),
    st.sampled_from(sorted(STOP_REPLIES)),
    st.frozensets(st.integers(min_value=0, max_value=29), max_size=6),
)
@example(gaps=[1] * 20, placed=2, filled=10, earlier=False, stop="accepted", down=frozenset())
@example(gaps=[1] * 20, placed=0, filled=29, earlier=True, stop="accepted", down=frozenset())
@example(gaps=[1] * 30, placed=0, filled=2, earlier=False, stop="unconfirmed", down=frozenset())
@example(gaps=[1] * 30, placed=0, filled=2, earlier=False, stop="rejected", down=frozenset())
@example(gaps=[1] * 20, placed=19, filled=0, earlier=False, stop="accepted", down=frozenset({0}))
@example(
    gaps=[1, 12, 1, 1], placed=3, filled=0, earlier=False, stop="accepted", down=frozenset({1})
)
def test_fill_sync_cadence(
    gaps: list[int], placed: int, filled: int, earlier: bool, stop: str, down: frozenset[int]
) -> None:
    """HAT-EXEC-060 (review 2026-10-22 item 1): the periodic passes, `gaps` loop steps of 5 s
    apart, read an account's fills at every pass while a real entry of today's signals may still
    fill (from the pass it is sent until the pass that reads its fill) or a stop's answer is not
    in yet, and otherwise at the first pass and then once a minute: an entry of the previous
    session still shown working and a stop the broker refused do not speed it up. A pass whose
    read failed (`down`: an outage, or the call budget refusing it, PR 50) is tried again at the
    next pass, never waited for. Harm: an entry filled just after a sync
    sits up to a minute at the broker with no stop, or a refused stop is re-sent every 5 s until
    its tag's sequence runs out."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        desks = Desks(
            sessionmaker(engine),
            lambda _account: rig.broker,
            RecordingNotifier(),
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
        )
        now, last, sent, seen = AT, None, False, False
        signal_id = None
        for n, gap in enumerate(gaps):
            now += timedelta(seconds=5 * gap)
            if n == placed:
                signal_id = rig.route(at=now).signal_id
                sent = len(rig.places()) == 1
                if earlier:  # the entry of the previous session, still shown working
                    rig.signal(signal_id).trade_date = TRADE_DAY - timedelta(days=1)
                    session.commit()
            if sent and not earlier and n == placed + filled:
                _fill_entry(rig.fake, rig.places()[0])
                rig.fake.script("place_order", STOP_REPLIES[stop]())
            due = last is None or now - last >= timedelta(minutes=1) or _watched(session, now)
            if due and n in down:
                # an outage, or (odd passes) the call budget refusing the read (PR 50): either
                # way the 5-second cadence yields and the next pass reads again
                failure = (
                    FyersRateLimitedError("Fyers fetch_tradebook request limit reached")
                    if n % 2
                    else BrokerUnavailableError("Fyers tradebook failed")
                )
                rig.fake.script("fetch_tradebook", failure)
            reads = rig.fake.count("fetch_orderbook")
            desks.sync_due(now)
            assert (rig.fake.count("fetch_orderbook") > reads) == due
            if due and n not in down:
                last = now
                seen = seen or bool(signal_id and rig.positions(signal_id))
        desks.release()
    if placed + filled < len(gaps) and not earlier and placed + filled not in down:
        assert seen  # the fill landed


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
            mode_changes.request_mode_change(
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
        mode_changes.request_mode_change(
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
        assert mode_changes.apply_mode_changes(session, opened) == int(start != target)
        assert mode_changes.apply_mode_changes(session, opened) == 0
        assert bot.execution_mode == target
        assert rig.route().mode == target
        if target == modes.SHADOW:
            assert rig.places() == []
        assert session.scalar(
            select(func.count()).select_from(HatOrder).where(HatOrder.is_virtual.is_(False))
        ) == len(rig.places())


@settings(max_examples=20, deadline=None)
@given(stored=st.booleans(), live=st.booleans())
@example(stored=True, live=False)
def test_a_stored_capability_never_enables_a_real_entry(stored: bool, live: bool) -> None:
    """HAT-EXEC-012, HAT-BRK-092: an auto entry reaches the broker only when the account's
    stored capabilities and the adapter's own both report GTT for delivery. A stored `true` the
    adapter does not report routes the bot as auto but places nothing and ends the signal
    Rejected `no_gtt_delivery` before any order is reserved; a stored `false` routes as
    shadow. Harm: a hand-written or stale account row buys real shares that get no overnight
    stop."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, gtt=stored, live_gtt=live)
        outcome = rig.route()
        signal = rig.signal(outcome.signal_id)
        assert len(rig.places()) == (1 if stored and live else 0)
        if stored and not live:  # refused at placement, before any order row is reserved
            assert signal.state == lifecycle.REJECTED
            assert _rejected_reason(session, signal.id) == "no_gtt_delivery"
            assert session.scalar(select(func.count()).select_from(HatOrder)) == 0
        if not stored:
            assert outcome.mode == modes.SHADOW


@settings(max_examples=20, deadline=None)
@given(stored=st.booleans(), live=st.booleans())
def test_desks_start_with_the_adapters_capabilities(stored: bool, live: bool) -> None:
    """HAT-BRK-092, HAT-EXEC-012: building the desks rewrites each account's stored
    capabilities to its adapter's before any routing reads them, so the first signal after a
    start routes by the adapter's GTT value: auto when it reports GTT, shadow when it does not,
    whatever the row said. Harm: a stale row sends confirm cards or auto entries the placement
    then refuses, or keeps a revoked account out of shadow until its first session check; a
    placement's capability read waits behind a slow broker call."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO, gtt=stored, live_gtt=live)
        desks = Desks(
            sessionmaker(engine),
            lambda _account: rig.broker,
            RecordingNotifier(),
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
        )
        desk = desks.by_account[rig.account_id]
        read: list[object] = []

        def in_flight() -> None:  # a broker call holds the owner; the capability read is local
            reader = threading.Thread(target=lambda: read.append(desk.broker.capabilities()))
            reader.start()
            reader.join(timeout=2)
            assert read, "the capability read waited for a broker call in flight"

        desk.owner.call(in_flight)
        desks.release()
        account = session.get(HatBrokerAccount, rig.account_id)
        assert account is not None
        session.refresh(account)
        assert account.capabilities["gtt_delivery"] is live
        outcome = rig.route()
        assert outcome.mode == (modes.AUTO if live else modes.SHADOW)
        assert len(rig.places()) == (1 if live else 0)


class ScriptedSession:
    """The adapter with a scripted session status: live, expired, or a read that fails."""

    def __init__(self, broker: FyersHatBroker) -> None:
        self._broker = broker
        self.answer = "live"

    def session_status(self) -> SessionStatus:
        if self.answer == "fails":
            raise BrokerUnavailableError("broker_unavailable")
        return SessionStatus.LIVE if self.answer == "live" else SessionStatus.EXPIRED

    def __getattr__(self, name: str) -> object:
        return getattr(self._broker, name)


SESSION_CHECKS = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=24 * 60 - 1),
        st.sampled_from(("live", "expired", "fails")),
    ),
    min_size=1,
    max_size=8,
    # one check per minute at most, as the periodic step runs them: a trip and a clear at one
    # instant resolve to the trip by design (`kill._latest`, fail safe)
    unique_by=lambda check: check[0],
)


@settings(max_examples=25, deadline=None)
@given(SESSION_CHECKS, st.booleans())
def test_broker_health_follows_the_session(checks: list[tuple[int, str]], feed_down: bool) -> None:
    """HAT-RISK-063 (design review item 5): after every session check of a trading day, the
    account's bots have `broker_health` tripped iff the session is not live (a failed read counts
    as not live) between 09:05 and the 15:30 close IST, and cleared otherwise; the check never
    touches another reason (a tripped `feed_health` stays), the account switch or a bot of
    another account. Harm: a missed login leaves the bots looking healthy while their exits are
    off, a check clears the feed's trip and entries run on a stale feed, or one account's
    outage halts another's bots."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        scripted = ScriptedSession(rig.broker)
        desks = Desks(
            sessionmaker(engine),
            lambda _account: scripted,  # type: ignore[arg-type,return-value]
            RecordingNotifier(),
            issuer=None,
            calendar=lambda: CALENDAR,
            tick_for=lambda _s, _p: None,
        )
        bot = rig.world.bot_id
        sibling = add_bot(session, rig.world).bot_id  # a second bot on the account
        stranger = add_bot(session, rig.world).bot_id  # a bot on another account
        sibling_row = session.get(HatBot, sibling)
        assert sibling_row is not None
        sibling_row.broker_account_id = rig.account_id
        session.commit()
        if feed_down:
            controls.sync_health(
                session, scope="bot", bot_id=bot, failing=True, reason="feed_health", now=AT
            )
            session.commit()
        midnight = datetime.combine(TRADE_DAY, time(0), tzinfo=IST)
        for minute, answer in sorted(checks):
            now = midnight + timedelta(minutes=minute)
            scripted.answer = answer
            desks.watch_sessions(now)
            session.expire_all()
            rows = controls.kill_rows(session)
            down = broker_health_failing(now, answer == "live")
            for mine in (bot, sibling):
                state = kill.current_state(rows, "bot", mine, stream="broker_health")
                assert state == (kill.TRIPPED if down else kill.ARMED)
            assert kill.current_state(rows, "bot", stranger) == kill.ARMED
            feed = kill.current_state(rows, "bot", bot, stream="feed_health")
            assert feed == (kill.TRIPPED if feed_down else kill.ARMED)
            assert kill.current_state(rows, "account", None) == kill.ARMED
        desks.release()


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
    """HAT-EXEC-020,: an auto entry whose bot became manage-only after its risk check (a
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
                symbol=TICKERS[0],
                price=snapshot.entry_price_of(session, signal),
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
            mode,
            modes.Gates(
                "active", {"gtt_delivery": gtt}, ddpi_confirmed=True, weights_confirmed=True
            ),
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


# --- HAT-GOV-085, HAT-EXEC-014: the owner's gates and auto by confidence -----------------------

OPENED = CALENDAR.days[AT.date()].open_at
assert OPENED is not None
assert OPENED < AT


@settings(max_examples=40, deadline=None)
@given(
    mode=MODE,
    ddpi=st.sampled_from(("none", "before_open", "after_open", "revoked")),
    weights=st.booleans(),
)
@example(mode=modes.AUTO, ddpi="after_open", weights=True)
@example(mode=modes.CONFIRM, ddpi="before_open", weights=False)
def test_mode_gates_hold_bots_in_shadow(mode: str, ddpi: str, weights: bool) -> None:
    """HAT-GOV-085: a Live bot on an account with GTT for delivery runs the mode it asked for
    only while the owner had confirmed DDPI on its account by the session's open (one set
    during the session waits for the next open; one revoked holds it back at once) and the
    policy in force at the open confirms the base weights; otherwise it runs shadow and calls
    no broker. Harm: real orders before DDPI is registered (sales stall on a TPIN prompt) or
    before the owner set the allocation numbers."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=mode, weights=weights)
        account = session.get(HatBrokerAccount, rig.account_id)
        assert account is not None
        if ddpi == "none":
            account.ddpi_confirmed, account.ddpi_confirmed_at = False, None
        elif ddpi == "after_open":
            account.ddpi_confirmed_at = OPENED + timedelta(minutes=1)
        elif ddpi == "revoked":  # confirmed long ago, then cleared
            account.ddpi_confirmed = False
        session.commit()
        bot = session.get(HatBot, rig.world.bot_id)
        assert bot is not None
        gated = ddpi == "before_open" and weights
        expected = mode if gated else modes.SHADOW
        assert modes.bot_mode(session, bot, AT, CALENDAR) == expected
        outcome = rig.route()
        assert outcome.mode == expected
        if expected == modes.SHADOW:
            assert rig.fake.calls == []
            view = modes.mode_view(session, bot, AT, CALENDAR)
            reason = "ddpi_not_confirmed" if ddpi != "before_open" else "weights_not_confirmed"
            assert view.why == (None if mode == modes.SHADOW else reason)
        if expected == modes.AUTO:
            assert len(rig.places()) == 1


@settings(max_examples=30, deadline=None)
@given(st.sampled_from((modes.HIGH, "medium", "low", None, "HIGH", "")))
@example(None)
def test_auto_places_only_high_confidence(confidence: str | None) -> None:
    """HAT-EXEC-014, D149: an accepted auto signal labelled high sends exactly one entry order;
    any other label, or none, is routed as confirm: an approval card, the signal in
    AwaitingApproval and no broker call. Harm: a medium- or low-confidence trade is bought
    without the owner's approval."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, confidence=confidence)
        outcome = rig.route()
        signal = rig.signal(outcome.signal_id)
        if confidence == modes.HIGH:
            assert outcome.mode == signal.mode == modes.AUTO
            assert len(rig.places()) == 1
            assert session.get(HatApproval, signal.id) is None
        else:
            assert outcome.mode == signal.mode == modes.CONFIRM
            assert signal.state == lifecycle.AWAITING_APPROVAL
            assert session.get(HatApproval, signal.id) is not None
            assert rig.fake.calls == []


@settings(max_examples=20, deadline=None)
@given(st.sampled_from((modes.HIGH, "medium", "low")))
@example("low")
def test_placement_rechecks_confidence(label: str) -> None:
    """HAT-EXEC-014, D149: placement reads the stored label again, so an auto signal stored
    without a high one (written by another path) is never sent: it ends Rejected with
    `not_high_confidence` and no broker call. Harm: the auto rule rests on one write, and a
    medium- or low-confidence trade is bought without the owner's approval."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=modes.AUTO, confidence=None)
        request = request_for(rig.world, rig.world.instruments[0])
        outcome = trades.open_entry(
            session,
            replace(request, triggered_at=AT),
            mode=modes.AUTO,
            confidence=Confidence(modes.HIGH, {"reason": "test"}),
        )
        signal = rig.signal(outcome.signal_id)
        assert signal.mode == modes.AUTO
        signal.confidence = label  # SQLite has no trigger: the row another path could write
        session.commit()
        rig.router.place_entry(session, signal.id, now=AT, check=LIVE)
        signal = rig.signal(outcome.signal_id)
        if label == modes.HIGH:
            assert len(rig.places()) == 1
        else:
            assert rig.fake.calls == []
            assert signal.state == lifecycle.REJECTED
            assert _rejected_reason(session, signal.id) == modes.NOT_HIGH_CONFIDENCE


@settings(max_examples=40, deadline=None)
@given(
    start=MODE,
    target=MODE,
    ddpi_at_open=st.booleans(),
    weights_at_open=st.booleans(),
)
@example(start=modes.SHADOW, target=modes.AUTO, ddpi_at_open=False, weights_at_open=True)
def test_open_settles_mode_changes(
    start: str, target: str, ddpi_at_open: bool, weights_at_open: bool
) -> None:
    """HAT-GOV-085, HAT-GOV-084: a change queued in the evening, while every gate passed, is
    settled at the next open by the gates as they stand then: applied (the bot row takes the
    mode, one `mode.applied`) or refused with the gate's code (the bot keeps its mode, one
    `mode.apply_refused`, and the change never routes an entry). A second run at the same open
    changes nothing. Harm: a change silently fails, or a refused change takes effect later
    without a request."""
    with Session(memory_engine()) as session:
        rig = Rig(session, mode=start)
        bot = session.get(HatBot, rig.world.bot_id)
        assert bot is not None
        mode_changes.request_mode_change(
            session,
            bot_id=bot.id,
            to_mode=target,
            requested_at=EVENING,
            calendar=CALENDAR,
            via="telegram",
            chat_id=7,
        )
        session.commit()
        account = session.get(HatBrokerAccount, rig.account_id)
        assert account is not None
        account.ddpi_confirmed = ddpi_at_open  # the owner revokes it overnight, or not
        confirm_gates(session, rig.account_id, USER, ddpi=ddpi_at_open, weights=weights_at_open)
        session.commit()
        refused = target != modes.SHADOW and not (ddpi_at_open and weights_at_open)
        after = start if refused else target
        changed = mode_changes.apply_mode_changes(session, OPENED)
        assert changed == int(after != start)
        assert mode_changes.apply_mode_changes(session, OPENED) == 0
        assert bot.execution_mode == after
        [row] = session.scalars(select(HatModeChange)).all()
        kinds = session.scalars(
            select(HatEvent.event_type).where(
                HatEvent.event_type.in_(("mode.applied", "mode.apply_refused"))
            )
        ).all()
        if refused:
            assert row.refused_reason == (
                "ddpi_not_confirmed" if not ddpi_at_open else "weights_not_confirmed"
            )
            assert row.applied_at is None
            assert kinds == ["mode.apply_refused"]
        else:
            assert row.refused_reason is None
            assert row.applied_at is not None
            assert kinds == ["mode.applied"]
        assert modes.requested_mode(session, bot, AT) == after


# --- crash safety (PR 52, audit T-F4, T-F7) ---------------------------------------------------


def _left_stranded(session: Session, signal_id: uuid.UUID) -> bool:
    """The signal still waits to be placed with no entry order that may be working (a row
    reserved with no attempt stamped provably never reached the broker, so it does not count)."""
    signal = session.get(HatSignal, signal_id)
    assert signal is not None
    session.refresh(signal)
    if signal.state not in (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL):
        return False
    if signal.state == lifecycle.AWAITING_APPROVAL:
        approval = session.get(HatApproval, signal_id)
        if approval is None or approval.decision != "take":
            return False  # an undecided card: the sweep's
    working = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.status.in_(("pending", "unconfirmed", "placed", "partially_filled")),
            (HatOrder.status != "pending") | HatOrder.placed_at.is_not(None),
        )
    )
    return not working


@settings(max_examples=60, deadline=None)
@example(
    path="auto", replies=[False, False], k=9, error="crash", again=0
)  # after the 2nd rejection
@example(
    path="take", replies=[True, True], k=1, error="crash", again=0
)  # after the Take's decision
@example(
    path="auto", replies=[True, True], k=1, error="crash", again=0
)  # after the trigger's commit
@example(path="take", replies=[False, True], k=1, error="raise", again=0)
@example(path="take", replies=[True, True], k=1, error="crash", again=1)  # the resume fails once
@given(
    path=st.sampled_from(("auto", "take")),
    replies=st.lists(st.booleans(), min_size=2, max_size=2),
    k=st.integers(min_value=1, max_value=12),
    error=st.sampled_from(("crash", "raise")),
    again=st.integers(min_value=0, max_value=3),
)
def test_crash_resumes_entry_once(
    path: str, replies: list[bool], k: int, error: str, again: int
) -> None:
    """HAT-OPS-003, HAT-EXEC-023 (PR 52, audit T-F4, T-F7): an auto entry, or a Take, whose
    process dies (or whose placement raises) right after any commit or broker call is, after the
    restart's recovery and resume (run twice), placed or ended exactly once: at most two entry
    orders under distinct tags (a crash after the second rejection never sends a third), at most
    one the broker took, and the signal never left waiting with no order that may be working;
    when the first resume pass itself raises after its `again`-th step, a later pass places it.
    Harm: a restart sends a third entry, buys twice, or leaves a taken signal holding its slot and
    open risk for good."""
    fake = FakeBroker()
    fake.script(
        "place_order",
        *(
            accepted(f"B-{n}") if ok else rejected("Insufficient funds")
            for n, ok in enumerate(replies)
        ),
    )
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO if path == "auto" else modes.CONFIRM, fake=fake)
        live = desks_for(engine, rig)
        live.recover(AT)  # the live process started (and reconciled) before the trigger
        rig.router = live.by_account[rig.account_id].router
        rig.guard = live.by_account[rig.account_id].guard
        signal_id = None if path == "auto" else rig.route().signal_id
        failure = Crash if error == "crash" else PlacementError
        try:
            with crash_after(k, fake, failure):
                if path == "auto":
                    rig.route()
                else:
                    assert signal_id is not None
                    signal = rig.signal(signal_id)
                    rig.router.take(
                        session,
                        signal_id,
                        price=D(signal.trigger_price),
                        locked_upper_circuit=False,
                        now=AT,
                        chat_id=7,
                        check=LIVE,
                    )
        except (Crash, PlacementError):
            session.rollback()
        if signal_id is None:
            signal_id = session.scalar(select(HatSignal.id))
        assert signal_id is not None
        session.close()
        desks = restart(engine, rig, AT, None if error == "crash" else live, again)
        sent = rig.places()
        assert len(sent) <= MAX_ENTRY_REJECTIONS
        assert len({order["order_tag"] for order in sent}) == len(sent)
        took = [n for n in range(len(sent)) if n >= len(replies) or replies[n]]
        assert len(took) <= 1
        with Session(engine) as fresh:
            assert not _left_stranded(fresh, signal_id)
            state = fresh.get(HatSignal, signal_id).state  # type: ignore[union-attr]
            assert journal.replay_state(fresh, signal_id) == state
        desks.release()
        live.release()


def _snapshot(session: Session) -> tuple[object, ...]:
    """Everything a sweep can change: signals, cards, positions, orders and the event log."""
    session.expire_all()
    return (
        sorted(session.execute(select(HatSignal.id, HatSignal.state)).all()),
        sorted(session.execute(select(HatApproval.signal_id, HatApproval.decision)).all()),
        sorted(
            session.execute(
                select(HatPosition.signal_id, HatPosition.is_virtual, HatPosition.qty_open)
            ).all()
        ),
        sorted(session.execute(select(HatOrder.tag, HatOrder.status)).all()),
        session.scalar(select(func.count()).select_from(HatEvent)),
    )


SWEEP_ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(("sweep", "take", "skip", "take_crashed", "place")),
        st.integers(0, 1),
        st.integers(0, 600),
    ),
    min_size=1,
    max_size=8,
)


@settings(max_examples=40, deadline=None)
@example(actions=[("take_crashed", 0, 10), ("sweep", 0, 400)], sweeps=2)
@example(actions=[("sweep", 0, 300), ("take", 1, 310)], sweeps=3)
@example(actions=[("place", 0, 10), ("take_crashed", 1, 20), ("sweep", 0, 400)], sweeps=2)
@given(actions=SWEEP_ACTIONS, sweeps=st.integers(min_value=2, max_value=4))
def test_sweep_idempotent(actions: list[tuple[str, int, int]], sweeps: int) -> None:
    """HAT-EXEC-030, HAT-EXEC-032 (PR 52, audit T-F15, T-F7): for every interleaving of Takes,
    Skips, Takes whose placement crashed, re-driven placements and sweeps over two cards, running
    the approval sweep any number of times at one moment gives the state one sweep gives; no card
    yields more than one entry order and a re-driven placement never buys a card not taken; once
    the 5 minutes are up every card is decided; and the next trade date's sweep ends a Take left
    unplaced Rejected (`signal_expired`) with no order, so no signal holds a slot past its day.
    Harm: a repeated or overlapping sweep opens a second veto trade or a second order, or a
    stranded Take holds a slot and open risk for good."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.CONFIRM)
        ids = [rig.route(instrument=n).signal_id for n in range(2)]
        for action, n, after_s in sorted(actions, key=lambda a: a[2]):
            now = AT + timedelta(seconds=after_s)
            signal = rig.signal(ids[n])
            if action == "skip":
                rig.router.skip(session, ids[n], now=now, chat_id=7)
            elif action == "place":  # a resume's re-drive: places only a passed Take
                approval = session.get(HatApproval, ids[n])
                undecided = approval is not None and approval.decision is None
                before = len(rig.places())
                rig.router.place_entry(session, ids[n], now=now, check=LIVE)
                if undecided:
                    assert len(rig.places()) == before
            elif action == "sweep":
                rig.router.expire(session, now)
                once = _snapshot(session)
                for _ in range(sweeps - 1):
                    rig.router.expire(session, now)
                assert _snapshot(session) == once
            else:
                try:
                    with crash_after(1 if action == "take_crashed" else 0, rig.fake):
                        rig.router.take(
                            session,
                            ids[n],
                            price=D(signal.trigger_price),
                            locked_upper_circuit=False,
                            now=now,
                            chat_id=7,
                            check=LIVE,
                        )
                except Crash:
                    session.rollback()
        for signal_id in ids:
            entries = session.scalar(
                select(func.count())
                .select_from(HatOrder)
                .where(HatOrder.signal_id == signal_id, HatOrder.purpose == ENTRY)
            )
            assert entries <= 1
        assert len(rig.places()) <= 2
        late = AT + timedelta(minutes=10)
        rig.router.expire(session, late)
        for signal_id in ids:
            state = rig.signal(signal_id).state
            approval = session.get(HatApproval, signal_id)
            assert approval is not None
            assert approval.decision is not None  # every card decided after its 5 minutes
            if approval.decision == "take":  # placed, or still to be re-driven today
                assert state in (lifecycle.ENTRY_PLACED, lifecycle.AWAITING_APPROVAL)
            else:
                assert state != lifecycle.AWAITING_APPROVAL
        tomorrow = CALENDAR.next_trading_day(TRADE_DAY)
        assert tomorrow is not None
        nxt = at_day(tomorrow)
        rig.router.expire(session, nxt)
        settled = _snapshot(session)
        for _ in range(sweeps - 1):
            rig.router.expire(session, nxt)
        assert _snapshot(session) == settled
        for signal_id in ids:
            # nothing waits past its day: placed, or ended (no slot held)
            assert rig.signal(signal_id).state not in (
                lifecycle.TRIGGERED,
                lifecycle.AWAITING_APPROVAL,
            )


@settings(max_examples=30, deadline=None)
@example(k=1, order=("sweep",), path="auto")  # Triggered, no order
@example(k=2, order=("sweep", "restart"), path="auto")  # an entry reserved, never sent
@example(k=2, order=("sweep",), path="auto")  # the sweep alone cancels the unsent row
@example(k=1, order=("restart", "sweep"), path="take")
@given(
    k=st.integers(min_value=1, max_value=6),
    order=st.lists(st.sampled_from(("sweep", "restart")), min_size=1, max_size=3),
    path=st.sampled_from(("auto", "take")),
)
def test_stranded_entry_ends_next_day(k: int, order: list[str], path: str) -> None:
    """HAT-OPS-003, HAT-EXEC-030 (PR 52, audit T-F7): an auto entry or a Take whose process died
    after its k-th step and never came back that day is, on a later trade date, never placed:
    the sweep or the restart's resume (in any order, any number of times) ends it Rejected
    `signal_expired`, cancelling an entry order reserved and never sent, while one that may have
    reached the broker is left to reconciliation; nothing more reaches the broker and no signal
    waits with no order. Harm: yesterday's price, size and risk decision are bought today, or a
    stranded signal holds a slot and open risk for good."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO if path == "auto" else modes.CONFIRM)
        signal_id = None if path == "auto" else rig.route().signal_id
        try:
            with crash_after(k, rig.fake):
                if path == "auto":
                    rig.route()
                else:
                    assert signal_id is not None
                    rig.router.take(
                        session,
                        signal_id,
                        price=D(rig.signal(signal_id).trigger_price),
                        locked_upper_circuit=False,
                        now=AT,
                        chat_id=7,
                        check=LIVE,
                    )
        except Crash:
            session.rollback()
        signal_id = signal_id or session.scalar(select(HatSignal.id))
        assert signal_id is not None
        sent = len(rig.places())
        tomorrow = CALENDAR.next_trading_day(TRADE_DAY)
        assert tomorrow is not None
        nxt = at_day(tomorrow)
        for step in order:
            if step == "sweep":
                rig.router.expire(session, nxt)
            else:
                restart(engine, rig, nxt).release()
        assert len(rig.places()) == sent  # nothing more reaches the broker
        assert not _left_stranded(session, signal_id)
        state = rig.signal(signal_id).state
        if state == lifecycle.REJECTED:
            assert _rejected_reason(session, signal_id) == SIGNAL_EXPIRED
        settled = _snapshot(session)
        rig.router.expire(session, nxt)
        assert _snapshot(session) == settled


@settings(max_examples=10, deadline=None)
@example(bad=0)
@given(bad=st.integers(min_value=0, max_value=1))
def test_sweep_survives_a_bad_signal(bad: int) -> None:
    """HAT-EXEC-030 (PR 52): when ending one stranded entry raises (a database error on that
    signal), the sweep goes on: every other earlier-day stranded entry ends Rejected
    `signal_expired`, the failing one is left as it was (no order), and the next sweep, once it
    no longer fails, ends it. Harm: one bad row stops the sweep every minute and every stranded
    signal after it holds its slot for good."""
    engine = memory_engine()
    with Session(engine) as session:
        rig = Rig(session, mode=modes.AUTO)
        for n in range(2):  # two auto entries, each left in Triggered by a crash at its commit
            try:
                with crash_after(1, rig.fake):
                    rig.route(instrument=n)
            except Crash:
                session.rollback()
        ids = sorted(session.scalars(select(HatSignal.id)).all())
        assert len(ids) == 2
        failing = ids[bad]
        real_reject = trades.reject_signal

        def reject(s: Session, signal_id: uuid.UUID, *args: object, **kwargs: object) -> bool:
            if signal_id == failing:
                raise RuntimeError("database error")
            return real_reject(s, signal_id, *args, **kwargs)  # type: ignore[arg-type]

        tomorrow = CALENDAR.next_trading_day(TRADE_DAY)
        assert tomorrow is not None
        with mock.patch.object(trades, "reject_signal", reject):
            rig.router.expire(session, at_day(tomorrow))
        assert rig.places() == []
        for signal_id in ids:
            state = rig.signal(signal_id).state
            if signal_id == failing:
                assert state == lifecycle.TRIGGERED
            else:
                assert state == lifecycle.REJECTED
                assert _rejected_reason(session, signal_id) == SIGNAL_EXPIRED
        rig.router.expire(session, at_day(tomorrow))
        assert rig.signal(failing).state == lifecycle.REJECTED
        assert not any(_left_stranded(session, signal_id) for signal_id in ids)
