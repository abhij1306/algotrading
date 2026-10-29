"""The financial-results store against real Postgres: rows are never deleted and their figures
never updated (only `public_at` may move later), and `results_as_of` returns a revision only from
the time it became public."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
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
    iid: uuid.UUID,
    version: int,
    public_at: datetime,
    revenue: str,
    supersedes_id: uuid.UUID | None = None,
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
        supersedes_id=supersedes_id,
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

    early = datetime(2026, 1, 1, tzinfo=UTC)
    for statement in (
        update(HatFinancialResult).values(revenue=Decimal(1)),
        update(HatFinancialResult).values(public_at=early, public_at_source="xbrl"),
        delete(HatFinancialResult),
    ):
        nested = session.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"), nested:
            session.execute(statement)
    # only the timing moves: later, or for a first version earlier onto an announcement's
    # dissemination; a revision only later
    nested = session.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"), nested:
        session.execute(
            update(HatFinancialResult)
            .where(HatFinancialResult.id == revision.id)
            .values(public_at=datetime(2026, 2, 20, tzinfo=UTC), public_at_source="announcement")
        )
    later = datetime(2026, 1, 20, 10, 5, tzinfo=UTC)
    first = update(HatFinancialResult).where(HatFinancialResult.id == original.id)
    session.execute(first.values(public_at=later))
    moved = results_as_of(session, iid, datetime(2026, 1, 20, 10, 1, tzinfo=UTC))
    assert moved == []
    session.execute(first.values(public_at=datetime(2026, 1, 20, 10, 2, tzinfo=UTC)))
    # never earlier than the quarter's end, nor by more than 120 days, nor with an estimate flag
    # that does not match its source, nor a new XBRL estimate flag without a later XBRL time
    for statement in (
        first.values(public_at=datetime(2025, 12, 30, tzinfo=UTC)),
        first.values(public_at=later, public_at_estimated=True),
        first.values(public_at=datetime(2026, 7, 1, tzinfo=UTC), public_at_source="deadline"),
        first.values(xbrl_at_estimated=True),
    ):
        nested = session.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"), nested:
            session.execute(statement)
    late = datetime(2026, 7, 1, tzinfo=UTC)
    session.execute(
        first.values(public_at=late, public_at_source="deadline", public_at_estimated=True)
    )
    nested = session.begin_nested()
    with pytest.raises(DBAPIError, match="append-only"), nested:  # 121 days earlier
        session.execute(
            first.values(
                public_at=late - timedelta(days=121),
                public_at_source="announcement",
                public_at_estimated=False,
            )
        )
