"""A trade's position as the shadow engine and the real path both read and close it
(HAT-EXEC-070..074, HAT-SCH-057, HAT-BOT-083).

`shadow` (virtual positions) and `live_trades` (real ones) share these and nothing else: the
pinned exit stack and the position as the exit rules read it, one evaluation of that stack
(`evaluate_stack`), a stop raise that only ever raises, one recording of a sale fill
(`record_sale`) and the one trade result of a closed position. Like them, every function works
in the caller's open transaction, never commits, and records each state change through the
journal (`engine/journal.py`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import (
    HatBot,
    HatFill,
    HatOrder,
    HatPosition,
    HatSignal,
    HatStrategyVersion,
    HatTradeResult,
)
from ..risk import controls
from ..risk.modes import Mode
from ..risk.policy import effective_version
from ..risk.policy_service import load_versions
from ..time import trade_date_of
from . import exits, journal, lifecycle, rescale
from .strategy.config import ExitRule, parse_config

__all__ = [
    "STOP_PURPOSES",
    "Trade",
    "TradeError",
    "close_trade",
    "entry_qty",
    "evaluate_stack",
    "exit_rules_of",
    "fill_payload",
    "is_veto",
    "position_state",
    "raise_stop",
    "record_sale",
    "signal_and_bot",
    "sold_qty",
    "trade_of",
    "trade_r",
    "write_result",
]

logger = logging.getLogger(__name__)

_ZERO = Decimal(0)

_RATIO_PLACES = Decimal("0.000001")

STOP_PURPOSES = ("stop_day", "stop_gtt")
_OPEN_STATES = (lifecycle.OPEN, lifecycle.PARTIALLY_EXITED)


class TradeError(HatError):
    """A lifecycle operation was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def exit_rules_of(session: Session, signal: HatSignal) -> list[ExitRule]:
    """The exit stack of the strategy version the signal entered under: a version promoted
    after the entry never changes the rules of an open trade (HAT-BOT-083)."""
    version = session.get(HatStrategyVersion, signal.strategy_version_id)
    if version is None:
        raise TradeError("unknown_strategy_version")
    return list(parse_config(version.config).exits)


def entry_qty(session: Session, signal: HatSignal, position: HatPosition) -> int:
    """The shares the entry bought, on the position's current scale: each split or bonus applied
    to it since (`position.rescaled`) scales them as it scaled `qty_open`, so R and the partial
    exits stay on one scale (HAT-EXEC-141)."""
    qty = int(
        session.scalar(
            select(func.coalesce(func.sum(HatFill.qty), 0))
            .join(HatOrder, HatOrder.id == HatFill.order_id)
            .where(HatOrder.signal_id == signal.id, HatOrder.purpose == "entry")
        )
        or 0
    )
    for factor in rescale.applied_factors(session, position.id):
        qty = rescale.scale_qty(qty, factor)
    return qty


def trade_r(realized_pnl: Decimal, avg_entry: Decimal, initial_stop: Decimal, qty: int) -> Decimal:
    """HAT-BT-040: realised P&L over the initial risk (entry - initial stop) x entry quantity.
    Zero when there is no positive initial risk (the position was never validly sized)."""
    risk = (avg_entry - initial_stop) * qty
    if risk <= 0:
        return _ZERO
    return (realized_pnl / risk).quantize(_RATIO_PLACES)


def is_veto(signal: HatSignal, position: HatPosition) -> bool:
    """A virtual position of a confirm or auto signal is that signal's veto trade."""
    return position.is_virtual and signal.mode != Mode.SHADOW


@dataclass(frozen=True)
class Trade:
    """The rows one trade's writes name: its signal, bot and position (None before the entry
    opens it), and whether it is the signal's veto trade (its events are flagged `veto`)."""

    signal: HatSignal
    bot: HatBot
    position: HatPosition | None
    veto: bool = False


def trade_of(session: Session, position: HatPosition) -> Trade:
    signal, bot = signal_and_bot(session, position)
    return Trade(signal, bot, position, is_veto(signal, position))


