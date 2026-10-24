"""Persist before call, idempotent on tag, unconfirmed resolved by tag (HAT-BRK-101, -102,
HAT-SCH-058, HAT-DEC-B24, HAT-BRK-092, -110, -111).

An order is two steps with the database in between:

1. `reserve_order`: insert `hat_order` with status `pending` and its tag, and commit. The tag is
   the idempotency key: its sequence is the number of earlier orders of the same signal and
   purpose, so one logical order has one tag and a new logical order a new one.
2. `send_order(session, sender, tag)`: the only way an order reaches the broker. It is a no-op
   for an order that was already placed or ended. For one whose outcome is unknown
   (`unconfirmed`, or `pending` with an attempt already stamped in `placed_at`) it looks the
   order up by tag first: found means adopt it. Absent means the same tag may be placed again
   for a stop or a sale, never for an entry: Fyers does not enforce tag uniqueness and its book
   can lag a placed order, so a second entry could double the position (HAT-BRK-102, -103). An
   entry of unknown outcome is left to the fill sync, which adopts it when the book shows it, and
   to the session end, which ends its signal Rejected `entry_not_found` when the book never
   does.

Every refusal (account not active, no reconciliation this session, mismatch for the bot, no live
session, a virtual order, a non-delivery product, a short entry) is raised before the broker is
called; an intent the adapter refuses (`InvalidIntentError`) is a definite rejection. A GTT
carries its stop_gtt tag at Fyers (HAT-BRK-090), but its book can lag a GTT that landed and Fyers
does not enforce tag uniqueness, so `send_order` never retries an unconfirmed GTT
(`UnresolvedOrderError`): the `StopDesk` settles it against the broker's GTT book once it is old
enough (`order_book.find_gtt`: by its tag, else by its shape; `adopt_gtt`, `end_lost_order`).
`sender` (`OrderSender`) binds one account's broker, order gate and clock; the symbol, the bot
and a GTT's tick are read from the row, its signal and its position.

What a found order or a later book status does to the row and its signal is `order_book.py`.

Each commit below is one atomic step of the model in `verification/tla/OrderReconciliation.tla`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import Clock
from ..errors import HatError
from ..models import HatBrokerAccount, HatInstrument, HatOrder, HatPosition, HatSignal
from ..models.strategy import ACCOUNT_ACTIVE
from ..time import utc_now
from .broker import (
    CODE_INVALID_INTENT,
    CODE_OK,
    CODE_UNCONFIRMED,
    CODES_NOT_SENT,
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
from .order_book import (
    ENTRY,
    EVENT_ORDER_REJECTED,
    GTT_PURPOSE,
    adopt_broker_order,
    order_event,
    placed_event,
    resolve_by_tag,
)
from .position_desk import Verdict, found_verdict
from .tags import encode_tag

__all__ = [
    "AwaitingFillsError",
    "OrderRefusedError",
    "OrderRequest",
    "OrderSender",
    "Retry",
    "SendOutcome",
    "TickSource",
    "UnresolvedOrderError",
    "reserve_order",
    "retry_verdict",
    "send_order",
]

_ENDED = {"placed", "partially_filled", "filled", "cancelled", "rejected", "expired"}

# The instrument's tick for a position's stop prices (`data.ticks.tick_for_date`, HAT-EXEC-053).
TickSource = Callable[[Session, HatPosition], Decimal | None]


class OrderRefusedError(HatError):
    """The order was refused before the broker was called. `code` is a fixed word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnresolvedOrderError(HatError):
    """An order whose outcome is unknown and cannot be looked up by tag: not retried."""


class AwaitingFillsError(UnresolvedOrderError):
    """A stop or sale whose answer was lost, found in the broker's book with shares sold: not
    sent again; the fill sync adopts it with its fills (`found_verdict`)."""


@dataclass(frozen=True)
class OrderRequest:
    """One logical order."""

    bot_id: uuid.UUID
    signal_id: uuid.UUID
    broker_account_id: uuid.UUID
    purpose: str
    side: Side
    qty: int
    order_type: OrderType
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


@dataclass(frozen=True)
class OrderSender:
    """What sends one broker account's orders: its broker, its order gate, the clock that stamps
    each attempt (`placed_at`) and, for the stop desk, the tick a GTT's limit price is rounded to
    (`tick_for`; without it a GTT is refused before anything is stamped)."""

    broker: HatBroker
    guard: ExecutionGuard
    clock: Clock
    tick_for: TickSource | None = None


