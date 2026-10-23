"""Properties of the trade journal (`engine/journal.py`, PR 36; ).

The journal is the one writer of a trade's state: it checks each step against the transition
table before anything is written, refuses an engine decision the table forbids and keeps a broker
fact the table forbids as a non-step. The nightly `journal.check` names every trade whose stored
state is not the fold of its events.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.hat.clock import FixedClock
from app.hat.daily.nightly import JOURNAL_ALERTS, Nightly
from app.hat.engine import journal, lifecycle
from app.hat.events import record as append
from app.hat.models import HatApproval, HatEvent, HatOrder, HatPosition, HatSignal
from app.hat.notify import RecordingNotifier
from app.hat.telegram.render import URGENT_ALERTS

NOW = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
D = Decimal

# (event type, payload, aggregate): every event that drives a step, and order events that never
# do. `approval.sent` stands for the card (no event of its own).
EVENTS: list[tuple[str, dict[str, object], str]] = [
    ("signal.triggered", {}, "signal"),
    ("approval.sent", {}, "signal"),
    ("signal.rejected", {"reason": "x"}, "signal"),
    ("signal.expired", {"reason": "x"}, "signal"),
    ("signal.skipped", {}, "signal"),
    ("kill.tripped", {}, "signal"),
    ("order.placed", {"purpose": "entry"}, "order"),
    ("order.placed", {"purpose": "exit_full"}, "order"),
    ("order.placed", {"purpose": "exit_partial"}, "order"),
    ("order.rejected", {"purpose": "entry", "final": True}, "order"),
    ("order.rejected", {"purpose": "exit_full"}, "order"),
    ("order.cancelled", {"purpose": "entry"}, "order"),
    ("order.filled", {"purpose": "stop_day", "exit_triggered": True}, "order"),
    ("position.opened", {}, "position"),
    ("exit.partial", {}, "position"),
    ("position.closed", {}, "position"),
]
STEPS = st.lists(st.tuples(st.integers(0, len(EVENTS) - 1), st.booleans()), max_size=14)


def _trade(session: Session) -> tuple[HatSignal, HatPosition, HatOrder]:
    signal = HatSignal(
        id=uuid.uuid4(),
        bot_id=uuid.uuid4(),
        strategy_version_id=uuid.uuid4(),
        risk_policy_version_id=uuid.uuid4(),
        playbook="default",
        instrument_id=uuid.uuid4(),
        trade_date=NOW.date(),
        triggered_at=NOW,
        trigger_price=D(100),
        stop_price=D(95),
        qty=10,
        risk_amount=D(50),
        currency="INR",
        mode="confirm",
        state=lifecycle.INITIAL_STATE,
        risk_decision={},
        confidence="low",
        confidence_basis={"reason": "no_run"},
    )
    session.add(signal)
    session.flush()
    position = HatPosition(
        id=uuid.uuid4(),
        signal_id=signal.id,
        bot_id=signal.bot_id,
        instrument_id=signal.instrument_id,
        is_virtual=False,
        qty_open=10,
        avg_entry=D(100),
        initial_stop=D(95),
        current_stop=D(95),
        currency="INR",
        state=lifecycle.OPEN,  # created at its first state; checked once the signal opens
        opened_at=NOW,
        rules_fired=[],
    )
    order = HatOrder(
        id=uuid.uuid4(),
        signal_id=signal.id,
        broker_account_id=uuid.uuid4(),
        purpose="entry",
        side="buy",
        qty=10,
        order_type="LIMIT",
        price=D(100),
        tag="HTE0000000000000000",
        status="placed",
        is_virtual=False,
        currency="INR",
    )
    session.add_all([position, order])
    session.flush()
    return signal, position, order


def _events(session: Session) -> int:
    session.flush()
    return session.scalar(select(func.count()).select_from(HatEvent)) or 0


@settings(max_examples=150, deadline=None)
@given(STEPS)
@example([(0, False), (6, False), (13, False), (7, False), (7, True)])  # a retried sale
@example([(0, False), (13, False)])  # a decision the table forbids
@example([(0, False), (1, False), (6, True), (13, True), (9, True)])  # a card, then a fill
def test_validates_before_writing(steps: list[tuple[int, bool]]) -> None:
    """: for every sequence of lifecycle events, each recorded
    through the journal as an engine decision or as a broker fact, an allowed step moves the
    signal (and, once it holds a position, the position) and writes one event; a decision the
    table forbids raises before anything is written and leaves the state; a broker fact the table
    forbids writes one event marked `no_step` and leaves the state. After every step the stored
    state is the fold of the log. Harm: a refused step leaves an event the state never took, or
    the log and the trade table disagree and the system acts on the wrong one."""
    with Session(memory_engine()) as session:
        signal, position, order = _trade(session)
        for index, broker_fact in steps:
            event_type, payload, aggregate = EVENTS[index]
            before, count = signal.state, _events(session)
            step = lifecycle.step_of(event_type, payload)
            allowed = step is None or (before, step) in lifecycle.TRANSITIONS
            if event_type == "approval.sent":
                if allowed and session.get(HatApproval, signal.id) is None:
                    journal.approval_sent(signal)
                    session.add(
                        HatApproval(
                            signal_id=signal.id, sent_at=NOW, expires_at=NOW, token_hash="h"
                        )
                    )
                    assert signal.state == lifecycle.AWAITING_APPROVAL
                elif not allowed:
                    with pytest.raises(lifecycle.LifecycleStateError):
                        journal.approval_sent(signal)
                    assert signal.state == before
                continue
            aggregate_id = {"signal": signal.id, "order": order.id, "position": position.id}
            call = {
                "aggregate_type": aggregate,
                "aggregate_id": aggregate_id[aggregate],
                "now": NOW,
                "position": position,
                "broker_fact": broker_fact,
            }
            if not allowed and not broker_fact:
                with pytest.raises(lifecycle.LifecycleStateError):
                    journal.record(session, signal, event_type, payload, **call)
                assert signal.state == before
                assert _events(session) == count
                continue
            moved = journal.record(session, signal, event_type, payload, **call)
            assert _events(session) == count + 1
            last = session.scalars(select(HatEvent).order_by(HatEvent.id.desc())).first()
            assert last is not None
            if step is not None and allowed:
                assert moved
                assert signal.state == lifecycle.TRANSITIONS[(before, step)]
            else:
                assert not moved
                assert signal.state == before
                assert last.payload.get(lifecycle.NO_STEP) is (True if step else None)
            if signal.state in lifecycle.POSITION_STATES and signal.state != lifecycle.OPEN:
                assert position.state == signal.state
            assert journal.replay_state(session, signal.id) == signal.state


def _record(
    session: Session, signal: HatSignal, event_type: str, payload: dict[str, object], **call: object
) -> None:
    journal.record(session, signal, event_type, payload, now=NOW, **call)  # type: ignore[arg-type]


def _open(session: Session, signal: HatSignal, position: HatPosition, order: HatOrder) -> None:
    """A real trade the journal took to Open."""
    _record(
        session, signal, "signal.triggered", {}, aggregate_type="signal", aggregate_id=signal.id
    )
    entry = {"purpose": "entry"}
    _record(session, signal, "order.placed", entry, aggregate_type="order", aggregate_id=order.id)
    _record(
        session,
        signal,
        "position.opened",
        {},
        aggregate_type="position",
        aggregate_id=position.id,
        position=position,
    )


def _veto(session: Session, signal: HatSignal, position: HatPosition, order: HatOrder) -> None:
    """A skipped card whose veto trade the journal opened."""
    _record(
        session, signal, "signal.triggered", {}, aggregate_type="signal", aggregate_id=signal.id
    )
    journal.approval_sent(signal)
    session.add(HatApproval(signal_id=signal.id, sent_at=NOW, expires_at=NOW, token_hash="h"))
    _record(session, signal, "signal.skipped", {}, aggregate_type="signal", aggregate_id=signal.id)
    position.is_virtual = True
    entry = {"purpose": "entry"}
    _record(
        session,
        signal,
        "order.placed",
        entry,
        aggregate_type="order",
        aggregate_id=order.id,
        veto=True,
    )
    _record(
        session,
        signal,
        "position.opened",
        {},
        aggregate_type="position",
        aggregate_id=position.id,
        position=position,
        veto=True,
    )


KINDS = st.sampled_from(["signal", "position", "ended", "veto", "unfoldable"])


@settings(max_examples=60, deadline=None)
@given(
    trades=st.lists(
        st.tuples(KINDS, st.booleans(), st.sampled_from(lifecycle.STATES)), min_size=1, max_size=5
    )
)
@example(trades=[("signal", True, lifecycle.TRIGGERED), ("signal", True, lifecycle.CLOSED)])
@example(trades=[("position", True, lifecycle.EXIT_PENDING), ("veto", False, lifecycle.OPEN)])
@example(trades=[("veto", True, lifecycle.EXIT_PENDING), ("position", False, lifecycle.OPEN)])
@example(trades=[("unfoldable", False, lifecycle.OPEN)])
@example(trades=[("ended", True, lifecycle.CLOSED), ("ended", False, lifecycle.OPEN)])
def test_check_names_mismatches(trades: list[tuple[str, bool, str]]) -> None:
    """: the nightly `journal.check` (`journal.mismatches`) names exactly the
    trades not yet ended whose stored state differs from the fold of their events, or whose events
    do not fold (named `unfoldable`, the check going on to the next), each with the trade it is
    (`trade` or `veto`): a signal in an active state, its own position once it holds one, a signal
    stored as ended over its own position not yet Closed, and an open veto trade against its own
    fold. Harm: a trade whose stored state drifted from its log is acted on all night with nobody
    told."""
    with Session(memory_engine()) as session:
        want: set[tuple[uuid.UUID, str]] = set()
        for n, (kind, corrupt, state) in enumerate(trades):
            signal, position, order = _trade(session)
            order.tag = f"HTE{n:017d}"
            if kind == "signal":
                session.delete(position)  # no entry has filled
                _record(
                    session,
                    signal,
                    "signal.triggered",
                    {},
                    aggregate_type="signal",
                    aggregate_id=signal.id,
                )
                if corrupt:  # a write that bypassed the journal
                    session.execute(
                        update(HatSignal).where(HatSignal.id == signal.id).values(state=state)
                    )
                    if state in lifecycle.ACTIVE_STATES and state != lifecycle.TRIGGERED:
                        want.add((signal.id, journal.TRADE))
                continue
            if kind == "unfoldable":  # an event written past the journal that does not fold
                _open(session, signal, position, order)
                append(
                    session,
                    aggregate_type="position",
                    aggregate_id=position.id,
                    event_type="position.closed",
                    actor="engine",
                    payload={},
                    correlation_id=signal.id,
                    bot_id=signal.bot_id,
                    occurred_at=NOW,
                )
                want.add((signal.id, journal.TRADE))
                continue
            if kind == "ended":  # the signal stored as ended over a live position
                _open(session, signal, position, order)
                if corrupt:
                    state = state if state in lifecycle.ENDINGS else lifecycle.CANCELLED
                    session.execute(
                        update(HatSignal).where(HatSignal.id == signal.id).values(state=state)
                    )
                    want.add((signal.id, journal.TRADE))
                continue
            (_open if kind == "position" else _veto)(session, signal, position, order)
            if corrupt:
                state = state if state in lifecycle.POSITION_STATES else lifecycle.EXIT_PENDING
                session.execute(
                    update(HatPosition).where(HatPosition.id == position.id).values(state=state)
                )
                closed_veto = kind == "veto" and state == lifecycle.CLOSED
                if state != lifecycle.OPEN and not closed_veto:
                    want.add((signal.id, journal.VETO if kind == "veto" else journal.TRADE))
        session.flush()
        session.expire_all()
        found = journal.mismatches(session)
        assert {(signal_id, kind) for signal_id, kind, _, _ in found} == want
        unfoldable = {n for n, (kind, _, _) in enumerate(trades) if kind == "unfoldable"}
        assert sum(replayed == journal.UNFOLDABLE for *_, replayed in found) == len(unfoldable)


SAFE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")  # what an alert detail may hold (`render._SAFE`)


@settings(max_examples=25, deadline=None)
@given(drifted=st.integers(0, JOURNAL_ALERTS + 3), sound=st.integers(0, 2))
@example(drifted=JOURNAL_ALERTS + 1, sound=1)
def test_check_alerts(drifted: int, sound: int) -> None:
    """: the nightly `journal.check` job alerts URGENT
    `journal.mismatch` once per drifted trade, at most `JOURNAL_ALERTS` a night, each alert
    naming the signal, its stored and replayed states and the night's count in plain codes, and
    records the count as its rows; sound trades raise nothing and nothing is repaired. Harm: the
    owner never hears that a trade's row and its log disagree, or one bad night floods the chat."""
    factory = sessionmaker(bind=memory_engine())
    want = set()
    with factory() as session:
        for n in range(drifted + sound):
            signal, position, order = _trade(session)
            order.tag = f"HTE{n:017d}"
            _open(session, signal, position, order)
            if n < drifted:
                position.state = lifecycle.EXIT_PENDING  # a write that bypassed the journal
                want.add(str(signal.id))
        session.commit()
    notifier = RecordingNotifier()
    deps = SimpleNamespace(session_factory=factory, notifier=notifier, clock=FixedClock(NOW))
    result = Nightly(deps).journal_check(NOW.date())  # type: ignore[arg-type]
    assert result.rows == drifted
    assert len(notifier.alerts) == min(drifted, JOURNAL_ALERTS)
    assert journal.ALERT_JOURNAL_MISMATCH in URGENT_ALERTS
    for code, day, detail in notifier.alerts:
        assert (code, day) == (journal.ALERT_JOURNAL_MISMATCH, NOW.date())
        assert detail["signal"] in want
        assert detail["count"] == drifted
        assert detail["kind"] == journal.TRADE
        assert (detail["stored"], detail["replayed"]) == (
            f"position:{lifecycle.EXIT_PENDING}",
            lifecycle.OPEN,
        )
        assert all(SAFE.match(str(value)) for value in detail.values())
    with factory() as session:  # nothing repaired
        states = session.scalars(select(HatPosition.state)).all()
        assert states.count(lifecycle.EXIT_PENDING) == drifted
