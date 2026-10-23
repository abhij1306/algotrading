"""A trade's position as the shadow engine and the real path both read and close it
(HAT-EXEC-070..074, HAT-SCH-057, HAT-BOT-083).

`trades` (virtual positions) and `live_trades` (real ones) share these and nothing else: the
pinned exit stack and the position as the exit rules read it, a stop raise, and the one trade
result of a closed position. Like them, every function works in the caller's open transaction,
never commits, and records each state change through the journal (`engine/journal.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..execution.modes import SHADOW
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
from ..risk.policy import effective_version
from ..risk.policy_service import load_versions
from ..time import trade_date_of
from . import exits, journal, rescale
from .strategy.config import ExitRule, parse_config

__all__ = [
    "TradeError",
    "entry_qty",
    "exit_rules_of",
    "is_veto",
    "position_state",
    "raise_stop",
    "signal_and_bot",
    "trade_r",
    "write_result",
]

_ZERO = Decimal(0)

_RATIO_PLACES = Decimal("0.000001")


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
    return position.is_virtual and signal.mode != SHADOW


def raise_stop(
    session: Session,
    signal: HatSignal,
    position: HatPosition,
    old: Decimal,
    new: Decimal,
    now: datetime,
) -> None:
    journal.record(
        session,
        signal,
        "stop.moved",
        aggregate_type="position",
        aggregate_id=position.id,
        payload={"from": str(old), "to": str(new)},
        now=now,
        veto=is_veto(signal, position),
    )
    position.current_stop = new


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
    qty = entry_qty(session, signal, position)
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
