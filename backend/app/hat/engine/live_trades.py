"""Real positions against the database: broker fills and the exit stack on a real position
(HAT-EXEC-060, -073, -074).

Like `shadow`, every function works in the caller's open transaction and never commits, takes the
bot's lock first, and writes a state change together with the event that explains it, so a replay
of the events still reproduces the stored state. Nothing here reaches a broker: the fills come
from the broker's trade book through `execution/fills.py`, and the sales the exit stack asks for
are sent by `execution/manage.py`.

- An entry fill opens the real position (EntryPlaced -> Open) at the fill price with the signal's
  stop, or adds to it (a partial fill; the average entry is weighted).
- An exit fill sells from it through `positions.record_sale`, the recording the shadow engine
  uses: a partial sale (`exit.partial`), or the last share (`position.closed` and the trade
  result). While the entry order still works at the broker (a
  part-filled DAY limit), the emptied position stays open at quantity 0: the stop desk cancels
  the remainder; a late fill of it adds to the position, is protected like any entry fill and
  fires again the rules of the sale that emptied it; `close_if_emptied` closes the position once
  the broker shows the entry ended (F2). A flatten fires no rules: a late fill of a position a
  `/flatten` emptied is `flatten_owed`, and the manager sells it under that flatten (Q13). The
  first fill of a broker stop is the stop triggering:
  its `order.filled` carries `exit_triggered` and stands for the exit order placed (ExitPending),
  as does any fill that empties a position no full exit order was placed for.
- A broker fill is a broker fact (Q2): every event a real fill writes is kept even when the
  lifecycle has no step for it (`journal.Event.broker_fact`). A late entry fill on a trade that
  had already ended (its entry seen cancelled or rejected, or its position closed) opens or
  reopens the position, so the stop covers the shares; a sale fill on a closed position is stored
  with its proceeds and changes nothing else. Either is `off_lifecycle`, for the fill sync's
  URGENT `fill.off_lifecycle`.
- `resolve_sale` is the owner's booking of a lost sale of an earlier session that was settled
  from the broker's holdings without its fill (Q12): the quantity and price of the contract
  note become its fill, and an emptied position closes with a trade result that has proceeds.
- `evaluate_real_position` runs the same `positions.evaluate_stack` as the shadow engine (T12).
  It raises the stop and marks fired the rules that sell nothing; a rule that sells is marked
  fired by `mark_fired` once its order went out, so a sale the broker refused is evaluated again.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ids import uuid7
from ..models import HatEvent, HatFill, HatOrder, HatPosition, HatSignal
from ..time import as_utc, trade_date_of
from . import exits, journal, lifecycle
from .botlock import lock_bot
from .owner import OwnerCommand
from .positions import (
    STOP_PURPOSES,
    TradeError,
    close_trade,
    evaluate_stack,
    fill_payload,
    record_sale,
    sold_qty,
    trade_of,
    write_result,
)
from .snapshot import ENTRY_WORKING, HARD_STOP

__all__ = [
    "EXIT_PURPOSES",
    "Booked",
    "BrokerFill",
    "apply_entry_fill",
    "apply_exit_fill",
    "close_if_emptied",
    "entry_working",
    "flatten_owed",
    "evaluate_real_position",
    "mark_fired",
    "unmark_fired",
    "real_position_of",
    "resolve_sale",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
EXIT_PURPOSES = ("exit_partial", "exit_full", "flatten")
_PRICE_PLACES = Decimal("0.0001")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class BrokerFill:
    """One broker trade against one HAT order."""

    qty: int
    price: Decimal
    trade_id: str
    at: datetime
    fees: Decimal = _ZERO


@dataclass(frozen=True)
class Booked:
    """A broker trade recorded: the position it moved, and whether the lifecycle had already
    ended for it (a late fill booked as a broker fact)."""

    position: HatPosition
    off_lifecycle: bool = False


def real_position_of(session: Session, signal_id: uuid.UUID) -> HatPosition | None:
    """The signal's real position (a signal has at most one)."""
    return session.scalar(
        select(HatPosition).where(
            HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(False)
        )
    )


def _locked_order(session: Session, order_id: uuid.UUID) -> tuple[HatOrder, HatSignal]:
    """The real order and its signal, re-read under the bot's lock."""
    order = session.get(HatOrder, order_id)
    if order is None:
        raise TradeError("unknown_order")
    if order.is_virtual:
        raise TradeError("order_is_virtual")  # virtual fills are the shadow engine's
    signal = session.get(HatSignal, order.signal_id)
    if signal is None:
        raise TradeError("unknown_signal")
    lock_bot(session, signal.bot_id)
    session.refresh(signal)
    session.refresh(order)
    return order, signal


