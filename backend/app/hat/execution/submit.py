"""Persist before call, idempotent on tag, unconfirmed resolved by tag (HAT-BRK-101, -102,
HAT-SCH-058, HAT-DEC-B24, HAT-BRK-092, -110, -111; 07 §10-§11).

An order is two steps with the database in between:

1. `reserve_order`: insert `hat_order` with status `pending` and its tag, and commit. The tag is
   the idempotency key: its sequence is the number of earlier orders of the same signal and
   purpose, so one logical order has one tag and a new logical order a new one.
2. `send_order(tag)`: the only way an order reaches the broker. It is a no-op for an order that
   was already placed or ended. For one whose outcome is unknown (`unconfirmed`, or `pending`
   with an attempt already stamped in `placed_at`) it looks the order up by tag first: found means
   adopt it. Absent means the same tag may be placed again for a stop or a sale, never for an
   entry: Fyers does not enforce tag uniqueness and its book can lag a placed order, so a second
   entry could double the position (HAT-BRK-102, -103). An entry of unknown outcome is left to
   the fill sync, which adopts it when the book shows it, and to the session end, which ends its
   signal Rejected `entry_not_found` when the book never does.

Every refusal (account not active, no reconciliation this session, mismatch for the bot, no live
session, a virtual order, a non-delivery product, a short entry) is raised before the broker is
called; an intent the adapter refuses (`InvalidIntentError`) is a definite rejection. A GTT
carries its stop_gtt tag at Fyers (HAT-BRK-090), but its book can lag a GTT that landed and Fyers
does not enforce tag uniqueness, so `send_order` never retries an unconfirmed GTT
(`UnresolvedOrderError`): the `StopDesk` settles it against the broker's GTT book once it is old
enough (`find_gtt`: by its tag, else by its shape; `adopt_gtt`, `end_lost_order`).

A sale the broker took and then ended without selling all of it (rejected, cancelled, expired)
records `order.rejected` and fires its exit rules again (`hat_order.rule_ids`), so the next
evaluation retries it under the next tag (HAT-EXEC-075); `rejected_sales` counts both kinds of
rejection toward the two a session allows (HAT-OPS-032).

Each commit below is one atomic step of the model in `verification/tla/OrderReconciliation.tla`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..engine import lifecycle, live_trades, trades
from ..errors import HatError
from ..events import record
from ..models import HatBrokerAccount, HatOrder, HatPosition, HatSignal
from ..time import trade_date_of, utc_now
from .broker import (
    CODE_INVALID_INTENT,
    CODE_OK,
    CODE_UNCONFIRMED,
    CODES_NOT_SENT,
    BrokerGtt,
    BrokerOrder,
    BrokerUnavailableError,
    GttRequest,
    HatBroker,
    InvalidIntentError,
    OrderIntent,
    OrderType,
    SessionStatus,
    Side,
)
from .gtt import gtt_limit_price
from .guard import ExecutionGuard
from .modes import NO_GTT_DELIVERY, real_orders_allowed
from .tags import encode_tag, is_hat_tag

__all__ = [
    "ACCOUNT_ACTIVE",
    "EXIT_SALES",
    "MAX_SALE_REJECTIONS",
    "OrderRefusedError",
    "OrderRequest",
    "SendOutcome",
    "TagBook",
    "UnresolvedOrderError",
    "adopt_broker_order",
    "adopt_gtt",
    "end_lost_order",
    "find_gtt",
    "record_broker_status",
    "rejected_sales",
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
_ENTRY_ENDINGS = {"cancelled", "expired", "rejected"}
EXIT_SALES = ("exit_partial", "exit_full", "flatten")
MAX_SALE_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
# The reason a signal ends Rejected when its entry is first found already ended, unfilled.
ENTRY_NOT_FILLED = "entry_not_filled"


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
    rule_ids: tuple[str, ...] = ()  # a sale's exit rules, fired again if it ends unsold


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
                rule_ids=list(request.rule_ids) or None,
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


@dataclass(frozen=True)
class TagBook:
    """The broker's order book as HAT's rows read it. Fyers does not enforce tag uniqueness
    (HAT-BRK-103), so one tag may name two orders: a row that holds a broker order id is matched
    by that id only, and a row without one adopts the first order under its tag that the broker
    has not ended unfilled (else the first), the one `resolve_by_tag` picks, so every reader of
    one book adopts the same order and a dead duplicate never ends a live one's signal."""

    first: dict[str, BrokerOrder]
    by_id: dict[str, BrokerOrder]

    @classmethod
    def of(cls, orders: Iterable[BrokerOrder]) -> TagBook:
        first: dict[str, BrokerOrder] = {}
        by_id: dict[str, BrokerOrder] = {}
        for order in orders:
            if order.tag is not None and is_hat_tag(order.tag):
                held = first.get(order.tag)
                if held is None or (_dead(held) and not _dead(order)):
                    first[order.tag] = order
                by_id[order.order_id] = order
        return cls(first, by_id)

    def for_row(self, row: HatOrder) -> BrokerOrder | None:
        if row.broker_order_id is None:
            return self.first.get(row.tag)
        found = self.by_id.get(row.broker_order_id)
        return found if found is not None and found.tag == row.tag else None


