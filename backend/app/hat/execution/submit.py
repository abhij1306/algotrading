"""Persist before call, idempotent on tag, unconfirmed resolved by tag (HAT-BRK-101, -102,
HAT-SCH-058, HAT-DEC-B24, HAT-BRK-092, -110, -111; 07 §10-§11).

An order is two steps with the database in between:

1. `reserve_order`: insert `hat_order` with status `pending` and its tag, and commit. The tag is
   the idempotency key: its sequence is the number of earlier orders of the same signal and
   purpose, so one logical order has one tag and a new logical order a new one.
2. `send_order(tag)`: the only way an order reaches the broker. It is a no-op for an order that
   was already placed or ended. For one whose outcome is unknown (`unconfirmed`, or `pending`
   with an attempt already stamped in `placed_at`) it looks the order up by tag first: found means
   adopt it, absent means the same tag may be placed again.

Every refusal (account not active, no reconciliation this session, mismatch for the bot, no live
session, a virtual order, a non-delivery product, a short entry) is raised before the broker is
called. GTT carries no tag at Fyers, so an unconfirmed GTT cannot be looked up: it is not retried
(`UnresolvedOrderError`) and waits for the nightly GTT verify.

Each commit below is one atomic step of the model in `verification/tla/OrderReconciliation.tla`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..engine import lifecycle
from ..errors import HatError
from ..events import record
from ..models import HatBrokerAccount, HatOrder, HatPosition, HatSignal
from ..time import utc_now
from .broker import (
    CODE_OK,
    CODE_UNAVAILABLE,
    CODE_UNCONFIRMED,
    BrokerOrder,
    BrokerUnavailableError,
    GttRequest,
    HatBroker,
    OrderIntent,
    OrderType,
    SessionStatus,
    Side,
)
from .gtt import gtt_limit_price
from .guard import ExecutionGuard
from .tags import encode_tag

__all__ = [
    "ACCOUNT_ACTIVE",
    "OrderRefusedError",
    "OrderRequest",
    "SendOutcome",
    "UnresolvedOrderError",
    "adopt_broker_order",
    "record_broker_status",
    "reserve_order",
    "resolve_by_tag",
    "send_order",
]

logger = logging.getLogger(__name__)

ACCOUNT_ACTIVE = "active"
ENTRY = "entry"
GTT_PURPOSE = "stop_gtt"
EVENT_ORDER_REJECTED = "order.rejected"
_ENDED = {"placed", "partially_filled", "filled", "cancelled", "rejected", "expired"}
_PLACED_STATES = {"placed", "partially_filled", "filled"}


class OrderRefusedError(HatError):
    """The order was refused before the broker was called. `code` is a fixed word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnresolvedOrderError(HatError):
    """An order whose outcome is unknown and cannot be looked up by tag: not retried."""


@dataclass(frozen=True)
class OrderRequest:
    """One logical order. `symbol` is the database symbol of the signal's instrument."""

    bot_id: uuid.UUID
    signal_id: uuid.UUID
    broker_account_id: uuid.UUID
    purpose: str
    side: Side
    qty: int
    order_type: OrderType
    symbol: str
    position_id: uuid.UUID | None = None
    price: Decimal | None = None
    trigger_price: Decimal | None = None


@dataclass(frozen=True)
class SendOutcome:
    tag: str
    status: str
    broker_order_id: str | None
    code: str
    broker_calls: int  # place calls this send made (0 for a no-op or an adopted order)


def next_sequence(session: Session, signal_id: uuid.UUID, purpose: str) -> int:
    """The sequence of a new order: how many orders this signal already has for the purpose."""
    return session.execute(
        select(func.count())
        .select_from(HatOrder)
        .where(HatOrder.signal_id == signal_id, HatOrder.purpose == purpose)
    ).scalar_one()


