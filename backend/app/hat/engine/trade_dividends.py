"""The cash dividends a position earns (CA4, R-DIV), by the backtest's rule (CA3,
`backtest/loop.py`): a position holding shares at the start of an ex-date session earns its
dividends that day, the shares then held times the stored cash per share (`dividend_credit`,
to the paisa); a buy on the ex-date earns nothing, a sale on it does not lose the credit.

The shares held at the start of a day are read from the position's own history, oldest first:
its fills (a buy adds, a sale takes away) and its `position.rescaled` events (a split or bonus
sets the new share count). A closed trade's dividends are part of its result in the backtest,
paper and live alike (`positions.write_result`: its P&L and R include them, its `dividends`
column shows them). A dividend is stored on its ex-date's night, after a trade that closed that
day was written, and an amount can be corrected later, so `positions.restate_dividends` brings
every closed trade up to the stored dividends each night. An open shadow position's credit is
computed each time (`shadow_dividend_cash`): a shadow sleeve's equity is its nominal capital
plus its realised P&L plus these credits (`daily/entries.py`), so paper and the backtest
compare like for like. A real account's cash comes in the broker's funds.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.corporate_actions import TYPE_DIVIDEND
from ..governance import Mode
from ..models import HatCorporateAction, HatEvent, HatFill, HatOrder, HatPosition, HatSignal
from ..money import ZERO, dividend_credit
from ..time import as_utc, trade_date_of
from .rescale import EVENT_RESCALED

__all__ = [
    "Step",
    "held_at_start",
    "position_dividends",
    "shadow_dividend_cash",
    "trade_dividends",
    "trades_dividends",
]

BUY = "buy"
SELL = "sell"
RESCALE = "rescale"


@dataclass(frozen=True)
class Step:
    """One change to a position's shares: a buy or sale of `qty`, or a rescale to `qty`."""

    at: datetime
    kind: str
    qty: int


def held_at_start(steps: Iterable[Step], day: date) -> int:
    """The shares held at the start of `day`'s session: every step of an earlier trade date
    applied oldest first (a buy adds, a sale takes away, a rescale sets the count)."""
    held = 0
    for step in sorted(steps, key=lambda s: s.at):
        if trade_date_of(step.at) >= day:
            break
        if step.kind == BUY:
            held += step.qty
        elif step.kind == SELL:
            held -= step.qty
        else:
            held = step.qty
    return max(held, 0)


def position_dividends(
    steps: Sequence[Step], dividends: Iterable[tuple[date, Decimal]], through: date
) -> Decimal:
    """The cash the position's shares earned from `dividends` ((ex-date, cash per share)) going
    ex on or before `through`."""
    return sum(
        (
            dividend_credit(held_at_start(steps, ex_date), cash)
            for ex_date, cash in dividends
            if ex_date <= through
        ),
        ZERO,
    )


def _steps(session: Session, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[Step]]:
    """Each position's share history: its own fills (virtual on a virtual position, real on a
    real one) and its rescales."""
    out: dict[uuid.UUID, list[Step]] = defaultdict(list)
    for position_id, side, qty, at in session.execute(
        select(HatOrder.position_id, HatOrder.side, HatFill.qty, HatFill.filled_at)
        .join(HatFill, HatFill.order_id == HatOrder.id)
        .join(HatPosition, HatPosition.id == HatOrder.position_id)
        .where(HatOrder.position_id.in_(ids), HatFill.is_virtual == HatPosition.is_virtual)
    ):
        out[position_id].append(Step(as_utc(at), BUY if side == BUY else SELL, qty))
    for event in session.scalars(
        select(HatEvent).where(
            HatEvent.event_type == EVENT_RESCALED,
            HatEvent.aggregate_type == "position",
            HatEvent.aggregate_id.in_(ids),
        )
    ):
        qty = int(event.payload["to"]["qty_open"])
        out[event.aggregate_id].append(Step(as_utc(event.occurred_at), RESCALE, qty))
    return out


def _dividends(
    session: Session, instruments: set[uuid.UUID]
) -> Mapping[uuid.UUID, list[tuple[date, Decimal]]]:
    out: dict[uuid.UUID, list[tuple[date, Decimal]]] = defaultdict(list)
    for instrument_id, ex_date, cash in session.execute(
        select(
            HatCorporateAction.instrument_id,
            HatCorporateAction.ex_date,
            HatCorporateAction.cash_amount,
        ).where(
            HatCorporateAction.instrument_id.in_(instruments),
            HatCorporateAction.action_type == TYPE_DIVIDEND,
            HatCorporateAction.cash_amount.is_not(None),
        )
    ):
        out[instrument_id].append((ex_date, Decimal(cash)))
    return out


def trades_dividends(
    session: Session, positions: Sequence[tuple[uuid.UUID, uuid.UUID, date]]
) -> dict[uuid.UUID, Decimal]:
    """The dividends each of `positions` ((position, instrument, through)) earned on ex-dates
    on or before its `through`; a position that earned none is left out."""
    if not positions:
        return {}
    paid = _dividends(session, {instrument for _, instrument, _ in positions})
    held = [p for p in positions if paid.get(p[1])]
    if not held:
        return {}
    steps = _steps(session, [p[0] for p in held])
    out = {
        position_id: position_dividends(steps.get(position_id, []), paid[instrument], through)
        for position_id, instrument, through in held
    }
    return {k: v for k, v in out.items() if v}


def trade_dividends(session: Session, position: HatPosition, through: date) -> Decimal:
    """The dividends the position earned on ex-dates on or before `through` (its exit day)."""
    found = trades_dividends(session, [(position.id, position.instrument_id, through)])
    return found.get(position.id, ZERO)


def shadow_dividend_cash(session: Session, bot_id: uuid.UUID, through: date) -> Decimal:
    """The dividends the bot's open shadow positions earned on ex-dates on or before `through`
    (a closed one's are in its trade result's P&L). A veto trade of a skipped confirm signal is
    not the shadow book's (HAT-EXEC-130)."""
    positions = session.execute(
        select(HatPosition.id, HatPosition.instrument_id)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.is_virtual.is_(True),
            HatSignal.mode == Mode.SHADOW,
            HatPosition.closed_at.is_(None),
        )
    ).all()
    found = trades_dividends(session, [(p.id, p.instrument_id, through) for p in positions])
    return sum(found.values(), ZERO)
