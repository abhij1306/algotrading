"""The trade lifecycle against the database: per-bot serialisation, entries, the virtual (shadow)
fill engine, exits and the trade result (07 §4, HAT-EXEC-040..044, HAT-SCH-057, HAT-BOT-083).

Every function works in the caller's open transaction and never commits. A state change and the
event that explains it are written together, so a replay of the events always reproduces the
stored state (`replay_state`).

**One locked transaction per bot.** `evaluate_signal` is pure over the snapshot it is given, so
two evaluations that read the same snapshot could both take the last position slot. `open_entry`
therefore takes the bot's lock first, reads the snapshot, decides and inserts under it; a second
entry for the same bot waits for the first to commit and then sees its position. On Postgres the
lock is a transaction-scoped advisory lock keyed by the bot; SQLite has one writer at a time and
needs none.

**Shadow only (D20).** Entries and exits here are virtual: the rows carry `is_virtual = true`,
nothing in this module reaches a broker, and it imports none (CONF `import_graph_boundaries`).
Confirm and auto go through the execution router (PR 15), which calls the same `exits.evaluate`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..events import record
from ..execution.tags import encode_tag
from ..ids import uuid7
from ..models import (
    HatBot,
    HatEvent,
    HatFill,
    HatIndustry,
    HatOrder,
    HatPosition,
    HatSignal,
    HatStrategyVersion,
    HatTradeResult,
    HatWatchlistEntry,
)
from ..risk import controls
from ..risk.caps import OpenPosition
from ..risk.decision import REASON_CODES, RiskDecision, SignalContext
from ..risk.policy import PolicyVersion, effective_version
from ..risk.policy_service import load_versions
from ..risk.service import evaluate_signal
from ..time import IST, as_utc, trade_date_of
from . import exits, lifecycle
from .botlock import lock_bot
from .strategy.config import ExitRule, parse_config
from .watchlist import bot_universe_members

__all__ = [
    "EntryOutcome",
    "EntryRequest",
    "TradeError",
    "bot_snapshot",
    "evaluate_position",
    "expire_watchlist",
    "lock_bot",
    "open_entry",
    "replay_state",
    "risk_inputs",
    "trade_r",
]

logger = logging.getLogger(__name__)
SHADOW = "shadow"
DECIDE = Callable[[SignalContext], RiskDecision]
_SLOT_PENDING_STATES = (lifecycle.AWAITING_APPROVAL, lifecycle.ENTRY_PLACED)
_ENTERED_STATES = (
    lifecycle.AWAITING_APPROVAL,
    lifecycle.ENTRY_PLACED,
    lifecycle.OPEN,
    lifecycle.PARTIALLY_EXITED,
    lifecycle.EXIT_PENDING,
    lifecycle.CLOSED,
)
_POSITION_STATES = (
    lifecycle.OPEN,
    lifecycle.PARTIALLY_EXITED,
    lifecycle.EXIT_PENDING,
    lifecycle.CLOSED,
)
HARD_STOP = "hard_stop"
_ZERO = Decimal(0)
_RATIO_PLACES = Decimal("0.000001")


class TradeError(HatError):
    """A lifecycle operation was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the snapshot the caps read ---------------------------------------------------------------


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=IST)
    return as_utc(start), as_utc(start + timedelta(days=1))


def _industry_of(session: Session, instrument_id: uuid.UUID, day: date) -> str:
    row = session.scalar(
        select(HatIndustry.industry)
        .where(HatIndustry.instrument_id == instrument_id, HatIndustry.effective_from <= day)
        .order_by(HatIndustry.effective_from.desc())
        .limit(1)
    )
    return row or "unknown"