def _already_recorded(session: Session, order: HatOrder, fill: BrokerFill) -> bool:
    found = session.scalar(
        select(HatFill.id).where(
            HatFill.order_id == order.id, HatFill.broker_trade_id == fill.trade_id
        )
    )
    return found is not None


def _check_fill(fill: BrokerFill) -> None:
    if (
        isinstance(fill.qty, bool)
        or not isinstance(fill.qty, int)
        or fill.qty <= 0
        or not isinstance(fill.price, Decimal)
        or not fill.price.is_finite()
        or fill.price <= 0
        or not fill.trade_id
    ):
        raise TradeError("invalid_fill")


def _insert_fill(session: Session, order: HatOrder, fill: BrokerFill) -> None:
    session.add(
        HatFill(
            id=uuid7(),
            order_id=order.id,
            qty=fill.qty,
            price=fill.price,
            fees=fill.fees,
            filled_at=fill.at,
            broker_trade_id=fill.trade_id,
            is_virtual=False,
        )
    )
    session.flush()


def apply_entry_fill(session: Session, order_id: uuid.UUID, fill: BrokerFill) -> Booked | None:
    """Record one broker trade of a real entry order: the first opens the position at the fill
    price with the signal's stop (EntryPlaced -> Open), a later one adds to it. None when this
    broker trade is already recorded. A fill after the position's sales emptied it, while the
    entry still worked, adds to it the same way.

    A fill the lifecycle had already ended for is a broker fact and is booked all the same (Q2):
    with no position (the entry was seen cancelled or rejected) it opens one, on a closed
    position it reopens it, so the shares the broker holds are tracked and the stop covers them.
    The journal keeps its events as non-steps; the result is `off_lifecycle`. A fill of a signal
    whose placement is not adopted yet (still Triggered) raises TradeError: the fill sync adopts
    the order first and records the fill at its next run."""
    _check_fill(fill)
    order, signal = _locked_order(session, order_id)
    if order.purpose != ENTRY:
        raise TradeError("not_an_entry_order")
    if _already_recorded(session, order, fill):
        return None
    position = real_position_of(session, signal.id)
    if position is None and signal.state not in (lifecycle.ENTRY_PLACED, *lifecycle.ENDINGS):
        # its placement is not adopted yet (an answer lost): the next sync, after adoption
        raise TradeError("fill_without_entry")
    late = (position is None and signal.state in lifecycle.ENDINGS) or (
        position is not None and position.closed_at is not None
    )
    if late:
        logger.error("entry %s filled after its trade ended (%s)", order.tag, signal.state)
    _insert_fill(session, order, fill)
    journal.record(
        session,
        signal,
        journal.on_order(
            order,
            "order.filled",
            fill_payload(order, fill.qty, fill.price),
            now=fill.at,
            broker_fact=True,
        ),
    )
    if position is not None:
        position.closed_at = None  # a closed one reopens: the stop desk protects it again
        if position.qty_open == 0:
            _fire_again_late(session, position)
        # The average of every entry fill recorded (this one included), rounded once: weighted by
        # the shares bought, not those still held, so a sale before this fill (or one that
        # emptied the position) leaves the entry price as it was.
        bought = session.execute(
            select(HatFill.qty, HatFill.price)
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == signal.id, HatOrder.purpose == ENTRY)
        ).all()
        position.avg_entry = (
            sum((Decimal(price) * qty for qty, price in bought), _ZERO)
            / sum(qty for qty, _ in bought)
        ).quantize(_PRICE_PLACES)
        position.qty_open += fill.qty
        order.position_id = position.id
        session.flush()
        return Booked(position, late)
    stop = Decimal(signal.stop_price)
    position = HatPosition(
        id=uuid7(),
        bot_id=signal.bot_id,
        signal_id=signal.id,
        instrument_id=signal.instrument_id,
        is_virtual=False,
        qty_open=fill.qty,
        avg_entry=fill.price,
        initial_stop=stop,
        current_stop=stop,
        opened_at=fill.at,
        # a trade that had ended keeps its ending: the position is off its lifecycle
        state=lifecycle.CLOSED if late else lifecycle.OPEN,
        rules_fired=[],
    )
    session.add(position)
    session.flush()
    order.position_id = position.id
    session.flush()
    journal.record(
        session,
        signal,
        journal.on_position(
            position,
            "position.opened",
            {"qty": fill.qty, "avg_entry": str(fill.price), "stop": str(stop), "is_virtual": False},
            now=fill.at,
            broker_fact=True,
        ),
    )
    return Booked(position, late)


