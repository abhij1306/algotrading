"""Real positions against the database: broker fills, the exit stack on a real position and the
15:25 close-gap record (07 §4, §6, §7; HAT-EXEC-060, -073, -074, HAT-OPS-034; PR 15c).

Like `trades`, every function works in the caller's open transaction and never commits, takes the
bot's lock first, and writes a state change together with the event that explains it, so a replay
of the events still reproduces the stored state. Nothing here reaches a broker: the fills come
from the broker's trade book through `execution/fills.py`, and the sales the exit stack asks for
are sent by `execution/manage.py`.

- An entry fill opens the real position (EntryPlaced -> Open) at the fill price with the signal's
  stop, or adds to it (a partial fill; the average entry is weighted).
- An exit fill sells from it: a partial sale (`exit.partial`), or the last share
  (`position.closed` and the trade result). While the entry order still works at the broker (a
  part-filled DAY limit), the emptied position stays open at quantity 0: the stop desk cancels
  the remainder; a late fill of it adds to the position, is protected like any entry fill and
  fires again the rules of the sale that emptied it; `close_if_emptied` closes the position once
  the broker shows the entry ended (F2). The first fill of a broker stop is the stop triggering:
  its `order.filled` carries `exit_triggered` and stands for the exit order placed (ExitPending),
  as does any fill that empties a position no full exit order was placed for.
- `evaluate_real_position` runs the same `exits.evaluate` as the shadow engine (T12). It raises
  the stop and marks fired the rules that sell nothing; a rule that sells is marked fired by
  `mark_fired` once its order went out, so a sale the broker refused is evaluated again.
- `record_close_evaluation` keeps the live price of every 15:20-15:25 `close_below_sma`
  evaluation, and `record_close_gaps` adds the official close once the nightly ingest has it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ids import uuid7
from ..models import HatEvent, HatFill, HatOrder, HatPosition, HatSignal
from ..time import as_utc, day_bounds, trade_date_of
from . import exits, journal, lifecycle
from .botlock import lock_bot
from .positions import (
    TradeError,
    exit_rules_of,
    is_veto,
    position_state,
    raise_stop,
    signal_and_bot,
    write_result,
)
from .snapshot import HARD_STOP

__all__ = [
    "CLOSE_EVALUATED",
    "CLOSE_GAP",
    "EXIT_PURPOSES",
    "STOP_PURPOSES",
    "BrokerFill",
    "apply_entry_fill",
    "apply_exit_fill",
    "close_if_emptied",
    "entry_working",
    "evaluate_real_position",
    "mark_fired",
    "unmark_fired",
    "real_position_of",
    "record_close_evaluation",
    "record_close_gaps",
]

logger = logging.getLogger(__name__)

ENTRY = "entry"
STOP_PURPOSES = ("stop_day", "stop_gtt")
EXIT_PURPOSES = ("exit_partial", "exit_full", "flatten")
CLOSE_EVALUATED = "exit.close_evaluated"
CLOSE_GAP = "exit.close_gap"
CLOSE_RULE = "close_below_sma"
_OPEN_STATES = (lifecycle.OPEN, lifecycle.PARTIALLY_EXITED)
# An entry order the broker may still fill (`protect.WORKING`; the engine does not import it).
_ENTRY_WORKING = ("pending", "unconfirmed", "placed", "partially_filled")
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
            currency="INR",
            filled_at=fill.at,
            broker_trade_id=fill.trade_id,
            is_virtual=False,
        )
    )
    session.flush()


def _fill_payload(order: HatOrder, fill: BrokerFill) -> dict[str, object]:
    return {
        "qty": fill.qty,
        "price": str(fill.price),
        "purpose": order.purpose,
        "tag": order.tag,
        "is_virtual": False,
    }


def apply_entry_fill(session: Session, order_id: uuid.UUID, fill: BrokerFill) -> HatPosition | None:
    """Record one broker trade of a real entry order: the first opens the position at the fill
    price with the signal's stop (EntryPlaced -> Open), a later one adds to it. None when this
    broker trade is already recorded. A fill after the position's sales emptied it, while the
    entry still worked, adds to it the same way. Raises TradeError for a fill the lifecycle
    cannot take (the signal never reached EntryPlaced, or its position already closed); the
    caller rolls back and reconciliation reports the broker's position."""
    _check_fill(fill)
    order, signal = _locked_order(session, order_id)
    if order.purpose != ENTRY:
        raise TradeError("not_an_entry_order")
    if _already_recorded(session, order, fill):
        return None
    position = real_position_of(session, signal.id)
    if position is None and signal.state != lifecycle.ENTRY_PLACED:
        raise TradeError("fill_without_entry")
    if position is not None and position.closed_at is not None:
        raise TradeError("position_closed")
    _insert_fill(session, order, fill)
    journal.record(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload=_fill_payload(order, fill),
        now=fill.at,
    )
    if position is not None:
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
        return position
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
        currency="INR",
        opened_at=fill.at,
        state=lifecycle.OPEN,
        rules_fired=[],
    )
    session.add(position)
    session.flush()
    order.position_id = position.id
    session.flush()
    journal.record(
        session,
        signal,
        "position.opened",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={
            "qty": fill.qty,
            "avg_entry": str(fill.price),
            "stop": str(stop),
            "is_virtual": False,
        },
        now=fill.at,
        position=position,
    )
    return position


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