def _dead(order: BrokerOrder) -> bool:
    return order.status in ("cancelled", "rejected", "expired") and order.filled_qty == 0


def resolve_by_tag(broker: HatBroker, tag: str) -> BrokerOrder | None:
    """The broker's order with `tag`, or None when its order book has none. Raises
    BrokerUnavailableError when the book cannot be read (never "absent" on a failed read)."""
    return TagBook.of(o for o in broker.orders() if o.tag == tag).first.get(tag)


def adopt_broker_order(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """Record a broker order found by tag on its row (no commit). An entry first found already
    ended with nothing filled ends its signal at once (07 §4)."""
    first_knowledge = row.status in ("pending", "unconfirmed")
    row.broker_order_id = found.order_id
    row.status = found.status
    if row.placed_at is None:
        row.placed_at = utc_now()
    if not first_knowledge:
        return
    if found.status in _PLACED_STATES or (row.purpose == ENTRY and found.filled_qty > 0):
        # An entry that filled in part before it ended was placed: its fills open the position.
        _event(session, row, bot_id, _placed_event(row))
    elif row.purpose == ENTRY and found.status in _ENTRY_ENDINGS:
        _end_adopted_entry(session, row, found, bot_id)
    elif _sale_ended_short(row, found):
        _sale_not_sold(session, row, found, bot_id)
    elif found.status == "rejected":
        _event(session, row, bot_id, EVENT_ORDER_REJECTED)
    elif found.status == "cancelled":
        _event(session, row, bot_id, "order.cancelled")


def _end_adopted_entry(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """An entry first found already cancelled, expired or rejected, nothing filled. In
    EntryPlaced its signal ends as a broker update would end it; before that (the send's answer
    was lost, so the signal is still Triggered) the order records a non-final rejection carrying
    the broker status (no lifecycle step) and the signal ends Rejected `entry_not_filled`."""
    signal = session.get(HatSignal, row.signal_id)
    if signal is not None and signal.state == lifecycle.ENTRY_PLACED:
        _entry_ended(session, row, found, bot_id)
        return
    _event(session, row, bot_id, EVENT_ORDER_REJECTED, code=found.status)
    if row.signal_id is not None:
        trades.reject_signal(session, row.signal_id, ENTRY_NOT_FILLED, utc_now())


def _entry_ended(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """The lifecycle event of an entry in EntryPlaced that the broker ended, nothing filled."""
    if found.status in ("cancelled", "expired"):
        _event(session, row, bot_id, "order.cancelled")
    elif found.status == "rejected":
        _event(session, row, bot_id, EVENT_ORDER_REJECTED, code="exchange_rejected", final=True)


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
    if row.purpose == ENTRY and found.filled_qty == 0:
        _entry_ended(session, row, found, bot_id)
    elif _sale_ended_short(row, found):
        _sale_not_sold(session, row, found, bot_id)
    return True


def _sale_ended_short(row: HatOrder, found: BrokerOrder) -> bool:
    return (
        row.purpose in EXIT_SALES and found.status in _ENTRY_ENDINGS and found.filled_qty < row.qty
    )


def _sale_not_sold(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """A sale the broker took and then ended short (HAT-EXEC-075): `order.rejected` carrying the
    broker status (relayed as the health alert), and its rules fired again so the next evaluation
    sells again under the next tag. A partial exit that sold some shares keeps its rules fired
    (firing it again would sell its fraction twice); a full exit sells whatever is left."""
    code = "exchange_rejected" if found.status == "rejected" else found.status
    _event(session, row, bot_id, EVENT_ORDER_REJECTED, code=code)
    if row.purpose != "exit_partial" or found.filled_qty == 0:
        _fire_again(session, row)


def _fire_again(session: Session, row: HatOrder) -> None:
    if row.rule_ids and row.position_id is not None:
        live_trades.unmark_fired(session, row.position_id, row.rule_ids)


def end_lost_order(session: Session, row: HatOrder) -> None:
    """An order whose answer was lost and that the broker's book does not hold after a good read
    (no commit): it never landed, so it ends `expired`, and a sale's rules fire again."""
    row.status = "expired"
    if row.purpose in EXIT_SALES:
        _fire_again(session, row)


def find_gtt(
    gtts: list[BrokerGtt], row: HatOrder, symbol: str, claimed: set[str]
) -> BrokerGtt | None:
    """The broker GTT a stop_gtt row whose answer was lost became, among those no other HAT row
    holds: the one under the row's tag on its symbol; else one shown without a HAT tag (placed
    before GTTs were tagged) on the row's symbol for its quantity and trigger; never one under
    another HAT tag. None when the book has none."""
    free = [gtt for gtt in gtts if gtt.gtt_id not in claimed and gtt.symbol == symbol]
    tagged = next((gtt for gtt in free if gtt.tag == row.tag), None)
    if tagged is not None:
        return tagged
    trigger = Decimal(row.trigger_price) if row.trigger_price is not None else None
    return next(
        (
            gtt
            for gtt in free
            if gtt.tag is None
            and gtt.side is Side.SELL
            and gtt.qty == row.qty
            and gtt.trigger_price == trigger
        ),
        None,
    )


def adopt_gtt(session: Session, row: HatOrder, gtt: BrokerGtt, bot_id: uuid.UUID | None) -> None:
    """Record the broker GTT found for a row whose answer was lost (no commit): placed."""
    row.broker_order_id, row.status = gtt.gtt_id, "placed"
    _event(session, row, bot_id, _placed_event(row))


def rejected_sales(session: Session, signal_id: uuid.UUID, purpose: str, now: datetime) -> int:
    """Sales of `purpose` for the signal the broker rejected today, at once or after taking them
    (ended short with its order id). A lost sale that never landed is not a rejection."""
    start, end = trades._day_bounds(trade_date_of(now))
    count = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == purpose,
            or_(
                HatOrder.status == "rejected",
                and_(
                    HatOrder.status.in_(("cancelled", "expired")),
                    HatOrder.broker_order_id.is_not(None),
                ),
            ),
            HatOrder.placed_at >= start,
            HatOrder.placed_at < end,
        )
    )
    return int(count or 0)


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
        if not real_orders_allowed(
            account.status, account.capabilities or {}, broker.capabilities().gtt_delivery
        ):
            raise OrderRefusedError(NO_GTT_DELIVERY)  # HAT-EXEC-012: no overnight stop
        if broker.session_status() is not SessionStatus.LIVE:
            raise OrderRefusedError("session_not_live")
    elif row.side != Side.SELL.value:
        raise OrderRefusedError("exit_must_sell")


def _lookup_before_retry(
    session: Session, broker: HatBroker, row: HatOrder, tag: str, bot_id: uuid.UUID
) -> SendOutcome | None:
    """An attempt was made before: ask the broker for the tag. An order found is adopted and
    returned; None means the book has none and the same tag may be placed again (a stop or a
    sale). An entry is never placed again (HAT-BRK-102): the book may lag an order that landed."""
    if row.purpose == GTT_PURPOSE:
        # settled against the GTT book once old enough (StopDesk.settle_lost), never sent again
        raise UnresolvedOrderError("a GTT of unknown outcome is settled, never sent again")
    found = resolve_by_tag(broker, tag)
    if found is None:
        if row.purpose == ENTRY:
            raise UnresolvedOrderError("an entry of unknown outcome is never sent again")
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
            tag=tag,
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


def _claim_attempt(session: Session, row: HatOrder, earlier: datetime | None) -> bool:
    """Stamp a new attempt of `row` (now) in one conditional UPDATE on the attempt and status
    this sender read (`earlier`, None before the first): False when another sender (a thread
    or process holding the same tag) stamped one since, so its call may be in flight, or the
    fill sync adopted the order from the book (placed, its attempt time unchanged)."""
    same = HatOrder.placed_at.is_(None) if earlier is None else HatOrder.placed_at == earlier
    claimed = session.execute(
        update(HatOrder)
        .where(
            HatOrder.id == row.id,
            same,
            HatOrder.status == row.status,
            HatOrder.broker_order_id.is_(None),
        )
        .values(placed_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.refresh(row)
    return claimed.rowcount == 1  # type: ignore[attr-defined]


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
    # The attempt is stamped before the call, so a crash is seen as unknown; each attempt is
    # stamped anew, so a lost answer's settle wait runs from the send that may be in flight.
    if not _claim_attempt(session, row, earlier_attempt):
        # Another sender stamped this tag since this one read it: its call may be in flight, so
        # this one sends nothing and reports what that send left (unconfirmed while it is out).
        status = "unconfirmed" if row.status in ("pending", "unconfirmed") else row.status
        code = CODE_UNCONFIRMED if status == "unconfirmed" else CODE_OK
        return SendOutcome(tag, status, row.broker_order_id, code, 0)
    # A reconciliation or restart since the first check may have closed the gate: read it again.
    refusal = guard.refusal(bot_id, row.broker_account_id, is_entry=row.purpose == ENTRY)
    if refusal is not None:
        row.placed_at = earlier_attempt  # nothing was sent
        session.commit()
        raise OrderRefusedError(refusal)

    try:
        status, broker_id, code = _call_broker(broker, request)
    except InvalidIntentError:
        # The adapter refused it before any SDK call: a definite rejection, never a pending
        # attempt that no one resolves.
        status, broker_id, code = "rejected", None, CODE_INVALID_INTENT
    if status == "rejected" and code in CODES_NOT_SENT:
        # Nothing was sent (circuit open, no client) or Fyers refused it unread (the day's
        # requests spent, HAT-BRK-082): not a rejection, so it burns no retry and
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