def bot_snapshot(
    session: Session,
    bot_id: uuid.UUID,
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None = None,
) -> tuple[list[OpenPosition], int, int]:
    """(slots, entries today, stop-outs today) for the bot, read now.

    A slot is an open position, or a signal whose entry is placed or awaiting approval and has
    no position yet. A position with no price in `last_prices` is valued at its entry."""
    prices = last_prices or {}
    slots: list[OpenPosition] = []
    for position in session.scalars(
        select(HatPosition).where(HatPosition.bot_id == bot_id, HatPosition.closed_at.is_(None))
    ):
        entry = Decimal(position.avg_entry)
        slots.append(
            OpenPosition(
                bot_id=bot_id,
                instrument_id=position.instrument_id,
                industry=_industry_of(session, position.instrument_id, trade_date),
                qty=position.qty_open,
                avg_entry=entry,
                last_price=prices.get(position.instrument_id, entry),
                current_stop=Decimal(position.current_stop),
                is_virtual=position.is_virtual,
            )
        )
    for signal in session.scalars(
        select(HatSignal).where(
            HatSignal.bot_id == bot_id, HatSignal.state.in_(_SLOT_PENDING_STATES)
        )
    ):
        entry = Decimal(signal.trigger_price)
        slots.append(
            OpenPosition(
                bot_id=bot_id,
                instrument_id=signal.instrument_id,
                industry=_industry_of(session, signal.instrument_id, trade_date),
                qty=signal.qty,
                avg_entry=entry,
                last_price=prices.get(signal.instrument_id, entry),
                current_stop=Decimal(signal.stop_price),
                is_virtual=signal.mode == SHADOW,
            )
        )
    entries = session.scalar(
        select(func.count())
        .select_from(HatSignal)
        .where(
            HatSignal.bot_id == bot_id,
            HatSignal.trade_date == trade_date,
            HatSignal.state.in_(_ENTERED_STATES),
        )
    )
    start, end = _day_bounds(trade_date)
    stopouts = session.scalar(
        select(func.count())
        .select_from(HatTradeResult)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.closed_at >= start,
            HatPosition.closed_at < end,
            HatTradeResult.exit_reason == HARD_STOP,
        )
    )
    return slots, int(entries or 0), int(stopouts or 0)


def risk_inputs(session: Session, bot_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, object]:
    """The fields of a `SignalContext` that the database holds: the kill state, the exposure
    ladder and the risk-policy versions."""
    return {
        "kill": controls.kill_state(session, bot_id),
        "ladder": controls.ladder_state(session, bot_id),
        "policy_versions": load_versions(session, user_id),
    }


# --- events and steps -------------------------------------------------------------------------


def _emit(
    session: Session,
    signal: HatSignal,
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    payload: dict[str, object],
    now: datetime,
    position: HatPosition | None = None,
) -> None:
    """Record `event_type` and, when it drives a lifecycle step, move the signal (and its
    position) to the next state in the same transaction (HAT-EXEC-042)."""
    step = lifecycle.step_of(event_type, payload)
    if step is not None:
        after = lifecycle.next_state(signal.state, step)
        signal.state = after
        if position is not None and after in _POSITION_STATES:
            position.state = after
    record(
        session,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor="engine",
        payload=payload,
        correlation_id=signal.id,
        bot_id=signal.bot_id,
        occurred_at=now,
    )


def replay_state(session: Session, signal_id: uuid.UUID) -> str:
    """The state the signal's events give when folded over the transition table."""
    rows = session.execute(
        select(HatEvent.event_type, HatEvent.payload)
        .where(HatEvent.correlation_id == signal_id)
        .order_by(HatEvent.id)
    ).all()
    steps = [s for event_type, payload in rows if (s := lifecycle.step_of(event_type, payload))]
    return lifecycle.fold(steps)


def _next_tag(session: Session, signal_id: uuid.UUID, purpose: str) -> str:
    count = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(HatOrder.signal_id == signal_id, HatOrder.purpose == purpose)
    )
    return encode_tag(signal_id, purpose, int(count or 0))