@dataclass(frozen=True)
class _Prepared:
    """A send that passed its checks: the broker request and the bot it is for."""

    request: GttRequest | OrderIntent
    bot_id: uuid.UUID


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


def _check_allowed(
    session: Session, broker: HatBroker, guard: ExecutionGuard, row: HatOrder, bot_id: uuid.UUID
) -> None:
    if row.is_virtual:
        raise OrderRefusedError("virtual_order")
    is_entry = row.purpose == ENTRY
    refusal = guard.refusal(bot_id, row.broker_account_id, is_entry=is_entry)
    if refusal is not None:
        raise OrderRefusedError(refusal)
    if is_entry:
        # Only an entry needs an active account: a stop or a sale goes out whatever the row's
        # status (exits are never blocked, D118); the broker session refuses it if it must.
        account = session.get(HatBrokerAccount, row.broker_account_id)
        if account is None or account.status != ACCOUNT_ACTIVE:
            raise OrderRefusedError("account_not_active")
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


class Retry(StrEnum):
    """What a retry of an order of unknown outcome does after the lookup by tag."""

    SEND = "send"  # the book has none: place the same tag again (a stop or a sale)
    ADOPT = "adopt"  # the book holds it: record what the broker shows
    AWAIT_FILLS = "await_fills"  # a stop or sale found with shares sold: the fill sync adopts it
    NEVER = "never"  # an entry or a GTT of unknown outcome is never sent again


def retry_verdict(purpose: str, filled_qty: int | None) -> Retry:
    """The retry of an order of `purpose` whose earlier attempt has no known outcome;
    `filled_qty` is the shares the broker's book shows sold under its tag, None when the book
    has no order with the tag. A GTT is settled against the GTT book (`StopDesk.settle_lost`)
    and an entry left to the fill sync and the session end (HAT-BRK-102): the book may lag an
    order that landed, so neither is ever sent again."""
    if purpose == GTT_PURPOSE:
        return Retry.NEVER
    if filled_qty is None:
        return Retry.NEVER if purpose == ENTRY else Retry.SEND
    # found_verdict, not settle_verdict: nothing found here means send again at once (an entry
    # never), not a settle wait; the found branch is the one they share
    if purpose != ENTRY and found_verdict(filled_qty) is Verdict.WAIT:
        return Retry.AWAIT_FILLS
    return Retry.ADOPT


def _lookup_before_retry(
    session: Session, broker: HatBroker, row: HatOrder, bot_id: uuid.UUID, clock: Clock
) -> SendOutcome | None:
    """An attempt was made before: ask the broker for the tag and act on `retry_verdict`. An
    order found is adopted and returned; None means the same tag may be placed again."""
    if row.purpose == GTT_PURPOSE:
        raise UnresolvedOrderError("a GTT of unknown outcome is settled, never sent again")
    found = resolve_by_tag(broker, row.tag)
    verdict = retry_verdict(row.purpose, None if found is None else found.filled_qty)
    if verdict is Retry.NEVER:
        raise UnresolvedOrderError("an entry of unknown outcome is never sent again")
    if verdict is Retry.SEND or found is None:
        return None
    if verdict is Retry.AWAIT_FILLS:
        raise AwaitingFillsError("found with shares sold: the fill sync adopts it")
    adopt_broker_order(session, row, found, bot_id, clock=clock)
    session.commit()
    return SendOutcome(row.tag, row.status, row.broker_order_id, CODE_OK, 0)


def _order_of(session: Session, row: HatOrder) -> tuple[uuid.UUID, str, HatPosition | None]:
    """The bot, the instrument's current symbol and the position of the row's order: the
    position's instrument for a stop or a sale (after a series change the adapter maps it to the
    new series' Fyers symbol, HAT-EXEC-146), the signal's for an entry."""
    signal = session.get(HatSignal, row.signal_id) if row.signal_id is not None else None
    if signal is None:
        raise OrderRefusedError("unknown_signal")
    position = session.get(HatPosition, row.position_id) if row.position_id is not None else None
    instrument_id = position.instrument_id if position is not None else signal.instrument_id
    symbol = session.scalar(select(HatInstrument.symbol).where(HatInstrument.id == instrument_id))
    if symbol is None:
        raise OrderRefusedError("unknown_instrument")
    return signal.bot_id, symbol, position


