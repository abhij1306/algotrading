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

from ..errors import HatError
from ..events import record
from ..models import HatBrokerAccount, HatOrder
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
    "reserve_order",
    "resolve_by_tag",
    "send_order",
]

logger = logging.getLogger(__name__)

ACCOUNT_ACTIVE = "active"
ENTRY = "entry"
GTT_PURPOSE = "stop_gtt"
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
            session.rollback()  # a concurrent reserve took this sequence: count again
            continue
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
        _event(session, row, bot_id, "order.rejected")
    elif first_knowledge and found.status == "cancelled":
        _event(session, row, bot_id, "order.cancelled")


def _placed_event(row: HatOrder) -> str:
    return "gtt.placed" if row.purpose == GTT_PURPOSE else "order.placed"


def _event(
    session: Session,
    row: HatOrder,
    bot_id: uuid.UUID | None,
    event_type: str,
    code: str = CODE_OK,
) -> None:
    record(
        session,
        aggregate_type="order",
        aggregate_id=row.id,
        event_type=event_type,
        actor="engine",
        payload={
            "tag": row.tag,
            "purpose": row.purpose,
            "broker_order_id": row.broker_order_id,
            "code": code,
        },
        correlation_id=row.signal_id,
        bot_id=bot_id,
    )


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


def send_order(
    session: Session,
    broker: HatBroker,
    guard: ExecutionGuard,
    tag: str,
    *,
    symbol: str,
    bot_id: uuid.UUID,
    tick_size: Decimal | None = None,
) -> SendOutcome:
    """Send the reserved order `tag` to the broker, at most once per tag. Safe to call again."""
    row = _row(session, tag)
    if row.status in _ENDED:
        return SendOutcome(tag, row.status, row.broker_order_id, CODE_OK, 0)
    _check_allowed(session, broker, guard, row, bot_id)

    attempted = row.status == "unconfirmed" or row.placed_at is not None
    if attempted:
        if row.purpose == GTT_PURPOSE:
            raise UnresolvedOrderError("a GTT has no tag to look up")
        found = resolve_by_tag(broker, tag)
        if found is not None:
            adopt_broker_order(session, row, found, bot_id)
            session.commit()
            return SendOutcome(tag, row.status, row.broker_order_id, CODE_OK, 0)
        # The lookup returned absent: the same tag may be placed again.
    row.placed_at = utc_now()
    session.commit()  # the attempt is stamped before the call, so a crash is seen as unknown
    # A reconciliation or restart since the first check may have closed the gate: read it again.
    refusal = guard.refusal(bot_id, row.broker_account_id, is_entry=row.purpose == ENTRY)
    if refusal is not None:
        raise OrderRefusedError(refusal)

    if row.purpose == GTT_PURPOSE:
        if tick_size is None or row.trigger_price is None:
            raise OrderRefusedError("gtt_needs_trigger_and_tick")
        result = broker.place_gtt_stop(
            GttRequest(
                symbol=symbol,
                qty=row.qty,
                trigger_price=row.trigger_price,
                limit_price=gtt_limit_price(row.trigger_price, tick_size),
            )
        )
        status, broker_id, code = result.status, result.gtt_id, result.code
    else:
        outcome = broker.place_order(
            OrderIntent(
                symbol=symbol,
                side=Side(row.side),
                qty=row.qty,
                order_type=OrderType(row.order_type),
                tag=tag,
                price=row.price,
                trigger_price=row.trigger_price,
            )
        )
        status, broker_id, code = outcome.status, outcome.broker_order_id, outcome.code

    if status == "rejected" and code == CODE_UNAVAILABLE:
        # Nothing was sent (circuit open, no client): not a rejection, so it burns no retry.
        session.commit()
        raise BrokerUnavailableError(code)
    if status == "placed":
        row.status, row.broker_order_id = "placed", broker_id
        _event(session, row, bot_id, _placed_event(row))
    elif status == "rejected":
        row.status = "rejected"
        _event(session, row, bot_id, "order.rejected", code)
    else:
        row.status, code = "unconfirmed", CODE_UNCONFIRMED
    session.commit()
    return SendOutcome(tag, row.status, row.broker_order_id, code, 1)