# --- entries ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryRequest:
    """One trigger. `context` holds the market-side inputs (prices, equity, cash, ADV, the
    calendar, gate, feed and quality flags, kill, ladder and policy versions); `open_entry`
    fills in the bot, the instrument, the signal id and the database snapshot itself."""

    bot_id: uuid.UUID
    playbook: str
    instrument_id: uuid.UUID
    trade_date: date
    triggered_at: datetime
    context: SignalContext
    last_prices: dict[uuid.UUID, Decimal] | None = None


@dataclass(frozen=True)
class EntryOutcome:
    signal_id: uuid.UUID
    state: str
    decision: RiskDecision | None
    position_id: uuid.UUID | None
    duplicate: bool = False


def _decision_row(decision: RiskDecision) -> dict[str, object]:
    document = decision.to_json()
    if not decision.accepted and decision.reason not in REASON_CODES:
        document["reason"] = "invalid_input"
    return document


def _existing_signal(session: Session, request: EntryRequest) -> HatSignal | None:
    return session.scalar(
        select(HatSignal).where(
            HatSignal.bot_id == request.bot_id,
            HatSignal.playbook == request.playbook,
            HatSignal.instrument_id == request.instrument_id,
            HatSignal.trade_date == request.trade_date,
        )
    )


def _entry_policy(session: Session, bot: HatBot, request: EntryRequest) -> PolicyVersion:
    """The policy version a new entry is decided under, after the rules that refuse it outright."""
    if bot.execution_mode != SHADOW:
        raise TradeError("mode_not_shadow")  # confirm and auto are the router's (PR 15, D20)
    if request.instrument_id not in bot_universe_members(session, bot, request.trade_date):
        # Leaving the universe stops new entries (HAT-PORT-023); an open position is untouched.
        raise TradeError("not_in_universe")
    if bot.live_version_id is None:
        raise TradeError("no_live_version")
    versions: Sequence[PolicyVersion] = request.context.policy_versions or []
    policy = effective_version(versions, request.triggered_at)
    if policy is None:
        raise TradeError("no_policy_version")
    return policy


