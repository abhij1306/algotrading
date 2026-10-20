"""Trading paths open their sessions through db_session_scope() or get_db(), never a bare
SessionLocal(): every session they own is closed, and rolled back when the work fails.

Each test points `app.database.SessionLocal` at the test database with a session class
that records commit, rollback and close, so the assertions read what happened to the
session the code under test opened.
"""

from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.main import app
from app.models.live_order import LiveOrder
from app.models.live_position import LIVE_ACCOUNT_USER_ID, LivePosition
from app.routers import terminal
from app.services.position_sync_service import position_sync_service
from app.services.risk_manager import RiskConfig, RiskManager, RiskStatus
from app.utils.cache import clear_cache

from fakes.broker import FakeBroker, broker_position

SBIN_ID = f"{LIVE_ACCOUNT_USER_ID}-NSE:SBIN-EQ-INTRADAY"


class RecordingSession(Session):
    """A real Session that logs commit, rollback and close in `events`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - Session's own signature
        super().__init__(*args, **kwargs)
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")
        super().commit()

    def rollback(self) -> None:
        self.events.append("rollback")
        super().rollback()

    def close(self) -> None:
        self.events.append("close")
        super().close()


@pytest.fixture
def opened(test_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> list[RecordingSession]:
    """Every session opened through app.database, in order."""
    sessions: list[RecordingSession] = []
    factory = sessionmaker(
        autocommit=False, autoflush=False, bind=test_engine, class_=RecordingSession
    )

    def open_session() -> RecordingSession:
        session = factory()
        sessions.append(session)
        return session

    monkeypatch.setattr(database, "SessionLocal", open_session)
    return sessions


@pytest.fixture
def rows(db_session: Session) -> Generator[Session, None, None]:
    """The test database with no orders or positions, before and after the test."""
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()
    clear_cache()
    yield db_session
    db_session.rollback()
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()
    clear_cache()


def store_sbin(db: Session, net_qty: int) -> None:
    db.add(
        LivePosition(
            id=SBIN_ID,
            user_id=LIVE_ACCOUNT_USER_ID,
            symbol="SBIN",
            fyers_symbol="NSE:SBIN-EQ",
            product_type="INTRADAY",
            side="LONG",
            net_qty=net_qty,
            net_avg=790.0,
            ltp=800.0,
        )
    )
    db.commit()


def stored_net_qty(db: Session) -> list[tuple[str, int]]:
    db.expire_all()
    return [(p.id, p.net_qty) for p in db.query(LivePosition).order_by(LivePosition.id)]


# --- position sync ---------------------------------------------------------------------


def test_a_sync_commits_its_rows_and_closes_the_session(
    rows: Session, opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    store_sbin(rows, 10)
    fake_broker.positions = [broker_position("NSE:SBIN-EQ", 12)]

    result = position_sync_service.sync_positions()

    assert (result["status"], result["updated"], result["synced"]) == ("success", 1, 0)
    assert [s.events for s in opened] == [["commit", "commit", "close"]]
    assert stored_net_qty(rows) == [(SBIN_ID, 12)]


def test_a_failed_sync_rolls_back_every_row_and_closes_the_session(
    rows: Session, opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    store_sbin(rows, 10)
    malformed = broker_position("NSE:INFY-EQ", 5)
    del malformed["pnl"]
    fake_broker.positions = [broker_position("NSE:SBIN-EQ", 12), malformed]

    result = position_sync_service.sync_positions()

    assert result == {"status": "error", "message": "Position sync failed"}
    assert [s.events for s in opened] == [["rollback", "close"]]
    assert stored_net_qty(rows) == [(SBIN_ID, 10)]


def test_reading_positions_closes_the_session(
    rows: Session, opened: list[RecordingSession]
) -> None:
    store_sbin(rows, 10)

    positions = position_sync_service.get_positions(LIVE_ACCOUNT_USER_ID)

    assert [(p["id"], p["net_qty"]) for p in positions] == [(SBIN_ID, 10)]
    assert [s.events for s in opened] == [["commit", "close"]]


# --- risk manager ----------------------------------------------------------------------


def manager(fake_broker: FakeBroker) -> RiskManager:
    risk = RiskManager(RiskConfig(max_orders_per_day=5))
    risk._broker = fake_broker  # type: ignore[assignment]
    return risk


@pytest.mark.usefixtures("rows")
def test_a_check_without_a_session_opens_and_closes_its_own(
    opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    result = manager(fake_broker)._check_order_frequency(None)

    assert (result.status, result.code) == (RiskStatus.PASS, "ORDER_FREQUENCY_OK")
    assert [s.events for s in opened] == [["commit", "close"]]


def test_a_check_given_a_session_leaves_it_to_its_owner(
    rows: Session, opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    given = RecordingSession(bind=rows.get_bind())

    result = manager(fake_broker)._check_order_frequency(given)

    assert (result.status, result.code) == (RiskStatus.PASS, "ORDER_FREQUENCY_OK")
    assert given.events == []
    assert opened == []
    given.close()


@pytest.mark.usefixtures("rows")
def test_a_failing_check_rolls_back_and_closes_its_own_session(
    opened: list[RecordingSession], fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_count(_db: Session) -> int:
        raise RuntimeError("count failed")

    monkeypatch.setattr(RiskManager, "_live_orders_today", staticmethod(broken_count))
    risk = manager(fake_broker)

    with pytest.raises(RuntimeError, match="count failed"):
        risk._check_order_frequency(None)

    assert [s.events for s in opened] == [["rollback", "close"]]


def test_the_exposure_summary_closes_its_session(
    rows: Session, opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    store_sbin(rows, 10)

    summary = manager(fake_broker).get_exposure_summary(LIVE_ACCOUNT_USER_ID)

    assert (summary["long_exposure"], summary["position_count"]) == (8000.0, 1)
    assert [s.events for s in opened] == [["commit", "close"]]


# --- terminal paper order (request session from get_db) --------------------------------


PAPER_ORDER = {"symbol": "SBIN", "side": "buy", "quantity": 3, "order_type": "market"}


def test_a_paper_order_is_stored_through_the_request_session(
    rows: Session, opened: list[RecordingSession], fake_broker: FakeBroker
) -> None:
    response = TestClient(app).post("/api/terminal/paper/order", json=PAPER_ORDER)

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["mode"]) == ("SUBMITTED", "PAPER")
    stored = [(o.symbol, o.side, o.quantity, o.is_paper) for o in rows.query(LiveOrder)]
    assert stored == [("SBIN", "BUY", 3, 1)]
    assert [s.events for s in opened] == [["commit", "close"]]
    assert fake_broker.calls == []


def test_a_failed_paper_order_rolls_back_and_closes_the_request_session(
    rows: Session,
    opened: list[RecordingSession],
    fake_broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_to_fyers(_symbol: str) -> str:
        raise RuntimeError("symbol lookup failed")

    monkeypatch.setattr(terminal.symbol_master, "to_fyers", broken_to_fyers)

    response = TestClient(app).post("/api/terminal/paper/order", json=PAPER_ORDER)

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to place paper order"
    assert rows.query(LiveOrder).count() == 0
    assert [s.events for s in opened] == [["rollback", "close"]]
    assert fake_broker.calls == []
