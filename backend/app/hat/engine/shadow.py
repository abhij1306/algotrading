"""The shadow engine against the database: virtual orders and fills, the exit stack on a virtual
position, veto trades and the watchlist expiry (HAT-EXEC-032, -041, -070..073, HAT-SCH-057). A
virtual sale is recorded by `positions.record_sale`, as a real one is (`live_trades`).

Every function works in the caller's open transaction, never commits, takes the bot's lock
first, and writes a state change together with the event that explains it, so a replay of the
events always reproduces the stored state (`journal.replay_state`). Nothing here reaches a
broker, and it imports none (CONF `import_graph_boundaries`); real exits are the router's.

**Veto trades (HAT-EXEC-032).** A confirm signal that is skipped or expires continues as a
virtual trade for the veto score: `open_veto_trade` opens it on the signal's own row, which has
reached its ending (Cancelled or Expired). Its events carry the signal id as every trade event
does (HAT-SCH-064) and `veto: true` in their payload; they move only the veto position's state,
so `journal.replay_state` folds the signal's own events and `journal.replay_veto_state` the veto
trade's, which starts at EntryPlaced with its virtual order.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..configs.records import Bot
from ..configs.unit import configs_of
from ..costs import DEFAULT_COST_MODEL, DEFAULT_SLIPPAGE_BPS, BacktestFills, load_cost_schedule
from ..execution.tags import encode_tag
from ..governance import Mode
from ..ids import uuid7
from ..models import (
    HatApproval,
    HatFill,
    HatOrder,
    HatPosition,
    HatSignal,
    HatWatchlistEntry,
)
from ..time import day_bounds, trade_date_of
from . import exits, journal, lifecycle
from .botlock import lock_bot
from .positions import Trade, TradeError, evaluate_stack, fill_payload, record_sale, trade_of
from .snapshot import entry_price_of

__all__ = [
    "SHADOW_FILLS",
    "FillModel",
    "close_position",
    "evaluate_position",
    "expire_watchlist",
    "open_veto_trade",
    "open_virtual",
]


class FillModel(Protocol):
    """How a virtual fill is priced: (fill price, fees) for `qty` shares at the reference
    `price` on `side` ("buy" or "sell") on trade date `day`, under the charges in force on it; a
    sale of an instrument already `sold_today` pays no second DP charge."""

    def fill(
        self, side: str, qty: int, price: Decimal, day: date, /, *, sold_today: bool = False
    ) -> tuple[Decimal, Decimal]: ...


# Shadow trading's fills carry the backtest's default cost model (QA4): the default slippage
# against the trade, then the statutory charges, so a virtual result is net of what a real one
# pays. A backtest run passes its own (its slippage and cost model); both fill through the same
# functions (HAT-BT-010).
SHADOW_FILLS: FillModel = BacktestFills(
    load_cost_schedule(DEFAULT_COST_MODEL), DEFAULT_SLIPPAGE_BPS
)


def _next_tag(session: Session, signal_id: uuid.UUID, purpose: str) -> str:
    count = session.scalar(
        select(func.count())
        .select_from(HatOrder)
        .where(HatOrder.signal_id == signal_id, HatOrder.purpose == purpose)
    )
    return encode_tag(signal_id, purpose, int(count or 0))


def _virtual_order(
    session: Session,
    trade: Trade,
    *,
    purpose: str,
    side: str,
    qty: int,
    fill: tuple[Decimal, Decimal],
    now: datetime,
) -> tuple[HatOrder, HatFill]:
    """A virtual order and its fill at `fill` (price, fees): no broker id, no broker trade id."""
    price, fees = fill
    signal = trade.signal
    order = HatOrder(
        id=uuid7(),
        signal_id=signal.id,
        position_id=None if trade.position is None else trade.position.id,
        broker_account_id=trade.bot.broker_account_id,
        purpose=purpose,
        side=side,
        qty=qty,
        order_type="market",
        tag=_next_tag(session, signal.id, purpose),
        broker_order_id=None,
        status="filled",
        is_virtual=True,
        placed_at=now,
    )
    session.add(order)
    session.flush()
    fill = HatFill(
        id=uuid7(),
        order_id=order.id,
        qty=qty,
        price=price,
        fees=fees,
        filled_at=now,
        broker_trade_id=None,
        is_virtual=True,
    )
    session.add(fill)
    session.flush()
    return order, fill


def open_virtual(
    session: Session,
    trade: Trade,
    qty: int,
    stop: Decimal,
    fill: tuple[Decimal, Decimal],
    now: datetime,
) -> HatPosition:
    """Open the virtual position of `trade` (it has none yet) with its entry order and fill."""
    signal, veto = trade.signal, trade.veto
    entry = fill[0]
    order, _ = _virtual_order(
        session, trade, purpose="entry", side="buy", qty=qty, fill=fill, now=now
    )
    placed = {"purpose": "entry", "qty": qty, "is_virtual": True, "tag": order.tag}
    filled = fill_payload(order, qty, entry)
    journal.record(
        session, signal, journal.on_order(order, "order.placed", placed, now=now, veto=veto)
    )
    journal.record(
        session, signal, journal.on_order(order, "order.filled", filled, now=now, veto=veto)
    )
    position = HatPosition(
        id=uuid7(),
        bot_id=trade.bot.id,
        signal_id=signal.id,
        instrument_id=signal.instrument_id,
        is_virtual=True,
        qty_open=qty,
        avg_entry=entry,
        initial_stop=stop,
        current_stop=stop,
        opened_at=now,
        state=lifecycle.OPEN,
        rules_fired=[],
    )
    session.add(position)
    session.flush()  # the position row first: the order's foreign key points at it
    order.position_id = position.id
    session.flush()
    opened = {"qty": qty, "avg_entry": str(entry), "stop": str(stop), "is_virtual": True}
    journal.record(
        session,
        signal,
        journal.on_position(position, "position.opened", opened, now=now, veto=veto),
    )
    return position


# --- veto trades ------------------------------------------------------------------------------


def _veto_position(session: Session, signal_id: uuid.UUID) -> HatPosition | None:
    return session.scalar(
        select(HatPosition).where(
            HatPosition.signal_id == signal_id, HatPosition.is_virtual.is_(True)
        )
    )


def open_veto_trade(session: Session, signal_id: uuid.UUID, now: datetime) -> HatPosition | None:
    """Open the virtual trade of a skipped or expired confirm signal for the veto score
    (HAT-EXEC-032, HAT-TG-045), at the entry price, quantity and stop the card offered, under
    the bot's lock. At most one per signal: a second call returns the one already open. None
    when the signal is not a decided-away confirm signal or was never sized."""
    signal = session.get(HatSignal, signal_id)
    if signal is None:
        raise TradeError("unknown_signal")
    lock_bot(session, signal.bot_id)
    session.refresh(signal)
    approval = session.get(HatApproval, signal_id)
    if (
        signal.mode != Mode.CONFIRM
        or approval is None
        or approval.decision not in ("skip", "expired")
        or signal.state not in (lifecycle.CANCELLED, lifecycle.EXPIRED)
        or signal.qty <= 0
    ):
        return None
    existing = _veto_position(session, signal_id)
    if existing is not None:
        return existing
    bot = configs_of(session).get(Bot, signal.bot_id)
    if bot is None:
        raise TradeError("unknown_bot")
    fill = SHADOW_FILLS.fill("buy", signal.qty, entry_price_of(session, signal), trade_date_of(now))
    trade = Trade(signal, bot, None, veto=True)
    return open_virtual(session, trade, signal.qty, Decimal(signal.stop_price), fill, now)


# --- exits ------------------------------------------------------------------------------------


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


def evaluate_position(
    session: Session,
    position_id: uuid.UUID,
    view: exits.ExitView,
    phase: exits.Phase,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
    fills: FillModel = SHADOW_FILLS,
) -> exits.Evaluation:
    """Run the pinned exit stack on one virtual position and carry out what it says: raise the
    stop, sell partials, or close the position and write its trade result (HAT-EXEC-070..073).

    `high` and `low` are the extremes the price made since entry, for the result's MFE and MAE.
    A sale fills at `view.price` through `fills`, in this transaction, so its rules are marked
    fired with it."""
    position = _locked_virtual_position(session, position_id)
    result = evaluate_stack(session, position, view, phase, now)
    if result.actions and view.price is not None:
        trade = trade_of(session, position)
        position.rules_fired = sorted({*(position.rules_fired or []), *result.fired})
        marks = _Marks(fills, high, low)
        for action in result.actions:
            sale = _Sale(action.kind == "full", action.qty, action.rule_ids[0].split(":", 1)[1])
            _virtual_exit(session, trade, sale, view.price, now, marks)
    session.flush()
    return result


def close_position(
    session: Session,
    position_id: uuid.UUID,
    price: Decimal,
    reason: str,
    now: datetime,
    *,
    high: Decimal | None = None,
    low: Decimal | None = None,
    fills: FillModel = SHADOW_FILLS,
) -> bool:
    """Sell the whole open quantity of a virtual position at `price` for a `reason` outside the
    exit stack (the backtest's period end, an instrument's last traded day) and write its trade
    result. False when the position was already closed."""
    position = _locked_virtual_position(session, position_id)
    if position.closed_at is not None or position.qty_open <= 0:
        return False
    sale = _Sale(True, position.qty_open, reason)
    _virtual_exit(session, trade_of(session, position), sale, price, now, _Marks(fills, high, low))
    session.flush()
    return True


@dataclass(frozen=True)
class _Sale:
    full: bool
    qty: int
    rule: str


@dataclass(frozen=True)
class _Marks:
    """How a sale fills, and the extremes since entry for the trade result."""

    fills: FillModel
    high: Decimal | None
    low: Decimal | None


def _sold_on(session: Session, trade: Trade, day: date) -> bool:
    """Whether the trade's broker account already sold the instrument virtually on `day`: the
    DP charge is one debit per instrument per sell day, however many sales."""
    position = trade.position
    if position is None:
        return False
    start, end = day_bounds(day)
    earlier = (
        select(HatOrder.id)
        .join(HatPosition, HatPosition.id == HatOrder.position_id)
        .where(
            HatOrder.broker_account_id == trade.bot.broker_account_id,
            HatOrder.side == "sell",
            HatOrder.is_virtual.is_(True),
            HatPosition.instrument_id == position.instrument_id,
            HatOrder.placed_at >= start,
            HatOrder.placed_at < end,
        )
        .limit(1)
    )
    return session.scalar(earlier) is not None


def _virtual_exit(
    session: Session, trade: Trade, sale: _Sale, price: Decimal, now: datetime, marks: _Marks
) -> None:
    """Sell `sale` from the virtual position: its order placed and filled at once, the fill
    recorded by `positions.record_sale`."""
    if trade.position is None:
        raise TradeError("no_open_position")  # an exit sells from the open position
    purpose = "exit_full" if sale.full else "exit_partial"
    day = trade_date_of(now)
    sold = _sold_on(session, trade, day)
    fill = marks.fills.fill("sell", sale.qty, price, day, sold_today=sold)
    order, _ = _virtual_order(
        session, trade, purpose=purpose, side="sell", qty=sale.qty, fill=fill, now=now
    )
    placed = {"purpose": purpose, "qty": sale.qty, "is_virtual": True, "rule": sale.rule}
    journal.record(
        session,
        trade.signal,
        journal.on_order(
            order, "order.placed", placed, now=now, position=trade.position, veto=trade.veto
        ),
    )
    record_sale(
        session, trade, order, sale.qty, fill[0], sale.rule, at=now, high=marks.high, low=marks.low
    )


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
