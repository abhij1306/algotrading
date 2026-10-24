"""The corporate-action loader against real Postgres: the unique key the
database enforces never trips through repeated, overlapping and conflicting loads, and a key the
owner deleted stays deleted."""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from hat_mcp_support import security
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session

from app.hat.data import corporate_actions as ca
from app.hat.data import instruments as inst
from app.hat.models import HatCorporateAction, HatInstrument, HatMarket

pytestmark = pytest.mark.postgres


def test_corporate_action_load_on_postgres(conn: Connection) -> None:
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
    inst.upsert_instruments(
        session, market.id, [security("INE000000771", "EQ", "SPLT")], today=date(2026, 1, 5)
    )
    iid = session.scalar(select(HatInstrument.id).where(HatInstrument.isin == "INE000000771"))
    ex = date(2018, 6, 14)
    mcp = ca.ActionRecord(iid, ex, "split", Decimal("0.2"), "mcp text", "mcp")
    archive = ca.ActionRecord(iid, ex, "split", Decimal("0.5"), "archive text", "archive")
    first = ca.load_actions(session, [mcp, mcp, archive])
    assert (first.created, first.unchanged, len(first.conflicts)) == (1, 1, 1)
    again = ca.load_actions(session, [mcp, archive])
    assert (again.created, again.unchanged, len(again.conflicts)) == (0, 1, 1)
    rows = session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.instrument_id == iid)
    ).all()
    assert [(r.adjustment_factor, r.source) for r in rows] == [(Decimal("0.2"), "mcp")]
    assert session.scalar(select(func.count()).select_from(HatCorporateAction)) >= 1
    # the owner deletes the key: no later load re-creates it (D1)
    fix = ca.Correction("INE000000771", ex, "split", None, note="wrong split")
    ca.correct_action(session, fix, chat_id=1, user_id=uuid.uuid4(), now=datetime.now(UTC))
    after = ca.load_actions(session, [mcp, archive])
    assert (after.created, after.held, len(after.conflicts)) == (0, 2, 0)
    gone = select(HatCorporateAction).where(HatCorporateAction.instrument_id == iid)
    assert session.scalars(gone).all() == []
    session.rollback()