def _exit_rule_of(session: Session, order: HatOrder) -> str:
    """Why the sale happened: `hard_stop` for a broker stop, else the rule its placing recorded
    on `order.placed` (the purpose when the placement was adopted without one)."""
    if order.purpose in STOP_PURPOSES:
        return HARD_STOP
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.aggregate_id == order.id, HatEvent.event_type == "order.placed"
        )
    )
    for payload in payloads:
        rule = (payload or {}).get("rule")
        if isinstance(rule, str) and rule:
            return rule
    return order.purpose


def apply_exit_fill(session: Session, order_id: uuid.UUID, fill: BrokerFill) -> Booked | None:
    """Record one broker trade of a real sell (an exit order or a broker stop) against the
    position: the open quantity drops, a partial sale writes `exit.partial`, the last share
    closes the position and writes its trade result. None when this broker trade is already
    recorded. A sale larger than the open quantity is logged and closes the position (the
    broker's book is then reconciliation's). A sale on a position already closed is a broker
    fact (Q2): stored with its proceeds and an `order.filled`, `off_lifecycle`, and nothing else
    moves (the trade's result stands)."""
    _check_fill(fill)
    order, signal = _locked_order(session, order_id)
    if order.purpose not in (*EXIT_PURPOSES, *STOP_PURPOSES):
        raise TradeError("not_a_sell_order")
    if _already_recorded(session, order, fill):
        return None
    position = real_position_of(session, signal.id)
    if position is None:
        raise TradeError("no_open_position")
    session.refresh(position)
    if position.closed_at is not None:
        logger.error("sale %s filled on a closed position", order.tag)
        _insert_fill(session, order, fill)
        order.position_id = position.id
        journal.record(
            session,
            signal,
            journal.on_order(
                order,
                "order.filled",
                fill_payload(order, fill.qty, fill.price),
                now=fill.at,
                broker_fact=True,
            ),
        )
        return Booked(position, off_lifecycle=True)
    rule = _exit_rule_of(session, order)
    # read only for a sale that empties the position: it stays open while the entry works
    empties = sold_qty(position.qty_open, fill.qty) == 0
    working = empties and entry_working(session, signal.id, fill.at)
    _insert_fill(session, order, fill)
    trade = trade_of(session, position)
    record_sale(
        session, trade, order, fill.qty, fill.price, rule, at=fill.at, entry_working=working
    )
    return Booked(position)


def entry_working(session: Session, signal_id: uuid.UUID, now: datetime) -> bool:
    """The signal's real entry order may still fill at the broker: sent on `now`'s trade date
    and not seen ended. A DAY limit sent on an earlier date expired at that close, even when no
    sync read its last status."""
    sent = session.scalars(
        select(HatOrder.placed_at).where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.is_virtual.is_(False),
            HatOrder.status.in_(ENTRY_WORKING),
            HatOrder.placed_at.is_not(None),
        )
    )
    today = trade_date_of(as_utc(now))
    return any(trade_date_of(as_utc(at)) >= today for at in sent)


def _fire_again_late(session: Session, position: HatPosition) -> None:
    """A late entry fill of an emptied position: the rules of the exit sale that emptied it fire
    again, so the next evaluation sells these shares too (a broker stop protects them now)."""
    rule_ids = session.scalars(
        select(HatOrder.rule_ids)
        .join(HatFill, HatFill.order_id == HatOrder.id)
        .where(HatOrder.position_id == position.id, HatOrder.purpose.in_(EXIT_PURPOSES))
        .order_by(HatFill.filled_at.desc(), HatFill.id.desc())
        .limit(1)
    ).first()
    if rule_ids:
        gone = set(rule_ids)
        position.rules_fired = [rid for rid in (position.rules_fired or []) if rid not in gone]