def raise_stop(
    session: Session, signal: HatSignal, position: HatPosition, new: Decimal, now: datetime
) -> None:
    """Move the position's stop up to `new` (`stop.moved`). A stop never lowers (invariant 1):
    `new` at or below the current stop raises `TradeError("stop_would_lower")` and writes
    nothing. This is the one Python check, so the SQLite backtest, which has no Postgres
    trigger, keeps the rule too; only a corporate-action rescale moves a stop down
    (`rescale.py`)."""
    old = Decimal(position.current_stop)
    if not new > old:
        raise TradeError("stop_would_lower")
    journal.record(
        session,
        signal,
        journal.on_position(
            position,
            "stop.moved",
            {"from": str(old), "to": str(new)},
            now=now,
            veto=is_veto(signal, position),
        ),
    )
    position.current_stop = new


def evaluate_stack(
    session: Session,
    position: HatPosition,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
) -> exits.Evaluation:
    """Run the pinned exit stack on one position held under its bot's lock, the same for a
    virtual and a real one (T12): raise the stop, log the rules ignored, and mark fired the
    rules that sell nothing. The sales are returned; the caller carries them out (virtual) or
    sends them (real) and marks their rules. A closed or emptied position evaluates to nothing;
    a sale with no price raises `TradeError("exit_without_price")` before anything is
    written."""
    if position.closed_at is not None or position.qty_open <= 0:
        return exits.Evaluation()
    signal, _ = signal_and_bot(session, position)
    rules, state = position_state(session, position)
    result = exits.evaluate(rules, state, view, phase)
    if result.actions and view.price is None:
        raise TradeError("exit_without_price")
    if result.new_stop is not None:
        raise_stop(session, signal, position, result.new_stop, now)
    for rule_id, reason in result.ignored:
        # A rule that would not raise the stop is logged, never applied (HAT-RISK-070).
        logger.info("exit rule %s ignored: %s", rule_id, reason)
    selling = {rid for action in result.actions for rid in action.rule_ids}
    instant = [rid for rid in result.fired if rid not in selling]
    position.rules_fired = sorted({*(position.rules_fired or []), *instant})
    return result


def fill_payload(order: HatOrder, qty: int, price: Decimal) -> dict[str, object]:
    """The payload of an order's `order.filled`: a real one also names its purpose and tag."""
    if order.is_virtual:
        return {"qty": qty, "price": str(price), "is_virtual": True}
    return {
        "qty": qty,
        "price": str(price),
        "purpose": order.purpose,
        "tag": order.tag,
        "is_virtual": False,
    }


def sold_qty(qty_open: int, qty: int) -> int:
    """The shares still open after a sale of `qty`: a sale larger than the open quantity leaves
    none, never a negative quantity."""
    return max(0, qty_open - qty)