def open_entry(
    session: Session, request: EntryRequest, *, decide: DECIDE = evaluate_signal
) -> EntryOutcome:
    """Evaluate one trigger and, when accepted, open the virtual position, in one transaction
    holding the bot's lock (HAT-EXEC-021, HAT-RISK-100).

    A repeated trigger for the same (bot, playbook, instrument, day) returns the existing
    signal and writes nothing. A rejection is stored as a signal in state `Rejected` with its
    reason (shown in the daily digest only, HAT-EXEC-043)."""
    lock_bot(session, request.bot_id)
    bot = session.get(HatBot, request.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    existing = _existing_signal(session, request)
    if existing is not None:
        # Checked before the entry rules: a retry after a mode or universe change still gets
        # the signal it already made.
        return EntryOutcome(existing.id, existing.state, None, None, duplicate=True)
    policy = _entry_policy(session, bot, request)
    base = request.context
    slots, entries, stopouts = bot_snapshot(
        session, request.bot_id, request.trade_date, request.last_prices
    )
    signal_id = uuid7()
    ctx = replace(
        base,
        signal_id=signal_id,
        bot_id=request.bot_id,
        instrument_id=request.instrument_id,
        at=request.triggered_at,
        is_virtual=True,
        open_positions=slots,
        entries_today=entries,
        stopouts_today=stopouts,
    )
    decision = decide(ctx)
    accepted = bool(decision.accepted and decision.quantity and decision.stop is not None)
    entry = base.entry_price if base.entry_price is not None else _ZERO
    signal = HatSignal(
        id=signal_id,
        bot_id=request.bot_id,
        strategy_version_id=bot.live_version_id,
        risk_policy_version_id=decision.policy_version_id or policy.version_id,
        playbook=request.playbook,
        instrument_id=request.instrument_id,
        trade_date=request.trade_date,
        triggered_at=request.triggered_at,
        trigger_price=entry,
        stop_price=decision.stop if decision.stop is not None else (base.stop_price or _ZERO),
        qty=decision.quantity or 0 if accepted else 0,
        risk_amount=decision.risk_amount or _ZERO if accepted else _ZERO,
        currency="INR",
        mode=SHADOW,
        state=lifecycle.INITIAL_STATE,
        risk_decision=_decision_row(decision),
    )
    session.add(signal)
    session.flush()
    now = request.triggered_at
    _emit(
        session,
        signal,
        "signal.triggered",
        aggregate_type="signal",
        aggregate_id=signal.id,
        payload={"playbook": request.playbook, "trigger_price": str(entry), "mode": SHADOW},
        now=now,
    )
    if not accepted:
        _emit(
            session,
            signal,
            "signal.rejected",
            aggregate_type="signal",
            aggregate_id=signal.id,
            payload={"reason": signal.risk_decision["reason"]},
            now=now,
        )
        return EntryOutcome(signal.id, signal.state, decision, None)
    position = _virtual_entry(session, signal, bot, decision, entry, now)
    return EntryOutcome(signal.id, signal.state, decision, position.id)


def _virtual_order(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    position: HatPosition | None,
    *,
    purpose: str,
    side: str,
    qty: int,
    price: Decimal,
    now: datetime,
) -> tuple[HatOrder, HatFill]:
    """A virtual order and its fill at `price`: no broker id, no broker trade id, no fees."""
    order = HatOrder(
        id=uuid7(),
        signal_id=signal.id,
        position_id=None if position is None else position.id,
        broker_account_id=bot.broker_account_id,
        purpose=purpose,
        side=side,
        qty=qty,
        order_type="market",
        tag=_next_tag(session, signal.id, purpose),
        broker_order_id=None,
        status="filled",
        is_virtual=True,
        placed_at=now,
        currency="INR",
    )
    session.add(order)
    session.flush()
    fill = HatFill(
        id=uuid7(),
        order_id=order.id,
        qty=qty,
        price=price,
        fees=_ZERO,
        currency="INR",
        filled_at=now,
        broker_trade_id=None,
        is_virtual=True,
    )
    session.add(fill)
    session.flush()
    return order, fill


def _virtual_entry(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    decision: RiskDecision,
    entry: Decimal,
    now: datetime,
) -> HatPosition:
    qty = int(decision.quantity or 0)
    stop = decision.stop or _ZERO
    order, _ = _virtual_order(
        session, signal, bot, None, purpose="entry", side="buy", qty=qty, price=entry, now=now
    )
    _emit(
        session,
        signal,
        "order.placed",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"purpose": "entry", "qty": qty, "is_virtual": True, "tag": order.tag},
        now=now,
    )
    _emit(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"qty": qty, "price": str(entry), "is_virtual": True},
        now=now,
    )
    position = HatPosition(
        id=uuid7(),
        bot_id=bot.id,
        signal_id=signal.id,
        instrument_id=signal.instrument_id,
        is_virtual=True,
        qty_open=qty,
        avg_entry=entry,
        initial_stop=stop,
        current_stop=stop,
        currency="INR",
        opened_at=now,
        state=lifecycle.OPEN,
        rules_fired=[],
    )
    session.add(position)
    session.flush()  # the position row first: the order's foreign key points at it
    order.position_id = position.id
    session.flush()
    _emit(
        session,
        signal,
        "position.opened",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"qty": qty, "avg_entry": str(entry), "stop": str(stop), "is_virtual": True},
        now=now,
        position=position,
    )
    return position


# --- exits ------------------------------------------------------------------------------------


def exit_rules_of(session: Session, signal: HatSignal) -> list[ExitRule]:
    """The exit stack of the strategy version the signal entered under: a version promoted
    after the entry never changes the rules of an open trade (HAT-BOT-083)."""
    version = session.get(HatStrategyVersion, signal.strategy_version_id)
    if version is None:
        raise TradeError("unknown_strategy_version")
    return list(parse_config(version.config).exits)


