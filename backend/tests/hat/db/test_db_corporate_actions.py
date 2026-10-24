"""The corporate-action loader against real Postgres: the unique key the
database enforces never trips through repeated, overlapping and conflicting loads, and a key the
owner deleted stays deleted."""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from hat_mcp_support import security
from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.hat.data import corporate_actions as ca
from app.hat.data import instruments as inst
from app.hat.data.ca_corrections import add_action, apply_list, parse_list
from app.hat.models import (
    HatCorporateAction,
    HatCorporateActionVersion,
    HatInstrument,
    HatMarket,
)

pytestmark = pytest.mark.postgres


def _instrument(session: Session, isin: str, symbol: str) -> uuid.UUID:
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
        session, market.id, [security(isin, "EQ", symbol)], today=date(2026, 1, 5)
    )
    iid = session.scalar(select(HatInstrument.id).where(HatInstrument.isin == isin))
    assert iid is not None
    return iid


def test_corporate_action_load_on_postgres(conn: Connection) -> None:
    session = Session(bind=conn)
    iid = _instrument(session, "INE000000771", "SPLT")
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
    # CA6: each write appended its version, the repeated conflict once, the delete too
    versions = session.execute(
        select(
            HatCorporateActionVersion.seq,
            HatCorporateActionVersion.status,
            HatCorporateActionVersion.factor,
        )
        .where(HatCorporateActionVersion.instrument_id == iid)
        .order_by(HatCorporateActionVersion.seq)
    ).all()
    assert [tuple(v) for v in versions] == [
        (1, "accepted", Decimal("0.2")),
        (2, "conflict", Decimal("0.5")),
        (3, "deleted", Decimal("0.2")),
    ]
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(update(HatCorporateActionVersion).values(text="x"))
    session.rollback()


def test_a_moved_demerger_keeps_its_text_and_flag(conn: Connection) -> None:
    """F1: `correct-actions move` carries the row's own text and demerger flag to the new date;
    the note goes to the journal and the version only."""
    session = Session(bind=conn)
    iid = _instrument(session, "INE000000772", "DMRG")
    owner = {"chat_id": 1, "user_id": uuid.uuid4(), "now": datetime.now(UTC)}
    header = "op\tisin\tex_date\ttype\tfactor\tnew_ex_date\tnote\n"
    added = "add\tINE000000772\t2024-09-20\tdemerger\t0.7108\t\tDemerger 1:1 X [market gap]\n"
    moved = "move\tINE000000772\t2024-09-20\tother\t\t2024-09-23\tex-date per bars\n"
    for line in (added, moved):
        apply_list(session, parse_list(header + line), apply=True, **owner)
    row = session.scalars(
        select(HatCorporateAction).where(HatCorporateAction.instrument_id == iid)
    ).one()
    assert (row.ex_date, row.demerger, row.raw_text) == (
        date(2024, 9, 23),
        True,
        "Demerger 1:1 X [market gap]",
    )
    version = session.scalars(
        select(HatCorporateActionVersion.text).where(
            HatCorporateActionVersion.instrument_id == iid,
            HatCorporateActionVersion.ex_date == date(2024, 9, 23),
        )
    ).one()
    assert version == "ex-date per bars"
    with pytest.raises(ca.ActionCorrectionError, match="bad_action_type"):
        add_action(
            session,
            isin="INE000000772",
            ex_date=date(2024, 9, 30),
            action_type="split",
            factor=Decimal("0.5"),
            note="n",
            demerger=True,
            **owner,
        )
