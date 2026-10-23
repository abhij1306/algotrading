"""The trading universe: the active NSE instruments of series EQ or BE (HAT-DATA-041).

The nightly ingest and the quality gate read the members; the minute history reads them with
their Fyers symbol (`with_broker_symbol`), since a member without one cannot be asked for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatBrokerSymbol, HatInstrument, HatMarket
from ..models.strategy import BROKER_FYERS
from ..ops.jobs import JobError

UNIVERSE_SERIES = ("EQ", "BE")
CODE_NO_MARKET = "ingest_no_market"


@dataclass(frozen=True)
class Member:
    """One active EQ/BE instrument of the universe; `broker_symbol` is its Fyers symbol when
    asked for (None when it has none, or when not asked)."""

    instrument_id: uuid.UUID
    isin: str
    symbol: str
    series: str
    broker_symbol: str | None = None


def active_members(session: Session, *, with_broker_symbol: bool = False) -> list[Member]:
    """The active NSE instruments with series EQ or BE, in a stable order; `JobError` with
    `CODE_NO_MARKET` when there is no NSE market."""
    market_id = session.scalar(select(HatMarket.id).where(HatMarket.code == "NSE"))
    if market_id is None:
        raise JobError(CODE_NO_MARKET)
    columns = [HatInstrument.id, HatInstrument.isin, HatInstrument.symbol, HatInstrument.series]
    query = select(*columns)
    if with_broker_symbol:
        query = select(*columns, HatBrokerSymbol.broker_symbol).outerjoin(
            HatBrokerSymbol,
            (HatBrokerSymbol.instrument_id == HatInstrument.id)
            & (HatBrokerSymbol.broker == BROKER_FYERS),
        )
    rows = session.execute(
        query.where(
            HatInstrument.market_id == market_id,
            HatInstrument.active.is_(True),
            HatInstrument.series.in_(UNIVERSE_SERIES),
        ).order_by(HatInstrument.symbol, HatInstrument.series, HatInstrument.id)
    ).all()
    return [Member(*row) for row in rows]
