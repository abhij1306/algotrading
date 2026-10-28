"""The financial-results store against real Postgres: rows are never updated or deleted, and
`results_as_of` returns a revision only from the time it became public."""

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from hat_mcp_support import security
from sqlalchemy import Connection, delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.hat.data import instruments as inst
from app.hat.data.results.derive import results_as_of
from app.hat.models import HatFinancialResult, HatInstrument, HatMarket

pytestmark = pytest.mark.postgres


def _instrument(session: Session) -> uuid.UUID:
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
        session, market.id, [security("INE000000881", "EQ", "FINR")], today=date(2026, 1, 5)
    )
    iid = session.scalar(select(HatInstrument.id).where(HatInstrument.isin == "INE000000881"))
    assert iid is not None
    return iid


def _row(
    iid: uuid.UUID, version: int, public_at: datetime, revenue: str, **kw
) -> HatFinancialResult:
    return HatFinancialResult(
        instrument_id=iid,
        symbol="FINR",
        isin="INE000000881",
        period_end=date(2025, 12, 31),
        basis="consolidated",
        format="reg33_indas",
        source="nse",
        revenue=Decimal(revenue),
        public_at=public_at,
        public_at_source="announcement" if version == 1 else "xbrl",
        version=version,
        listing="reg33",
        listing_seq=str(version),
        source_url=f"https://example.invalid/{version}.xml",
        sha256=str(version) * 64,
        raw_path=f"blobs/{version}.gz",
        **kw,
    )


def test_results_are_append_only_and_point_in_time(conn: Connection) -> None:
    session = Session(bind=conn)
    iid = _instrument(session)
    original = _row(iid, 1, datetime(2026, 1, 20, 10, 0, tzinfo=UTC), "100")
    session.add(original)
    session.flush()
    revision = _row(
        iid, 2, datetime(2026, 3, 2, 10, 0, tzinfo=UTC), "110", supersedes_id=original.id
    )
    session.add(revision)
    session.flush()

    before = results_as_of(session, iid, datetime(2026, 1, 20, 9, 59, tzinfo=UTC))
    between = results_as_of(session, iid, datetime(2026, 2, 1, tzinfo=UTC))
    after = results_as_of(session, iid, datetime(2026, 3, 2, 10, 0, tzinfo=UTC))
    assert before == []
    assert [(r.version, r.revenue) for r in between] == [(1, Decimal("100"))]
    assert [(r.version, r.revenue) for r in after] == [(2, Decimal("110"))]

    for statement in (
        update(HatFinancialResult).values(revenue=Decimal(1)),
        delete(HatFinancialResult),
    ):
        nested = session.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"), nested:
            session.execute(statement)