def apply_exit_fill(session: Session, order_id: uuid.UUID, fill: BrokerFill) -> HatPosition | None:
    """Record one broker trade of a real sell (an exit order or a broker stop) against the
    position: the open quantity drops, a partial sale writes `exit.partial`, the last share
    closes the position and writes its trade result. None when this broker trade is already
    recorded. A sale larger than the open quantity is logged and closes the position (the
    broker's book is then reconciliation's)."""
    _check_fill(fill)
    order, signal = _locked_order(session, order_id)
    if order.purpose not in (*EXIT_PURPOSES, *STOP_PURPOSES):
        raise TradeError("not_a_sell_order")
    if _already_recorded(session, order, fill):
        return None
    position = real_position_of(session, signal.id)
    if position is None or position.closed_at is not None:
        raise TradeError("no_open_position")
    session.refresh(position)
    if fill.qty > position.qty_open:
        logger.error("sale of %s shares exceeds the open quantity of a position", fill.qty)
    remaining = max(0, position.qty_open - fill.qty)
    _insert_fill(session, order, fill)
    order.position_id = position.id
    payload = _fill_payload(order, fill)
    if signal.state in _OPEN_STATES and (order.purpose in STOP_PURPOSES or remaining == 0):
        # The stop triggered (or the position emptied without a full exit order): that is the
        # exit order placed of 07 §4.
        payload[lifecycle.EXIT_TRIGGERED] = True
    journal.record(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload=payload,
        now=fill.at,
        position=position,
    )
    position.qty_open = remaining
    rule = _exit_rule_of(session, order)
    if remaining > 0 or entry_working(session, signal.id, fill.at):
        if order.purpose == "exit_partial" and signal.state in _OPEN_STATES:
            journal.record(
                session,
                signal,
                "exit.partial",
                aggregate_type="position",
                aggregate_id=position.id,
                payload={"qty": fill.qty, "price": str(fill.price), "rule": rule},
                now=fill.at,
                position=position,
            )
        session.flush()
        return position
    _close(session, signal, position, rule, fill.price, fill.at)
    return position


def _close(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    rule: str,
    price: Decimal,
    at: datetime,
) -> None:
    position.closed_at = at
    journal.record(
        session,
        signal,
        "position.closed",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"price": str(price), "rule": rule},
        now=at,
        position=position,
    )
    write_result(session, signal, position, rule, at, high=None, low=None)
    session.flush()