def flatten_owed(session: Session, position: HatPosition) -> bool:
    """The real position holds shares an entry fill brought in after its last sale, and that sale
    was a flatten (Q13): the shares are owed the flatten's follow-up sale. Read from the stored
    fills, so a restart keeps it."""
    if position.is_virtual or position.closed_at is not None or position.qty_open <= 0:
        return False
    last_sale = session.execute(
        select(HatOrder.purpose, HatFill.filled_at, HatFill.id)
        .join(HatFill, HatFill.order_id == HatOrder.id)
        .where(
            HatOrder.position_id == position.id,
            HatOrder.purpose.in_((*EXIT_PURPOSES, *STOP_PURPOSES)),
        )
        .order_by(HatFill.filled_at.desc(), HatFill.id.desc())
        .limit(1)
    ).first()
    if last_sale is None or last_sale.purpose != "flatten":
        return False
    later_entry = session.scalar(
        select(HatFill.id)
        .join(HatOrder, HatOrder.id == HatFill.order_id)
        .where(
            HatOrder.signal_id == position.signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.is_virtual.is_(False),
            HatFill.filled_at >= last_sale.filled_at,
        )
        .limit(1)
    )
    return later_entry is not None


def close_if_emptied(session: Session, position_id: uuid.UUID, now: datetime) -> HatPosition | None:
    """Close a real position its sales emptied while its entry still worked, once the entry has
    ended at the broker (or its trade date passed): `position.closed` and the trade result at
    its last sale's price, rule and time. None when the position is not such a one (shares
    open, the entry still working, already closed, or never sold)."""
    position = _locked_real_position(session, position_id)
    signal = session.get(HatSignal, position.signal_id)
    if signal is None or position.closed_at is not None or position.qty_open > 0:
        return None
    if entry_working(session, signal.id, now):
        return None
    last = session.execute(
        select(HatOrder, HatFill)
        .join(HatFill, HatFill.order_id == HatOrder.id)
        .where(
            HatOrder.position_id == position.id,
            HatOrder.purpose.in_((*EXIT_PURPOSES, *STOP_PURPOSES)),
        )
        .order_by(HatFill.filled_at.desc(), HatFill.id.desc())
        .limit(1)
    ).first()
    if last is None:
        return None
    order, fill = last
    rule = _exit_rule_of(session, order)
    # a real trade: MFE and MAE are NULL
    close_trade(
        session, trade_of(session, position), rule, Decimal(fill.price), at=as_utc(fill.filled_at)
    )
    return position


