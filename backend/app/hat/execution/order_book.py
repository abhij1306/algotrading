"""HAT's rows against the broker's books: what a broker order found by tag, or a status the book
now shows, does to an order row and its signal (HAT-BRK-102, -103, HAT-EXEC-075, HAT-OPS-032).

`TagBook` matches book orders to rows (Fyers does not enforce tag uniqueness). A found order is
adopted (`adopt_broker_order`); a placed one moves to the status the book shows
(`record_broker_status`). A sale the broker took and then ended without selling all of it
(rejected, cancelled, expired) records `order.rejected` and fires its exit rules again
(`hat_order.rule_ids`), so the next evaluation retries it under the next tag (HAT-EXEC-075);
`rejected_sales` counts both kinds of rejection toward the two a session allows (HAT-OPS-032). A
market sale HAT itself cancelled to send it again fresh (`mark_resend`, PR 48: under market price
protection a market sale can rest as a protected limit) is not a rejection: it records
`order.cancelled` (code `sale_resend`, with the order type and price the broker's book showed)
when HAT asks, and when it ends its rules fire again without `order.rejected`.

A GTT whose answer was lost is matched against the GTT book (`find_gtt`: by its tag, else by its
shape) and adopted (`adopt_gtt`) or ended (`end_lost_order`) by the `StopDesk`. None of these
commits: each runs inside one atomic step of `verification/tla/OrderReconciliation.tla` that its
caller commits.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Exists

from ..clock import Clock
from ..engine import entries, journal, lifecycle, live_trades
from ..events import record
from ..models import HatEvent, HatOrder, HatPosition, HatSignal
from ..time import day_bounds, trade_date_of, utc_now
from .broker import CODE_OK, BrokerGtt, BrokerOrder, HatBroker, Side
from .tags import is_hat_tag

__all__ = [
    "ENTRY",
    "EVENT_ORDER_CANCELLED",
    "EVENT_ORDER_REJECTED",
    "EXIT_SALES",
    "GTT_PURPOSE",
    "MAX_SALE_REJECTIONS",
    "RESEND",
    "TagBook",
    "adopt_broker_order",
    "adopt_gtt",
    "end_lost_order",
    "find_gtt",
    "mark_resend",
    "order_event",
    "placed_event",
    "record_broker_status",
    "rejected_sales",
    "resend_event",
    "resend_requested",
    "resolve_by_tag",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
GTT_PURPOSE = "stop_gtt"
EVENT_ORDER_REJECTED = "order.rejected"
EVENT_ORDER_CANCELLED = "order.cancelled"
# The code of the `order.cancelled` HAT records when it cancels a resting market sale to send it
# again fresh (PR 48); such a sale's end is not a rejection.
RESEND = "sale_resend"
_PLACED_STATES = {"placed", "partially_filled", "filled"}
_ENTRY_ENDINGS = {"cancelled", "expired", "rejected"}
EXIT_SALES = ("exit_partial", "exit_full", "flatten")
MAX_SALE_REJECTIONS = 2  # the original and one retry (HAT-OPS-032)
# The reason a signal ends Rejected when its entry is first found already ended, unfilled.
ENTRY_NOT_FILLED = "entry_not_filled"


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
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None, *, clock: Clock
) -> None:
    """Record a broker order found by tag on its row (no commit). An entry first found already
    ended with nothing filled ends its signal at once. `clock` stamps a first-known attempt and
    the entry's rejection."""
    first_knowledge = row.status in ("pending", "unconfirmed")
    row.broker_order_id = found.order_id
    row.status = found.status
    if row.placed_at is None:
        row.placed_at = utc_now(clock)
    if not first_knowledge:
        return
    if found.status in _PLACED_STATES or (row.purpose == ENTRY and found.filled_qty > 0):
        # An entry that filled in part before it ended was placed: its fills open the position.
        order_event(session, row, bot_id, placed_event(row))
    elif row.purpose == ENTRY and found.status in _ENTRY_ENDINGS:
        _end_adopted_entry(session, row, found, bot_id, clock)
    elif _sale_ended_short(row, found):
        _sale_not_sold(session, row, found, bot_id)
    elif found.status == "rejected":
        order_event(session, row, bot_id, EVENT_ORDER_REJECTED)
    elif found.status == "cancelled":
        order_event(session, row, bot_id, EVENT_ORDER_CANCELLED)


def _end_adopted_entry(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None, clock: Clock
) -> None:
    """An entry first found already cancelled, expired or rejected, nothing filled. In
    EntryPlaced its signal ends as a broker update would end it; before that (the send's answer
    was lost, so the signal is still Triggered) the order records a non-final rejection carrying
    the broker status (no lifecycle step) and the signal ends Rejected `entry_not_filled`."""
    signal = session.get(HatSignal, row.signal_id)
    if signal is not None and signal.state == lifecycle.ENTRY_PLACED:
        _entry_ended(session, row, found, bot_id)
        return
    order_event(session, row, bot_id, EVENT_ORDER_REJECTED, code=found.status)
    if row.signal_id is not None:
        entries.reject_signal(session, row.signal_id, ENTRY_NOT_FILLED, utc_now(clock))


