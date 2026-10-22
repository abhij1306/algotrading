"""A held instrument's series change (PR 47, HAT-EXEC-146; research G17).

An instrument is (market, ISIN, series) (HAT-DATA-020), so when NSE moves a stock from EQ to BE
or BZ the weekly reference refresh marks the EQ row delisted and lists the stock under a new
instrument. A position opened on the old row keeps it: its prices stop, its Fyers symbol
(`-EQ`) takes no order, and the broker cancels its GTT. Here such a position is found by its
successor, the active instrument of the same market and ISIN under another series. While it has
one, the position is manage-only (its exits see no view, `daily/live.py:ExitViews`), its broker
stops go out under the successor's symbol (`StopDesk._target`), and the morning GTT check
(`TradeManager.check_ex_date_gtts`) alerts it URGENT on each run until the owner resolves it.

ASM and GSM are surveillance lists, not series: the instrument master holds no such flag, so
they are not seen here.

Reads only; nothing reaches a broker.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.instruments import BROKER_FYERS
from ..models import HatBrokerSymbol, HatInstrument, HatPosition

__all__ = ["ALERT_SERIES_CHANGED", "broker_symbol", "series_changed", "successor"]

ALERT_SERIES_CHANGED = "position.series_changed"


def successor(session: Session, instrument_id: uuid.UUID) -> HatInstrument | None:
    """The instrument a delisted row's stock trades under now: the active one of the same market
    and ISIN under another series (the oldest id when several). None for an active row, or
    when no other series of the ISIN is active."""
    old = session.get(HatInstrument, instrument_id)
    if old is None or old.active:
        return None
    return session.scalar(
        select(HatInstrument)
        .where(
            HatInstrument.market_id == old.market_id,
            HatInstrument.isin == old.isin,
            HatInstrument.series != old.series,
            HatInstrument.active.is_(True),
        )
        .order_by(HatInstrument.id)
        .limit(1)
    )


def series_changed(session: Session, position: HatPosition) -> HatInstrument | None:
    """The successor of an open position's instrument, else None."""
    if position.closed_at is not None:
        return None
    return successor(session, position.instrument_id)


def broker_symbol(session: Session, instrument_id: uuid.UUID) -> str | None:
    """The stored Fyers symbol of the instrument, if any."""
    return session.scalar(
        select(HatBrokerSymbol.broker_symbol).where(
            HatBrokerSymbol.instrument_id == instrument_id, HatBrokerSymbol.broker == BROKER_FYERS
        )
    )
