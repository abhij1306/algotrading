"""The reference refresh against real Postgres (HAT-DATA-020, 021, 024): the unique keys the
database enforces hold through renames, delisting and a broker symbol moving between
instruments."""

from datetime import date, time

import pytest
from hat_mcp_support import security
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session

from app.hat.data import instruments as inst
from app.hat.data.symbols import symbol_master
from app.hat.models import HatBrokerSymbol, HatInstrument, HatMarket

pytestmark = pytest.mark.postgres


def test_reference__refresh_on_postgres(conn: Connection) -> None:
    session = Session(bind=conn)
    market = session.scalar(select(HatMarket).where(HatMarket.code == "NSE"))
    if market is None:
        market = HatMarket(
            code="NSE",
            timezone="Asia/Kolkata",
            currency="INR",
            session_open=time(9, 15),
            session_close=time(15, 30),
        )
        session.add(market)
        session.flush()
    held = session.scalar(select(func.count()).select_from(HatInstrument))
    day = date(2026, 1, 5)
    first = [security("INE000000001", "EQ", "AAA"), security("INE000000002", "EQ", "BBB")]
    inst.upsert_instruments(session, market.id, first, today=day)
    inst.refresh_fyers_symbols(session, symbol_master, market.id)
    # a rename keeps the id; the other stock is delisted and its broker row removed
    renamed = [security("INE000000001", "EQ", "CCC")]
    inst.upsert_instruments(session, market.id, renamed, today=date(2026, 1, 12))
    inst.refresh_fyers_symbols(session, symbol_master, market.id)
    # the symbol AAA now belongs to a different company: the broker unique key must not trip
    inst.upsert_instruments(
        session,
        market.id,
        [*renamed, security("INE000000003", "EQ", "AAA")],
        today=date(2026, 1, 19),
    )
    inst.refresh_fyers_symbols(session, symbol_master, market.id)
    assert session.scalar(select(func.count()).select_from(HatInstrument)) - held == 3
    mine = select(HatInstrument.id).where(HatInstrument.isin.like("INE00000000%"))
    rows = session.scalars(
        select(HatBrokerSymbol).where(HatBrokerSymbol.instrument_id.in_(mine))
    ).all()
    assert sorted(r.broker_symbol for r in rows) == ["NSE:AAA-EQ", "NSE:CCC-EQ"]
    session.rollback()
