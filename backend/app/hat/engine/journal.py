"""The trade journal: the one writer of a trade's lifecycle state (HAT-EXEC-042, HAT-SCH-062;
PR 36).

`hat_signal.state` and `hat_position.state` are always the fold of the trade's events over the
transition table (`lifecycle.TRANSITIONS`). Every event that may drive a step goes through
`record` as an `Event` (`on_signal`, `on_position` and `on_order` build the common ones), which
works out the step, checks it against the table *before* anything is written, then moves the
signal (and its position) and appends the event in the caller's transaction, so both commit or
roll back together. One rule decides a step the table has no row for:

- **An engine decision is refused.** `LifecycleStateError` is raised and nothing is written; the
  caller's transaction rolls back. A decision that does not fit the trade's state is a bug.
- **A broker fact is kept, as a non-step.** An order event recorded after the broker answered
  (`broker_fact=True`: the order path in `execution/submit.py`) is never refused, since the
  order's bookkeeping must not fail after a broker call. It is written with `no_step: true`, so
  it drives nothing on replay, and the state stays. Example: a sale the exchange rejected and
  HAT sent again records a second exit `order.placed` while the trade is already ExitPending.

The approval card has no event of its own (`approval_sent`): the `hat_approval` row written in
the same transaction stands for the step on replay. Nothing outside this module assigns `.state`
on a signal or a position (CONF `journal_only_writers`); a row is created at its first state.

`replay_state` and `replay_veto_state` fold the stored events; `mismatches` is the nightly check
(`journal.check`) that folds every trade not yet ended and names those whose stored state
differs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypedDict, Unpack

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Exists

from ..events import Actor
from ..events import record as append
from ..governance import Mode
from ..models import HatApproval, HatEvent, HatOrder, HatPosition, HatSignal
from . import lifecycle

__all__ = [
    "ALERT_JOURNAL_MISMATCH",
    "VETO",
    "TRADE",
    "UNFOLDABLE",
    "Event",
    "Mismatch",
    "approval_sent",
    "mismatches",
    "on_order",
    "on_position",
    "on_signal",
    "record",
    "replay_state",
    "replay_veto_state",
]

logger = logging.getLogger(__name__)

ALERT_JOURNAL_MISMATCH = "journal.mismatch"  # a trade's stored state is not its log's fold
VETO = "veto"  # the payload flag of a veto trade's events (HAT-EXEC-032)
SIGNAL_TRIGGERED = "signal.triggered"


@dataclass(frozen=True)
class Event:
    """One trade event: its type, the row it is about (`aggregate_type`, `aggregate_id`), its
    payload, and how `record` writes it. `position` is the row a step may move; `veto` flags a
    veto trade's event; `broker_fact` keeps an event the table has no row for as a non-step;
    `bot_id` overrides the signal's bot."""

    type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    payload: Mapping[str, Any] = field(default_factory=dict)
    now: datetime | None = None
    actor: Actor = "engine"
    position: HatPosition | None = None
    veto: bool = False
    broker_fact: bool = False
    bot_id: uuid.UUID | None = None


class Options(TypedDict, total=False):
    """`Event`'s fields after the payload, for `on_signal`, `on_position` and `on_order`."""

    now: datetime | None
    actor: Actor
    position: HatPosition | None
    veto: bool
    broker_fact: bool
    bot_id: uuid.UUID | None


def on_signal(
    signal: HatSignal, event_type: str, payload: Mapping[str, Any], **kw: Unpack[Options]
) -> Event:
    """An event about the signal itself."""
    return Event(event_type, "signal", signal.id, payload, **kw)


def on_position(
    position: HatPosition, event_type: str, payload: Mapping[str, Any], **kw: Unpack[Options]
) -> Event:
    """An event about a position; a step it drives moves that position with the trade."""
    kw["position"] = position
    return Event(event_type, "position", position.id, payload, **kw)


def on_order(
    order: HatOrder, event_type: str, payload: Mapping[str, Any], **kw: Unpack[Options]
) -> Event:
    """An event about an order (pass `position` when its step may move one)."""
    return Event(event_type, "order", order.id, payload, **kw)


def record(session: Session, signal: HatSignal, event: Event) -> bool:
    """Append `event` for the trade of `signal` and move the trade along the step it drives, in
    the caller's transaction. True when the state moved. A veto trade's event is flagged `veto`
    and moves only its position (the signal has already ended). A step the table forbids raises
    `LifecycleStateError` before anything is written, unless the event is a broker fact, which
    is written as a non-step (`no_step: true`) and moves nothing."""
    data = dict(event.payload)
    if event.veto:
        data[VETO] = True
    step = lifecycle.step_of(event.type, data)
    moved = False
    if step is not None:
        try:
            if event.veto:
                moved = _move_veto(event.position, event.type, step)
            else:
                moved = _move(signal, event.position, step)
        except lifecycle.LifecycleStateError:
            if not event.broker_fact:
                raise
            logger.error(
                "%s of signal %s kept without a step from %s", event.type, signal.id, signal.state
            )
            data[lifecycle.NO_STEP] = True
    append(
        session,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.type,
        actor=event.actor,
        payload=data,
        correlation_id=signal.id,
        bot_id=signal.bot_id if event.bot_id is None else event.bot_id,
        occurred_at=event.now,
    )
    return moved