def _entry_qty(session: Session, signal: HatSignal) -> int:
    qty = session.scalar(
        select(func.coalesce(func.sum(HatFill.qty), 0))
        .join(HatOrder, HatOrder.id == HatFill.order_id)
        .where(HatOrder.signal_id == signal.id, HatOrder.purpose == "entry")
    )
    return int(qty or 0)


def trade_r(realized_pnl: Decimal, avg_entry: Decimal, initial_stop: Decimal, qty: int) -> Decimal:
    """HAT-BT-040: realised P&L over the initial risk (entry - initial stop) x entry quantity.
    Zero when there is no positive initial risk (the position was never validly sized)."""
    risk = (avg_entry - initial_stop) * qty
    if risk <= 0:
        return _ZERO
    return (realized_pnl / risk).quantize(_RATIO_PLACES)


def _locked_virtual_position(session: Session, position_id: uuid.UUID) -> HatPosition:
    """The position, re-read under its bot's lock; only a virtual one is the engine's."""
    position = session.get(HatPosition, position_id)
    if position is None:
        raise TradeError("unknown_position")
    lock_bot(session, position.bot_id)
    session.refresh(position)
    if not position.is_virtual:
        raise TradeError("position_not_virtual")  # real exits are the router's (PR 15)
    return position


def _raise_stop(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    old: Decimal,
    new: Decimal,
    now: datetime,
) -> None:
    _emit(
        session,
        signal,
        "stop.moved",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"from": str(old), "to": str(new)},
        now=now,
    )
    position.current_stop = new


def evaluate_position(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> exits.Evaluation:
    """Run the pinned exit stack on one virtual position and carry out what it says: raise the
    stop, sell partials, or close the position and write its trade result (HAT-EXEC-070..073).

    `high` and `low` are the extremes the price made since entry, for the result's MFE and MAE."""
    position = _locked_virtual_position(session, position_id)
    if position.closed_at is not None or position.qty_open <= 0:
        return exits.Evaluation()
    signal = session.get(HatSignal, position.signal_id)
    bot = session.get(HatBot, position.bot_id)
    if signal is None or bot is None:
        raise TradeError("unknown_signal")
    rules = exit_rules_of(session, signal)
    state = exits.PositionState(
        qty_original=_entry_qty(session, signal),
        qty_open=position.qty_open,
        avg_entry=Decimal(position.avg_entry),
        initial_stop=Decimal(position.initial_stop),
        current_stop=Decimal(position.current_stop),
        fired=frozenset(position.rules_fired or []),
    )
    result = exits.evaluate(rules, state, view, phase)
    price = view.price
    if result.actions and price is None:
        raise TradeError("exit_without_price")  # nothing is written for an exit with no price
    if result.new_stop is not None and result.new_stop > state.current_stop:
        _raise_stop(session, signal, position, state.current_stop, result.new_stop, now)
    for rule_id, reason in result.ignored:
        # A rule that would not raise the stop is logged, never applied (HAT-RISK-070).
        _log_ignored(rule_id, reason)
    position.rules_fired = sorted({*(position.rules_fired or []), *result.fired})
    if price is not None:
        for action in result.actions:
            _virtual_exit(session, signal, bot, position, action, price, now, high=high, low=low)
    session.flush()
    return result


def _log_ignored(rule_id: str, reason: str) -> None:
    logger.info("exit rule %s ignored: %s", rule_id, reason)


def _virtual_exit(
    session: Session,
    signal: HatSignal,
    bot: HatBot,
    position: HatPosition,
    action: exits.ExitAction,
    price: Decimal,
    now: datetime,
    *,
    high: Decimal | None,
    low: Decimal | None,
) -> None:
    full = action.kind == "full"
    purpose = "exit_full" if full else "exit_partial"
    order, _ = _virtual_order(
        session,
        signal,
        bot,
        position,
        purpose=purpose,
        side="sell",
        qty=action.qty,
        price=price,
        now=now,
    )
    rule = action.rule_ids[0].split(":", 1)[1]
    _emit(
        session,
        signal,
        "order.placed",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"purpose": purpose, "qty": action.qty, "is_virtual": True, "rule": rule},
        now=now,
        position=position,
    )
    _emit(
        session,
        signal,
        "order.filled",
        aggregate_type="order",
        aggregate_id=order.id,
        payload={"qty": action.qty, "price": str(price), "is_virtual": True},
        now=now,
    )
    position.qty_open -= action.qty
    if not full:
        _emit(
            session,
            signal,
            "exit.partial",
            aggregate_type="position",
            aggregate_id=position.id,
            payload={"qty": action.qty, "price": str(price), "rule": rule},
            now=now,
            position=position,
        )
        return
    position.closed_at = now
    _emit(
        session,
        signal,
        "position.closed",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"price": str(price), "rule": rule},
        now=now,
        position=position,
    )
    _write_result(session, signal, position, rule, now, high=high, low=low)