def entry_working(session: Session, signal_id: uuid.UUID, now: datetime) -> bool:
    """The signal's real entry order may still fill at the broker: sent on `now`'s trade date
    and not seen ended. A DAY limit sent on an earlier date expired at that close, even when no
    sync read its last status."""
    sent = session.scalars(
        select(HatOrder.placed_at).where(
            HatOrder.signal_id == signal_id,
            HatOrder.purpose == ENTRY,
            HatOrder.is_virtual.is_(False),
            HatOrder.status.in_(_ENTRY_WORKING),
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
    _close(session, signal, position, rule, Decimal(fill.price), as_utc(fill.filled_at))
    return position


def _locked_real_position(session: Session, position_id: uuid.UUID) -> HatPosition:
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    lock_bot(session, position.bot_id)
    session.refresh(position)
    if position.is_virtual:
        raise TradeError("position_is_virtual")  # the shadow engine's: trades.evaluate_position
    return position


def evaluate_real_position(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
) -> exits.Evaluation:
    """Run the pinned exit stack on one real position, exactly as `trades.evaluate_position` does
    on a virtual one (T12): raise the stop (`stop.moved`; the broker stop follows it) and mark
    fired the rules that sell nothing. The sales are returned for the caller to send; their
    rules are marked by `mark_fired` once the orders went out."""
    position = _locked_real_position(session, position_id)
    if position.closed_at is not None or position.qty_open <= 0:
        return exits.Evaluation()
    signal, _ = signal_and_bot(session, position)
    rules, state = position_state(session, position)
    result = exits.evaluate(rules, state, view, phase)
    if result.actions and view.price is None:
        raise TradeError("exit_without_price")
    if result.new_stop is not None and result.new_stop > state.current_stop:
        raise_stop(session, signal, position, state.current_stop, result.new_stop, now)
    for rule_id, reason in result.ignored:
        logger.info("exit rule %s ignored: %s", rule_id, reason)  # never applied (HAT-RISK-070)
    selling = {rid for action in result.actions for rid in action.rule_ids}
    instant = [rid for rid in result.fired if rid not in selling]
    position.rules_fired = sorted({*(position.rules_fired or []), *instant})
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


# --- the 15:25 close vs the official close (HAT-OPS-034) ---------------------------------------


def record_close_evaluation(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    result: exits.Evaluation,
    trade_date: date,
    now: datetime,
) -> bool:
    """Record the live price a 15:20-15:25 evaluation used for the position's `close_below_sma`
    rules (`exit.close_evaluated`), whether or not one fired. False, writing nothing, when the
    position has no enabled `close_below_sma` rule, the view has no price, or the trade date's
    evaluation is already recorded (a retried job)."""
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    signal, _ = signal_and_bot(session, position)
    rules = exit_rules_of(session, signal)
    close_rules = [
        (exits.rule_id(index, rule), rule)
        for index, rule in enumerate(rules)
        if rule.enabled and rule.rule == CLOSE_RULE
    ]
    if not close_rules or view.price is None or _close_recorded(session, position.id, trade_date):
        return False
    ids = [rid for rid, _ in close_rules]
    levels = {
        str(rule.sma): str(view.sma[rule.sma])  # type: ignore[union-attr]
        for _, rule in close_rules
        if rule.sma in view.sma  # type: ignore[union-attr]
    }
    journal.record(
        session,
        signal,
        CLOSE_EVALUATED,
        aggregate_type="position",
        aggregate_id=position.id,
        payload={
            "trade_date": trade_date.isoformat(),
            "live_price": str(view.price),
            "rules": ids,
            "sma": levels,
            "fired": [rid for rid in result.fired if rid in ids],
            "is_virtual": bool(position.is_virtual),
        },
        now=now,
        veto=is_veto(signal, position),
    )
    return True


def _close_recorded(session: Session, position_id: uuid.UUID, trade_date: date) -> bool:
    payloads = session.scalars(
        select(HatEvent.payload).where(
            HatEvent.event_type == CLOSE_EVALUATED, HatEvent.aggregate_id == position_id
        )
    )
    day = trade_date.isoformat()
    return any((payload or {}).get("trade_date") == day for payload in payloads)


def _gap_pct(gap: Decimal, live: Decimal) -> Decimal:
    return (gap * 100 / live).quantize(_PRICE_PLACES)


def record_close_gaps(
    session: Session, trade_date: date, closes: Mapping[uuid.UUID, Decimal], now: datetime
) -> int:
    """For every `exit.close_evaluated` of `trade_date` that has no gap yet, record the official
    close of the position's instrument (`closes`, from the nightly ingest) and its gap to the
    live price used (`exit.close_gap`, against the same position). An instrument without a close
    is left for a later run. Returns the number of gaps recorded."""
    start, end = day_bounds(trade_date)
    rows = session.execute(
        select(HatEvent.aggregate_id, HatEvent.event_type, HatEvent.payload).where(
            HatEvent.event_type.in_((CLOSE_EVALUATED, CLOSE_GAP)),
            HatEvent.occurred_at >= start,
            HatEvent.occurred_at < end + (end - start),  # a gap is recorded the night after
        )
    ).all()
    day = trade_date.isoformat()
    done = {
        aggregate
        for aggregate, kind, payload in rows
        if kind == CLOSE_GAP and (payload or {}).get("trade_date") == day
    }
    recorded = 0
    for aggregate, kind, payload in rows:
        data = payload or {}
        if kind != CLOSE_EVALUATED or data.get("trade_date") != day or aggregate in done:
            continue
        position = session.get(HatPosition, aggregate)
        if position is None:
            continue
        official = closes.get(position.instrument_id)
        if official is None or not official.is_finite() or official <= 0:
            continue
        signal, _ = signal_and_bot(session, position)
        live = Decimal(str(data["live_price"]))
        gap = official - live
        sma = {int(k): Decimal(str(v)) for k, v in (data.get("sma") or {}).items()}
        journal.record(
            session,
            signal,
            CLOSE_GAP,
            aggregate_type="position",
            aggregate_id=position.id,
            payload={
                "trade_date": day,
                "live_price": str(live),
                "official_close": str(official),
                "gap": str(gap),
                "gap_pct": str(_gap_pct(gap, live)),
                "below_sma_live": sorted(str(n) for n, level in sma.items() if live < level),
                "below_sma_close": sorted(str(n) for n, level in sma.items() if official < level),
            },
            now=now,
            veto=is_veto(signal, position),
        )
        done.add(aggregate)
        recorded += 1
    return recorded