def _move(signal: HatSignal, position: HatPosition | None, step: str) -> bool:
    after = lifecycle.next_state(signal.state, step)  # raises before any assignment
    signal.state = after
    if position is not None and after in lifecycle.POSITION_STATES:
        position.state = after
    return True


def _move_veto(position: HatPosition | None, event_type: str, step: str) -> bool:
    if position is None:
        return False
    # The veto position is stored once it opens; before that its trade is EntryPlaced.
    before = lifecycle.ENTRY_PLACED if event_type == "position.opened" else position.state
    position.state = lifecycle.next_state(before, step)
    return True


def approval_sent(signal: HatSignal) -> None:
    """Move a confirm signal to AwaitingApproval. The card has no event: the `hat_approval` row
    the caller writes in the same transaction stands for this step on replay."""
    signal.state = lifecycle.next_state(signal.state, "approval.sent")


# --- replay ---------------------------------------------------------------------------------


def _trade_steps(session: Session, signal_id: uuid.UUID, *, veto: bool) -> list[str]:
    rows = session.execute(
        select(HatEvent.event_type, HatEvent.payload)
        .where(HatEvent.correlation_id == signal_id)
        .order_by(HatEvent.id)
    ).all()
    steps = []
    for event_type, payload in rows:
        if bool((payload or {}).get(VETO)) != veto:
            continue
        step = lifecycle.step_of(event_type, payload)
        if step is not None:
            steps.append(step)
    return steps


def replay_state(session: Session, signal_id: uuid.UUID) -> str:
    """The state the signal's events give when folded over the transition table. The approval
    card has no event of its own: its `hat_approval` row, written in the transaction that moved
    the signal to AwaitingApproval, stands for the step right after `signal.triggered`."""
    steps = _trade_steps(session, signal_id, veto=False)
    if session.get(HatApproval, signal_id) is not None and steps[:1] == [SIGNAL_TRIGGERED]:
        steps.insert(1, "approval.sent")
    return lifecycle.fold(steps)


def replay_veto_state(session: Session, signal_id: uuid.UUID) -> str | None:
    """The state of the signal's veto trade folded from its events (it starts at Triggered, so
    its virtual order moves it to EntryPlaced), or None when it has none."""
    steps = _trade_steps(session, signal_id, veto=True)
    return lifecycle.fold(steps, start=lifecycle.TRIGGERED) if steps else None


# --- the nightly check ----------------------------------------------------------------------


# (signal id, which trade of it (`trade`, or `veto` for its veto trade), the stored state
# (`position:<state>` when the signal's own position disagrees with it), the replayed state or
# `unfoldable`); plain codes, as the alert shows them
Mismatch = tuple[uuid.UUID, str, str, str]
UNFOLDABLE = "unfoldable"
TRADE = "trade"


def _own_position_open() -> Exists:
    """A position of the signal in the enclosing query, its own (not a veto trade), not Closed."""
    return exists().where(
        HatPosition.signal_id == HatSignal.id,
        HatPosition.state != lifecycle.CLOSED,
        or_(HatPosition.is_virtual.is_(False), HatSignal.mode == Mode.SHADOW),
    )


def mismatches(session: Session) -> list[Mismatch]:
    """Every trade not yet ended whose stored state differs from the fold of its events, or whose
    events do not fold (`journal.check`, HAT-EXEC-042): a signal in an active state or holding
    an own position not yet Closed (so a signal stored as ended over a live position is read),
    its own position, and an open veto trade (a virtual position of a confirm or auto signal,
    HAT-EXEC-032) against its own fold. Ordered by signal id, trades first."""
    found = [
        mismatch
        for signal in session.scalars(
            select(HatSignal)
            .where(or_(HatSignal.state.in_(lifecycle.ACTIVE_STATES), _own_position_open()))
            .order_by(HatSignal.id)
        )
        if (mismatch := _trade_mismatch(session, signal)) is not None
    ]
    for position in session.scalars(
        select(HatPosition)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.is_virtual.is_(True),
            HatSignal.mode != Mode.SHADOW,
            HatPosition.state != lifecycle.CLOSED,
        )
        .order_by(HatPosition.signal_id)
    ):
        replayed = _replayed(
            position.signal_id,
            lambda p=position: replay_veto_state(session, p.signal_id) or lifecycle.TRIGGERED,
        )
        if replayed != position.state:
            found.append((position.signal_id, VETO, position.state, replayed))
    return found


def _trade_mismatch(session: Session, signal: HatSignal) -> Mismatch | None:
    """The signal's own trade against its fold; its own position must hold the signal's state."""
    stored, disagrees = signal.state, False
    for position in session.scalars(select(HatPosition).where(HatPosition.signal_id == signal.id)):
        if position.is_virtual and signal.mode != Mode.SHADOW:
            continue  # its veto trade, checked on its own
        held = signal.state in lifecycle.POSITION_STATES or position.state != lifecycle.CLOSED
        if held and position.state != signal.state:
            stored, disagrees = f"position:{position.state}", True  # the rows disagree
    replayed = _replayed(signal.id, lambda: replay_state(session, signal.id))
    return (signal.id, TRADE, stored, replayed) if disagrees or replayed != stored else None


def _replayed(signal_id: uuid.UUID, replay: Callable[[], str]) -> str:
    try:
        return replay()
    except lifecycle.LifecycleStateError:
        logger.exception("events of signal %s do not fold", signal_id)
        return UNFOLDABLE