def reserve_order(session: Session, request: OrderRequest, *, retries: int = 3) -> str:
    """Insert the PENDING row and commit it; return its tag. Raises TagExhaustedError when the
    signal already has 36 orders for the purpose."""
    for _ in range(retries):
        tag = encode_tag(
            request.signal_id,
            request.purpose,
            next_sequence(session, request.signal_id, request.purpose),
        )
        session.add(
            HatOrder(
                signal_id=request.signal_id,
                position_id=request.position_id,
                broker_account_id=request.broker_account_id,
                purpose=request.purpose,
                side=request.side.value,
                qty=request.qty,
                order_type=request.order_type.value,
                price=request.price,
                trigger_price=request.trigger_price,
                tag=tag,
                status="pending",
                is_virtual=False,
                currency="INR",
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            if session.execute(select(HatOrder.id).where(HatOrder.tag == tag)).first() is None:
                raise OrderRefusedError("reservation_failed") from None  # not a tag race
            continue  # a concurrent reserve took this sequence: count again
        return tag
    raise OrderRefusedError("tag_conflict")


def _row(session: Session, tag: str) -> HatOrder:
    row = session.execute(select(HatOrder).where(HatOrder.tag == tag)).scalar_one_or_none()
    if row is None:
        raise OrderRefusedError("unknown_tag")
    return row


def resolve_by_tag(broker: HatBroker, tag: str) -> BrokerOrder | None:
    """The broker's order with `tag`, or None when its order book has none. Raises
    BrokerUnavailableError when the book cannot be read (never "absent" on a failed read)."""
    return next((o for o in broker.orders() if o.tag == tag), None)


def adopt_broker_order(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """Record a broker order found by tag on its row (no commit)."""
    first_knowledge = row.status in ("pending", "unconfirmed")
    row.broker_order_id = found.order_id
    row.status = found.status
    if row.placed_at is None:
        row.placed_at = utc_now()
    if first_knowledge and found.status in _PLACED_STATES:
        _event(session, row, bot_id, _placed_event(row))
    elif first_knowledge and found.status == "rejected":
        _event(session, row, bot_id, EVENT_ORDER_REJECTED)
    elif first_knowledge and found.status == "cancelled":
        _event(session, row, bot_id, "order.cancelled")


def record_broker_status(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> bool:
    """Move a placed order to the status the broker now shows (no commit); False when unchanged.
    An entry that ended with nothing filled records `order.cancelled` when it was cancelled or a
    DAY limit expired at the close (its signal ends Cancelled), and a final `order.rejected` when
    the exchange rejected it after the broker took it (its signal ends Rejected); any other
    change is the order's own bookkeeping (fills are recorded from the trade book, stops have no
    lifecycle step)."""
    if found.status == row.status:
        return False
    row.status = found.status
    if row.purpose != ENTRY or found.filled_qty != 0:
        return True
    if found.status in ("cancelled", "expired"):
        _event(session, row, bot_id, "order.cancelled")
    elif found.status == "rejected":
        _event(session, row, bot_id, EVENT_ORDER_REJECTED, code="exchange_rejected", final=True)
    return True


def _placed_event(row: HatOrder) -> str:
    return "gtt.placed" if row.purpose == GTT_PURPOSE else "order.placed"


def _event(
    session: Session,
    row: HatOrder,
    bot_id: uuid.UUID | None,
    event_type: str,
    code: str = CODE_OK,
    rule: str | None = None,
    final: bool = False,
) -> None:
    payload: dict[str, object] = {
        "tag": row.tag,
        "purpose": row.purpose,
        "broker_order_id": row.broker_order_id,
        "code": code,
    }
    if final:
        payload["final"] = True  # no retry follows: the lifecycle step to Rejected (07 §4)
    if rule is not None:
        payload["rule"] = rule  # the exit rule a sale is for (its trade result's exit reason)
    record(
        session,
        aggregate_type="order",
        aggregate_id=row.id,
        event_type=event_type,
        actor="engine",
        payload=payload,
        correlation_id=row.signal_id,
        bot_id=bot_id,
    )
    _advance(session, row, event_type, payload)


def _advance(session: Session, row: HatOrder, event_type: str, payload: dict[str, object]) -> None:
    """Move the order's signal along the lifecycle step its event drives, in the same commit
    (HAT-EXEC-042): an entry `order.placed` takes a Triggered or AwaitingApproval signal to
    EntryPlaced, and a full exit order's takes an open trade (signal and position) to
    ExitPending. An order event the table does not allow from the signal's state is logged and
    leaves the state: the order's own bookkeeping must never fail after a broker call."""
    step = lifecycle.step_of(event_type, payload)
    if step is None:
        return
    signal = session.get(HatSignal, row.signal_id)
    if signal is None:
        return
    try:
        signal.state = lifecycle.next_state(signal.state, step)
    except lifecycle.LifecycleStateError:
        logger.error("order event %s does not move a signal in %s", event_type, signal.state)
        return
    position = session.get(HatPosition, row.position_id) if row.position_id else None
    if position is not None and signal.state in lifecycle.POSITION_STATES:
        position.state = signal.state


def _check_allowed(
    session: Session, broker: HatBroker, guard: ExecutionGuard, row: HatOrder, bot_id: uuid.UUID
) -> None:
    if row.is_virtual:
        raise OrderRefusedError("virtual_order")
    account = session.get(HatBrokerAccount, row.broker_account_id)
    if account is None or account.status != ACCOUNT_ACTIVE:
        raise OrderRefusedError("account_not_active")
    is_entry = row.purpose == ENTRY
    refusal = guard.refusal(bot_id, row.broker_account_id, is_entry=is_entry)
    if refusal is not None:
        raise OrderRefusedError(refusal)
    if is_entry:
        if row.side != Side.BUY.value:
            raise OrderRefusedError("entry_must_buy")
        if broker.session_status() is not SessionStatus.LIVE:
            raise OrderRefusedError("session_not_live")
    elif row.side != Side.SELL.value:
        raise OrderRefusedError("exit_must_sell")


def _lookup_before_retry(
    session: Session, broker: HatBroker, row: HatOrder, tag: str, bot_id: uuid.UUID
) -> SendOutcome | None:
    """An attempt was made before: ask the broker for the tag. An order found is adopted and
    returned; None means the book has none and the same tag may be placed again."""
    if row.purpose == GTT_PURPOSE:
        raise UnresolvedOrderError("a GTT has no tag to look up")
    found = resolve_by_tag(broker, tag)
    if found is None:
        return None
    adopt_broker_order(session, row, found, bot_id)
    session.commit()
    return SendOutcome(tag, row.status, row.broker_order_id, CODE_OK, 0)


def _build_request(
    row: HatOrder, tag: str, symbol: str, tick_size: Decimal | None
) -> GttRequest | OrderIntent:
    """The broker request for the row. Raises before anything is stamped or sent when a GTT
    lacks its trigger or tick, or has no valid limit price."""
    if row.purpose == GTT_PURPOSE:
        if tick_size is None or row.trigger_price is None:
            raise OrderRefusedError("gtt_needs_trigger_and_tick")
        return GttRequest(
            symbol=symbol,
            qty=row.qty,
            trigger_price=row.trigger_price,
            limit_price=gtt_limit_price(row.trigger_price, tick_size),
        )
    return OrderIntent(
        symbol=symbol,
        side=Side(row.side),
        qty=row.qty,
        order_type=OrderType(row.order_type),
        tag=tag,
        price=row.price,
        trigger_price=row.trigger_price,
    )


def _call_broker(
    broker: HatBroker, request: GttRequest | OrderIntent
) -> tuple[str, str | None, str]:
    """Place the order or the GTT; return (status, broker id, code)."""
    if isinstance(request, GttRequest):
        result = broker.place_gtt_stop(request)
        return result.status, result.gtt_id, result.code
    outcome = broker.place_order(request)
    return outcome.status, outcome.broker_order_id, outcome.code


def send_order(
    session: Session,
    broker: HatBroker,
    guard: ExecutionGuard,
    tag: str,
    *,
    symbol: str,
    bot_id: uuid.UUID,
    tick_size: Decimal | None = None,
    rule: str | None = None,
) -> SendOutcome:
    """Send the reserved order `tag` to the broker, at most once per tag. Safe to call again.
    `rule` names the exit rule of a sale; it is kept on the order's `order.placed` event."""
    row = _row(session, tag)
    if row.status in _ENDED:
        return SendOutcome(tag, row.status, row.broker_order_id, CODE_OK, 0)
    _check_allowed(session, broker, guard, row, bot_id)

    if row.status == "unconfirmed" or row.placed_at is not None:
        adopted = _lookup_before_retry(session, broker, row, tag, bot_id)
        if adopted is not None:
            return adopted
        # The lookup returned absent: the same tag may be placed again.
    request = _build_request(row, tag, symbol, tick_size)  # may refuse: nothing stamped yet
    earlier_attempt = row.placed_at
    row.placed_at = utc_now()
    session.commit()  # the attempt is stamped before the call, so a crash is seen as unknown
    # A reconciliation or restart since the first check may have closed the gate: read it again.
    refusal = guard.refusal(bot_id, row.broker_account_id, is_entry=row.purpose == ENTRY)
    if refusal is not None:
        row.placed_at = earlier_attempt  # nothing was sent
        session.commit()
        raise OrderRefusedError(refusal)

    status, broker_id, code = _call_broker(broker, request)
    if status == "rejected" and code == CODE_UNAVAILABLE:
        # Nothing was sent (circuit open, no client): not a rejection, so it burns no retry and
        # leaves no attempt marker (a GTT with one could not be sent again).
        row.placed_at = earlier_attempt
        session.commit()
        raise BrokerUnavailableError(code)
    if status == "placed":
        row.status, row.broker_order_id = "placed", broker_id
        _event(session, row, bot_id, _placed_event(row), rule=rule)
    elif status == "rejected":
        row.status = "rejected"
        _event(session, row, bot_id, EVENT_ORDER_REJECTED, code)
    else:
        row.status, code = "unconfirmed", CODE_UNCONFIRMED
    session.commit()
    return SendOutcome(tag, row.status, row.broker_order_id, code, 1)