def resolve_sale(
    session: Session,
    tag: str,
    sale: BrokerFill,
    note: str,
    command: OwnerCommand,
) -> HatPosition:
    """The owner books the fill of a lost sale of an earlier session (Q12), from the contract note:
    `sale` is its quantity, price and trade time, `note` the contract note's number (the fill's
    trade id, `contract:<note>`). A sale settled from the broker's holdings keeps the open quantity
    the settle corrected; one still unsure (no holding baseline was known, so nothing was corrected)
    lowers it by the sale, and the fill sync resizes the stop; a partial exit's sale that leaves
    shares open moves the trade to PartiallyExited (`exit.partial`), as a fill from the broker does;
    with a quantity of 0 (the contract note shows no fill) it ends `expired` instead and its rules
    fire again (`order.expired`), so the exit can sell. A position left at no shares closes at this
    price (`position.closed` and the trade result, with the sale's proceeds), and one already closed
    gets its result rewritten. Recorded as the owner's (`actor = user`, the allowlisted chat, `via =
    cli`). Outside market hours, under the bot's lock. Refused, writing nothing: inside a session
    (`market_hours`), a tag that is no real sale (`sale_unknown`), a sale neither settled from the
    holdings nor lost in an earlier session (`sale_not_settled`: its fills come from the broker's
    trade book), a quantity beyond the sale's with what is already booked (`qty_exceeds_sale`), a
    time outside the sale's send and now (`time_outside_sale`), and an invalid fill, negative fees
    or empty note (`invalid_fill`)."""
    if command.in_session:
        raise TradeError("market_hours")
    if sale.qty != 0:
        _check_fill(sale)
    fees_ok = isinstance(sale.fees, Decimal) and sale.fees.is_finite() and sale.fees >= 0
    if not note.strip() or not fees_ok:
        raise TradeError("invalid_fill")
    order_id = session.scalar(select(HatOrder.id).where(HatOrder.tag == tag))
    if order_id is None:
        raise TradeError("sale_unknown")
    order, signal = _locked_order(session, order_id)
    if order.purpose not in EXIT_PURPOSES or order.position_id is None:
        raise TradeError("sale_unknown")
    if order.broker_order_id is not None or order.placed_at is None:
        raise TradeError("sale_not_settled")
    # still unsure: the settle found no holding baseline, so the open quantity was not corrected
    unsure = order.status in ("pending", "unconfirmed")
    earlier = trade_date_of(as_utc(order.placed_at)) < command.trade_date
    if not (order.status == "filled" or (unsure and earlier)):
        raise TradeError("sale_not_settled")
    if sale.qty == 0 and not unsure:
        raise TradeError("invalid_fill")
    booked = session.scalar(select(func.sum(HatFill.qty)).where(HatFill.order_id == order.id))
    if int(booked or 0) + sale.qty > order.qty:
        raise TradeError("qty_exceeds_sale")
    at = as_utc(sale.at)
    if at < as_utc(order.placed_at) or at > command.now:
        raise TradeError("time_outside_sale")
    position = session.get(HatPosition, order.position_id)
    if position is None:
        raise TradeError("sale_unknown")
    session.refresh(position)
    if sale.qty == 0:
        order.status = "expired"
        if order.rule_ids:
            unmark_fired(session, position.id, order.rule_ids)
        payload = {"chat_id": command.chat_id, "via": "cli", "contract_note": note.strip()}
        journal.record(
            session, signal, journal.on_order(order, "order.expired", payload, now=at, actor="user")
        )
        session.flush()
        return position
    if unsure:
        order.status = "filled"
        if position.closed_at is None:
            position.qty_open = sold_qty(position.qty_open, sale.qty)  # the stop follows it
    fill = BrokerFill(sale.qty, sale.price, f"contract:{note.strip()}", at, sale.fees)
    _insert_fill(session, order, fill)
    payload = {**fill_payload(order, fill.qty, fill.price), "chat_id": command.chat_id}
    payload |= {"via": "cli", "contract_note": note.strip()}
    closes = position.closed_at is None and position.qty_open == 0
    if closes and signal.state in (lifecycle.OPEN, lifecycle.PARTIALLY_EXITED):
        payload[lifecycle.EXIT_TRIGGERED] = True  # stands for the exit order placed
    open_trade = position.closed_at is None and signal.state in (
        lifecycle.OPEN,
        lifecycle.PARTIALLY_EXITED,
    )
    journal.record(
        session,
        signal,
        journal.on_order(
            order,
            "order.filled",
            payload,
            now=at,
            position=position,
            actor="user",
            broker_fact=True,
        ),
    )
    rule = _exit_rule_of(session, order)
    if open_trade and not closes and order.purpose == "exit_partial":
        partial = {"qty": fill.qty, "price": str(fill.price), "rule": rule, "via": "cli"}
        journal.record(
            session,
            signal,
            journal.on_position(
                position, "exit.partial", partial, now=at, actor="user", broker_fact=True
            ),
        )
    if position.closed_at is not None:
        write_result(session, signal, position, rule, as_utc(position.closed_at))
    elif closes:
        close_trade(session, trade_of(session, position), rule, fill.price, at=at)
    session.flush()
    return position


def _locked_real_position(session: Session, position_id: uuid.UUID) -> HatPosition:
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    lock_bot(session, position.bot_id)
    session.refresh(position)
    if position.is_virtual:
        raise TradeError("position_is_virtual")  # the shadow engine's: shadow.evaluate_position
    return position


def evaluate_real_position(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
) -> exits.Evaluation:
    """Run the pinned exit stack on one real position through `positions.evaluate_stack`, the
    evaluation `shadow.evaluate_position` runs on a virtual one (T12): raise the stop
    (`stop.moved`; the broker stop follows it) and mark fired the rules that sell nothing. The
    sales are returned for the caller to send; their rules are marked by `mark_fired` once the
    orders went out."""
    position = _locked_real_position(session, position_id)
    result = evaluate_stack(session, position, view, phase, now)
    session.flush()
    return result


def mark_fired(session: Session, position_id: uuid.UUID, rule_ids: Iterable[str]) -> None:
    """Mark the rules of a sale whose order went out as fired (fire once, HAT-EXEC-071)."""
    position = _locked_real_position(session, position_id)
    position.rules_fired = sorted({*(position.rules_fired or []), *rule_ids})
    session.flush()


def unmark_fired(session: Session, position_id: uuid.UUID, rule_ids: Iterable[str]) -> None:
    """Fire again the rules of a sale that ended without selling (the broker rejected it after
    taking it, or its answer was lost and it never landed): the next evaluation sells again."""
    position = _locked_real_position(session, position_id)
    gone = set(rule_ids)
    position.rules_fired = [rid for rid in (position.rules_fired or []) if rid not in gone]
    session.flush()