def _write_result(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    reason: str,
    now: datetime,
    *,
    high: Decimal | None,
    low: Decimal | None,
) -> None:
    """The one `hat_trade_result` of a closed position, in the closing transaction
    (HAT-SCH-057), and the ladder step for a real trade (a virtual one changes nothing)."""
    fills = session.execute(
        select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees)
        .join(HatOrder, HatOrder.id == HatFill.order_id)
        .where(HatOrder.signal_id == signal.id)
    ).all()
    pnl = _ZERO
    fees = _ZERO
    for side, qty, price, fee in fills:
        sign = 1 if side == "sell" else -1
        pnl += sign * qty * Decimal(price) - Decimal(fee)
        fees += Decimal(fee)
    entry = Decimal(position.avg_entry)
    stop = Decimal(position.initial_stop)
    qty = _entry_qty(session, signal)
    risk = entry - stop
    r = trade_r(pnl, entry, stop, qty)
    top = max(entry, high if high is not None else entry)
    bottom = min(entry, low if low is not None else entry)
    session.add(
        HatTradeResult(
            position_id=position.id,
            realized_pnl=pnl,
            fees=fees,
            currency="INR",
            r_multiple=r,
            holding_days=max(0, (trade_date_of(now) - signal.trade_date).days),
            exit_reason=reason,
            mfe_r=((top - entry) / risk).quantize(_RATIO_PLACES) if risk > 0 else _ZERO,
            mae_r=((bottom - entry) / risk).quantize(_RATIO_PLACES) if risk > 0 else _ZERO,
        )
    )
    policy = effective_version(load_versions(session, _owner_of(session, signal)), now)
    if policy is not None:
        controls.record_closed_trade(session, signal.bot_id, r, position.is_virtual, policy.policy)


def _owner_of(session: Session, signal: HatSignal) -> uuid.UUID:
    bot = session.get(HatBot, signal.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    return bot.user_id


# --- the end of the session -------------------------------------------------------------------


def expire_watchlist(session: Session, trade_date: date) -> int:
    """At the session end of `trade_date`, a watchlist row that never triggered ends as Expired
    (HAT-EXEC-041). Rows that triggered keep the status their signal gave them."""
    triggered = select(HatSignal.bot_id, HatSignal.playbook, HatSignal.instrument_id).where(
        HatSignal.trade_date == trade_date
    )
    keys = {tuple(row) for row in session.execute(triggered)}
    count = 0
    for entry in session.scalars(
        select(HatWatchlistEntry).where(
            HatWatchlistEntry.trade_date == trade_date,
            HatWatchlistEntry.status == lifecycle.WATCHLISTED,
        )
    ):
        if (entry.bot_id, entry.playbook, entry.instrument_id) in keys:
            entry.status = lifecycle.TRIGGERED
            continue
        entry.status = lifecycle.next_state(entry.status, "watchlist.expired")
        count += 1
    return count