def _entry_ended(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """The lifecycle event of an entry in EntryPlaced that the broker ended, nothing filled."""
    if found.status in ("cancelled", "expired"):
        order_event(session, row, bot_id, EVENT_ORDER_CANCELLED)
    elif found.status == "rejected":
        order_event(
            session, row, bot_id, EVENT_ORDER_REJECTED, code="exchange_rejected", final=True
        )


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
    resent = found.status == "cancelled" and resend_requested(session, row)
    if not resent:  # HAT cancelled it to send it again (PR 48): not a rejection
        code = "exchange_rejected" if found.status == "rejected" else found.status
        order_event(session, row, bot_id, EVENT_ORDER_REJECTED, code=code)
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
    holds: the one under the row's tag on its symbol; else one shown untagged (placed before GTTs
    were tagged) on the row's symbol for its quantity and trigger; never one under another tag,
    HAT's or foreign. None when the book has none."""
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
            and not gtt.foreign
            and gtt.side is Side.SELL
            and gtt.qty == row.qty
            and gtt.trigger_price == trigger
        ),
        None,
    )


def adopt_gtt(session: Session, row: HatOrder, gtt: BrokerGtt, bot_id: uuid.UUID | None) -> None:
    """Record the broker GTT found for a row whose answer was lost (no commit): placed."""
    row.broker_order_id, row.status = gtt.gtt_id, "placed"
    order_event(session, row, bot_id, placed_event(row))


def resend_event() -> Exists:
    """The `order.cancelled` (code `sale_resend`) of the order row in the enclosing query."""
    return (
        select(HatEvent.id)
        .where(
            HatEvent.aggregate_id == HatOrder.id,
            HatEvent.event_type == EVENT_ORDER_CANCELLED,
            HatEvent.payload["code"].as_string() == RESEND,
        )
        .exists()
    )


def rejected_sales(session: Session, signal_id: uuid.UUID, purpose: str, now: datetime) -> int:
    """Sales of `purpose` for the signal the broker rejected today, at once or after taking them
    (ended short with its order id). A lost sale that never landed is not a rejection, nor is one
    HAT cancelled to send it again (`mark_resend`, PR 48)."""
    start, end = day_bounds(trade_date_of(now))
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
                    ~resend_event(),
                ),
            ),
            HatOrder.placed_at >= start,
            HatOrder.placed_at < end,
        )
    )
    return int(count or 0)


def resend_requested(session: Session, row: HatOrder) -> bool:
    """Whether HAT asked the broker to cancel this sale to send it again (`mark_resend`)."""
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.aggregate_id == row.id, HatEvent.event_type == EVENT_ORDER_CANCELLED
        )
    )
    return any((payload or {}).get("code") == RESEND for payload in payloads)


def mark_resend(
    session: Session, row: HatOrder, found: BrokerOrder, bot_id: uuid.UUID | None
) -> None:
    """Record (no commit) that HAT asked the broker to cancel the resting market sale `row` to
    send it again (PR 48): `order.cancelled` with code `sale_resend` and the order type and price
    the broker's book showed for it (under market price protection, the protected limit)."""
    record(
        session,
        aggregate_type="order",
        aggregate_id=row.id,
        event_type=EVENT_ORDER_CANCELLED,
        actor="engine",
        payload={
            "tag": row.tag,
            "purpose": row.purpose,
            "broker_order_id": row.broker_order_id,
            "code": RESEND,
            "book_order_type": None if found.order_type is None else found.order_type.value,
            "book_price": None if found.price is None else str(found.price),
            "filled_qty": found.filled_qty,
        },
        correlation_id=row.signal_id,
        bot_id=bot_id,
    )


def placed_event(row: HatOrder) -> str:
    return "gtt.placed" if row.purpose == GTT_PURPOSE else "order.placed"


def order_event(
    session: Session,
    row: HatOrder,
    bot_id: uuid.UUID | None,
    event_type: str,
    code: str = CODE_OK,
    rule: str | None = None,
    final: bool = False,
) -> None:
    """Append the order's `event_type` (no commit): through the journal when it is a lifecycle
    step of the order's signal, else as the order's own event. Never refused: it records a
    broker fact."""
    payload: dict[str, object] = {
        "tag": row.tag,
        "purpose": row.purpose,
        "broker_order_id": row.broker_order_id,
        "code": code,
    }
    if final:
        payload["final"] = True  # no retry follows: the lifecycle step to Rejected
    if rule is not None:
        payload["rule"] = rule  # the exit rule a sale is for (its trade result's exit reason)
    step = lifecycle.step_of(event_type, payload)
    signal = session.get(HatSignal, row.signal_id) if step is not None else None
    if signal is None:  # no step, or no trade to move: the event is kept, never refused
        if step is not None:
            logger.error("order %s has no signal; %s kept without a step", row.tag, event_type)
            payload[lifecycle.NO_STEP] = True
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
        return
    position = session.get(HatPosition, row.position_id) if row.position_id else None
    # An order event is a broker fact: the journal moves the signal (an entry `order.placed` to
    # EntryPlaced, a full exit order's trade to ExitPending) and keeps an event the table has no
    # row for as a non-step, so the order's bookkeeping never fails after a broker call.
    journal.record(
        session,
        signal,
        journal.on_order(
            row, event_type, payload, position=position, broker_fact=True, bot_id=bot_id
        ),
    )
