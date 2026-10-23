"""The open positions, with their bot's label and instrument's symbol read in batches."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument, HatPosition
from ..time import as_utc
from .bots import labels

__all__ = ["UNKNOWN", "PositionRow", "open_positions", "symbol_of", "symbols"]

UNKNOWN = "?"


@dataclass(frozen=True)
class PositionRow:
    id: uuid.UUID
    bot_id: uuid.UUID
    bot: str
    instrument_id: uuid.UUID
    symbol: str
    qty_open: int
    avg_entry: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    currency: str
    opened_at: datetime
    state: str
    is_virtual: bool


def symbols(session: Session, instrument_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Each instrument's symbol, read in one query (one without a symbol is left out)."""
    rows = session.execute(
        select(HatInstrument.id, HatInstrument.symbol).where(
            HatInstrument.id.in_(set(instrument_ids))
        )
    )
    return {instrument_id: symbol for instrument_id, symbol in rows if symbol}


def symbol_of(session: Session, instrument_id: uuid.UUID) -> str:
    return symbols(session, (instrument_id,)).get(instrument_id, UNKNOWN)


def open_positions(session: Session) -> list[PositionRow]:
    """The open positions, oldest first."""
    rows = list(
        session.scalars(
            select(HatPosition)
            .where(HatPosition.closed_at.is_(None), HatPosition.qty_open > 0)
            .order_by(HatPosition.opened_at, HatPosition.id)
        )
    )
    names = labels(session, (p.bot_id for p in rows))
    found = symbols(session, (p.instrument_id for p in rows))
    return [
        PositionRow(
            id=p.id,
            bot_id=p.bot_id,
            bot=names.get(p.bot_id, UNKNOWN),
            instrument_id=p.instrument_id,
            symbol=found.get(p.instrument_id, UNKNOWN),
            qty_open=p.qty_open,
            avg_entry=Decimal(p.avg_entry),
            initial_stop=Decimal(p.initial_stop),
            current_stop=Decimal(p.current_stop),
            currency=p.currency,
            opened_at=as_utc(p.opened_at),
            state=p.state,
            is_virtual=bool(p.is_virtual),
        )
        for p in rows
    ]
