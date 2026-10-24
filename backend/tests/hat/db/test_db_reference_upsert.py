"""The reference refresh against real Postgres: the unique keys the
database enforces hold through renames, delisting and a broker symbol moving between
instruments."""

from datetime import date, time

import pytest
from hat_mcp_support import security
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session

from app.hat.data import instruments as inst
from app.hat.data.symbols import symbol_master
from app.hat.models import (
    HatBrokerSymbol,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatMarket,
)

pytestmark = pytest.mark.postgres


def _market(session: Session) -> HatMarket:
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
    return market


def test_reference__refresh_on_postgres(conn: Connection) -> None:
    session = Session(bind=conn)
    market = _market(session)
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


def test_reference__bars_rename_date_wins_over_the_masters_day(conn: Connection) -> None:
    """TATAMOTORS -> TMPV (D1): the master saw the rename on its first run after it; the bars'
    rename date then makes one TMPV interval from the day it took effect."""
    session = Session(bind=conn)
    market = _market(session)
    iid = inst.register_history_instrument(
        session,
        market.id,
        isin="INE000000009",
        series="EQ",
        symbol="OLDX",
        first_seen=date(2011, 9, 13),
    )
    inst.upsert_instruments(
        session, market.id, [security("INE000000009", "EQ", "NEWX")], today=date(2026, 10, 22)
    )
    rename = inst.SymbolRename("OLDX", "NEWX", date(2025, 10, 24), "INE000000009")
    for _ in range(2):  # idempotent
        inst.seed_symbol_history(session, market.id, [rename])
        session.flush()
        rows = session.scalars(
            select(HatInstrumentSymbolHistory)
            .where(HatInstrumentSymbolHistory.instrument_id == iid)
            .order_by(HatInstrumentSymbolHistory.valid_from)
        ).all()
        assert [(r.symbol, r.valid_from, r.valid_to) for r in rows] == [
            ("OLDX", date(2011, 9, 13), date(2025, 10, 24)),
            ("NEWX", date(2025, 10, 24), None),
        ]
    session.rollback()