def record_sale(
    session: Session,
    trade: Trade,
    order: HatOrder,
    qty: int,
    price: Decimal,
    rule: str,
    *,
    at: datetime,
    entry_working: bool = False,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> bool:
    """Record one fill of `qty` shares at `price` of a sell order against the trade's open
    position, virtual or real, once the caller has stored its `hat_fill` row: `order.filled`,
    the open quantity, then a partial sale (`exit.partial`) or, for the last share, the close
    (`close_trade`). True when the position closed.

    A sale larger than the open quantity is logged and empties the position (the broker's book is
    then reconciliation's). While `entry_working` (a real entry the broker may still fill) an
    emptied position stays open at quantity 0. The first fill of a broker stop, or one that
    empties a position no full exit order was placed for, stands for the exit order placed
    (`exit_triggered`). `high` and `low` are a virtual trade's extremes since entry."""
    signal, position = trade.signal, trade.position
    if position is None:
        raise TradeError("no_open_position")  # a sale sells from the open position
    if qty > position.qty_open:
        logger.error("sale of %s shares exceeds the open quantity of a position", qty)
    remaining = sold_qty(position.qty_open, qty)
    order.position_id = position.id
    payload = fill_payload(order, qty, price)
    if _trade_state(trade) in _OPEN_STATES and (order.purpose in STOP_PURPOSES or remaining == 0):
        payload[lifecycle.EXIT_TRIGGERED] = True
    journal.record(
        session,
        signal,
        journal.on_order(
            order, "order.filled", payload, now=at, position=position, veto=trade.veto
        ),
    )
    position.qty_open = remaining
    if remaining > 0 or entry_working:
        if order.purpose == "exit_partial" and _trade_state(trade) in _OPEN_STATES:
            partial = {"qty": qty, "price": str(price), "rule": rule}
            journal.record(
                session,
                signal,
                journal.on_position(position, "exit.partial", partial, now=at, veto=trade.veto),
            )
        session.flush()
        return False
    close_trade(session, trade, rule, price, at=at, high=high, low=low)
    return True


def close_trade(
    session: Session,
    trade: Trade,
    rule: str,
    price: Decimal,
    *,
    at: datetime,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> None:
    """Close the trade's emptied position at its last sale's `price` and `rule`:
    `position.closed` and the trade result, in this transaction."""
    signal, position = trade.signal, trade.position
    if position is None:
        raise TradeError("no_open_position")
    position.closed_at = at
    closed = {"price": str(price), "rule": rule}
    journal.record(
        session,
        signal,
        journal.on_position(position, "position.closed", closed, now=at, veto=trade.veto),
    )
    write_result(session, signal, position, rule, at, high=high, low=low)
    session.flush()


def _trade_state(trade: Trade) -> str:
    """Where the trade stands: a veto trade moves only its position (its signal has ended)."""
    if trade.veto and trade.position is not None:
        return trade.position.state
    return trade.signal.state


def signal_and_bot(session: Session, position: HatPosition) -> tuple[HatSignal, HatBot]:
    signal = session.get(HatSignal, position.signal_id)
    bot = session.get(HatBot, position.bot_id)
    if signal is None or bot is None:
        raise TradeError("unknown_signal")
    return signal, bot


def position_state(
    session: Session, position: HatPosition
) -> tuple[list[ExitRule], exits.PositionState]:
    """The pinned exit stack of a position and the position as the exit rules read it: what
    `evaluate_position` hands to `exits.evaluate`."""
    signal, _ = signal_and_bot(session, position)
    state = exits.PositionState(
        qty_original=entry_qty(session, signal, position),
        qty_open=position.qty_open,
        avg_entry=Decimal(position.avg_entry),
        initial_stop=Decimal(position.initial_stop),
        current_stop=Decimal(position.current_stop),
        fired=frozenset(position.rules_fired or []),
    )
    return exit_rules_of(session, signal), state


def write_result(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    reason: str,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> None:
    """The one `hat_trade_result` of a closed position, in the closing transaction
    (HAT-SCH-057), and the ladder step for a real trade (a virtual one changes nothing).

    MFE and MAE are measured on a virtual trade from `high` and `low`, the extremes since its
    entry (none given counts as the entry price). A real trade has no record of its extremes
    since entry, so its MFE and MAE are stored NULL, never a made-up zero."""
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
    qty = entry_qty(session, signal, position)
    risk = entry - stop
    r = trade_r(pnl, entry, stop, qty)
    mfe, mae = _excursions(entry, risk, high, low) if position.is_virtual else (None, None)
    session.add(
        HatTradeResult(
            position_id=position.id,
            realized_pnl=pnl,
            fees=fees,
            currency="INR",
            r_multiple=r,
            holding_days=max(0, (trade_date_of(now) - signal.trade_date).days),
            exit_reason=reason,
            mfe_r=mfe,
            mae_r=mae,
        )
    )
    policy = effective_version(load_versions(session, _owner_of(session, signal)), now)
    if policy is not None:
        controls.record_closed_trade(session, signal.bot_id, r, position.is_virtual, policy.policy)


def _excursions(
    entry: Decimal, risk: Decimal, high: Decimal | None, low: Decimal | None
) -> tuple[Decimal, Decimal]:
    """(MFE, MAE) in R of a trade whose price ranged over `low`..`high` since entry."""
    if risk <= 0:
        return _ZERO, _ZERO
    top = max(entry, high if high is not None else entry)
    bottom = min(entry, low if low is not None else entry)
    return ((top - entry) / risk).quantize(_RATIO_PLACES), ((bottom - entry) / risk).quantize(
        _RATIO_PLACES
    )


def _owner_of(session: Session, signal: HatSignal) -> uuid.UUID:
    bot = session.get(HatBot, signal.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    return bot.user_id