def _build_request(
    row: HatOrder, symbol: str, tick_size: Decimal | None
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
            tag=row.tag,
        )
    return OrderIntent(
        symbol=symbol,
        side=Side(row.side),
        qty=row.qty,
        order_type=OrderType(row.order_type),
        tag=row.tag,
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


def _claim_attempt(session: Session, row: HatOrder, earlier: datetime | None, clock: Clock) -> bool:
    """Stamp a new attempt of `row` (at `clock`'s now) in one conditional UPDATE on the attempt
    and status this sender read (`earlier`, None before the first): False when another sender
    (a thread or process holding the same tag) stamped one since, so its call may be in flight,
    or the fill sync adopted the order from the book (placed, its attempt time unchanged)."""
    same = HatOrder.placed_at.is_(None) if earlier is None else HatOrder.placed_at == earlier
    claimed = session.execute(
        update(HatOrder)
        .where(
            HatOrder.id == row.id,
            same,
            HatOrder.status == row.status,
            HatOrder.broker_order_id.is_(None),
        )
        .values(placed_at=utc_now(clock))
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.refresh(row)
    return claimed.rowcount == 1  # type: ignore[attr-defined]


def send_order(
    session: Session, sender: OrderSender, tag: str, *, rule: str | None = None
) -> SendOutcome:
    """Send the reserved order `tag` to the broker, at most once per tag. Safe to call again.
    `rule` names the exit rule of a sale; it is kept on the order's `order.placed` event."""
    row = _row(session, tag)
    prepared = _prepare(session, sender, row)
    if isinstance(prepared, SendOutcome):
        return prepared
    return _attempt(session, sender, row, prepared, rule)


def _prepare(session: Session, sender: OrderSender, row: HatOrder) -> _Prepared | SendOutcome:
    """The checks before an attempt (nothing is stamped): the outcome of an order already ended
    or found by its tag, else the request to send. Raises a refusal or an unresolved order."""
    if row.status in _ENDED:
        return SendOutcome(row.tag, row.status, row.broker_order_id, CODE_OK, 0)
    bot_id, symbol, position = _order_of(session, row)
    _check_allowed(session, sender.broker, sender.guard, row, bot_id)
    if row.status == "unconfirmed" or row.placed_at is not None:
        adopted = _lookup_before_retry(session, sender.broker, row, bot_id, sender.clock)
        if adopted is not None:
            return adopted
        # The lookup returned absent: the same tag may be placed again.
    tick = None
    if row.purpose == GTT_PURPOSE and position is not None and sender.tick_for is not None:
        tick = sender.tick_for(session, position)
    return _Prepared(_build_request(row, symbol, tick), bot_id)  # may refuse: nothing stamped


def _attempt(
    session: Session, sender: OrderSender, row: HatOrder, prepared: _Prepared, rule: str | None
) -> SendOutcome:
    """Claim the attempt, call the broker once and record its answer, each step its own commit."""
    tag, bot_id = row.tag, prepared.bot_id
    earlier_attempt = row.placed_at
    # The attempt is stamped before the call, so a crash is seen as unknown; each attempt is
    # stamped anew, so a lost answer's settle wait runs from the send that may be in flight.
    if not _claim_attempt(session, row, earlier_attempt, sender.clock):
        # Another sender stamped this tag since this one read it: its call may be in flight, so
        # this one sends nothing and reports what that send left (unconfirmed while it is out).
        status = "unconfirmed" if row.status in ("pending", "unconfirmed") else row.status
        code = CODE_UNCONFIRMED if status == "unconfirmed" else CODE_OK
        return SendOutcome(tag, status, row.broker_order_id, code, 0)
    # A reconciliation or restart since the first check may have closed the gate: read it again.
    refusal = sender.guard.refusal(bot_id, row.broker_account_id, is_entry=row.purpose == ENTRY)
    if refusal is not None:
        row.placed_at = earlier_attempt  # nothing was sent
        session.commit()
        raise OrderRefusedError(refusal)

    try:
        status, broker_id, code = _call_broker(sender.broker, prepared.request)
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
        order_event(session, row, bot_id, placed_event(row), utc_now(sender.clock), rule=rule)
    elif status == "rejected":
        row.status = "rejected"
        order_event(session, row, bot_id, EVENT_ORDER_REJECTED, utc_now(sender.clock), code)
    else:
        row.status, code = "unconfirmed", CODE_UNCONFIRMED
    session.commit()
    return SendOutcome(tag, row.status, row.broker_order_id, code, 1)
