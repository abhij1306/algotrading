"""The snapshot the caps read (HAT-RISK-030, HAT-RISK-036).

What an entry decision needs from the database, read in the caller's transaction under the bot's
lock (and, for an entry, the owner's): the slots of the owner's bots, the bot's entries and
stop-outs today, the latest hard stop of an instrument for the cooldown, and the kill, ladder and
risk-policy inputs. A slot is an open position, or a signal whose entry is triggered, placed or
awaiting approval and has no position yet; a veto trade (HAT-EXEC-032) is scored only and holds
none. Nothing here writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..execution.modes import SHADOW
from ..models import HatBot, HatEvent, HatIndustry, HatPosition, HatSignal, HatTradeResult
from ..risk.caps import OpenPosition
from ..time import as_utc, day_bounds, trade_date_of
from . import journal, lifecycle

__all__ = [
    "HARD_STOP",
    "bot_snapshot",
    "day_counts",
    "entry_price_of",
    "last_hard_stop",
    "owner_slots",
]

HARD_STOP = "hard_stop"
# An accepted auto signal rests in Triggered until the router places its order: it holds a slot.
_SLOT_PENDING_STATES = (lifecycle.TRIGGERED, lifecycle.AWAITING_APPROVAL, lifecycle.ENTRY_PLACED)

_ENTERED_STATES = (
    lifecycle.TRIGGERED,
    lifecycle.AWAITING_APPROVAL,
    lifecycle.ENTRY_PLACED,
    lifecycle.OPEN,
    lifecycle.PARTIALLY_EXITED,
    lifecycle.EXIT_PENDING,
    lifecycle.CLOSED,
)


def _industry_of(session: Session, instrument_id: uuid.UUID, day: date) -> str:
    row = session.scalar(
        select(HatIndustry.industry)
        .where(HatIndustry.instrument_id == instrument_id, HatIndustry.effective_from <= day)
        .order_by(HatIndustry.effective_from.desc())
        .limit(1)
    )
    return row or "unknown"


# A veto trade is a virtual position of a confirm or auto signal (`positions.is_veto`).
_NOT_VETO = or_(HatPosition.is_virtual.is_(False), HatSignal.mode == SHADOW)


def _slots(
    session: Session,
    bot_ids: Sequence[uuid.UUID] | Select[tuple[uuid.UUID]],
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None,
) -> list[OpenPosition]:
    """The slots of the bots in `bot_ids`, read now, each carrying its own bot.

    A slot is an open position, or a signal whose entry is triggered, placed or awaiting
    approval and has no position yet. A veto trade (the virtual trade of a skipped or expired
    confirm signal, HAT-EXEC-032) is scored only and holds no slot. A position with no price in
    `last_prices` is valued at its entry.

    The pending signals are read before the positions: another bot's fill (under that bot's
    lock only) can turn a pending signal into a position between the two reads, and in this
    order it is then counted twice, never missed."""
    prices = last_prices or {}
    pending = session.scalars(
        select(HatSignal).where(
            HatSignal.bot_id.in_(bot_ids), HatSignal.state.in_(_SLOT_PENDING_STATES)
        )
    ).all()
    slots: list[OpenPosition] = []
    for position in session.scalars(
        select(HatPosition)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(HatPosition.bot_id.in_(bot_ids), HatPosition.closed_at.is_(None), _NOT_VETO)
    ):
        entry = Decimal(position.avg_entry)
        slots.append(
            OpenPosition(
                bot_id=position.bot_id,
                instrument_id=position.instrument_id,
                industry=_industry_of(session, position.instrument_id, trade_date),
                qty=position.qty_open,
                avg_entry=entry,
                last_price=prices.get(position.instrument_id, entry),
                current_stop=Decimal(position.current_stop),
                is_virtual=position.is_virtual,
            )
        )
    for signal in pending:
        entry = entry_price_of(session, signal)  # the planned price, not the level broken
        slots.append(
            OpenPosition(
                bot_id=signal.bot_id,
                instrument_id=signal.instrument_id,
                industry=_industry_of(session, signal.instrument_id, trade_date),
                qty=signal.qty,
                avg_entry=entry,
                last_price=prices.get(signal.instrument_id, entry),
                current_stop=Decimal(signal.stop_price),
                is_virtual=signal.mode == SHADOW,
            )
        )
    return slots


def owner_slots(
    session: Session,
    user_id: uuid.UUID,
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None = None,
) -> list[OpenPosition]:
    """The slots of every bot of the owner, real and virtual, on every broker account
    (HAT-RISK-030): the global open-risk cap sums the ones of the entry's mode, the per-bot caps
    the entry's bot's own."""
    bots = select(HatBot.id).where(HatBot.user_id == user_id)
    return _slots(session, bots, trade_date, last_prices)


def bot_snapshot(
    session: Session,
    bot_id: uuid.UUID,
    trade_date: date,
    last_prices: dict[uuid.UUID, Decimal] | None = None,
) -> tuple[list[OpenPosition], int, int]:
    """(slots, entries today, stop-outs today) for the bot, read now (slots as `_slots`). A veto
    trade's stop-out is not the bot's. These are the bot's own slots only: an entry decision
    reads `owner_slots`, since the global open-risk cap sums every bot."""
    slots = _slots(session, [bot_id], trade_date, last_prices)
    entries, stopouts = day_counts(session, bot_id, trade_date)
    return slots, entries, stopouts


def day_counts(session: Session, bot_id: uuid.UUID, trade_date: date) -> tuple[int, int]:
    """(entries today, stop-outs today) for the bot."""
    entries = session.scalar(
        select(func.count())
        .select_from(HatSignal)
        .where(
            HatSignal.bot_id == bot_id,
            HatSignal.trade_date == trade_date,
            HatSignal.state.in_(_ENTERED_STATES),
        )
    )
    start, end = day_bounds(trade_date)
    stopouts = session.scalar(
        select(func.count())
        .select_from(HatTradeResult)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.closed_at >= start,
            HatPosition.closed_at < end,
            HatTradeResult.exit_reason == HARD_STOP,
            _NOT_VETO,
        )
    )
    return int(entries or 0), int(stopouts or 0)


def last_hard_stop(
    session: Session, bot_id: uuid.UUID, instrument_id: uuid.UUID, at: datetime
) -> tuple[date, ...]:
    """The trade date of the bot's latest `hard_stop` close of the instrument at or before `at`,
    as the cooldown reads it (HAT-RISK-036); empty when there is none. A veto trade's stop-out is
    not the bot's, as for the daily stop-out count."""
    closed = session.scalar(
        select(func.max(HatPosition.closed_at))
        .select_from(HatTradeResult)
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .outerjoin(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.instrument_id == instrument_id,
            HatPosition.closed_at <= at,
            HatTradeResult.exit_reason == HARD_STOP,
            _NOT_VETO,
        )
    )
    return () if closed is None else (trade_date_of(as_utc(closed)),)


def entry_price_of(session: Session, signal: HatSignal) -> Decimal:
    """The entry price the trigger planned, from its `signal.triggered` event (else the level)."""
    payload = session.scalar(
        select(HatEvent.payload).where(
            HatEvent.correlation_id == signal.id, HatEvent.event_type == journal.SIGNAL_TRIGGERED
        )
    )
    raw = (payload or {}).get("entry_price")
    price = Decimal(str(raw)) if raw is not None else Decimal(signal.trigger_price)
    return price if price.is_finite() and price > 0 else Decimal(signal.trigger_price)
