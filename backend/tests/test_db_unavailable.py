"""A down database answers 503 DATABASE_UNAVAILABLE, fails trading closed, leaks no driver
text and logs one line instead of a traceback per request."""

import asyncio
import logging
import sqlite3
from collections.abc import Generator
from typing import Any, NoReturn

import psycopg2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app import main as app_main
from app.database import get_db
from app.exceptions import DatabaseUnavailableError
from app.main import app
from app.models.live_order import LiveOrder
from app.routers import trading
from app.services import fyers_client
from app.services.history_service import daily_history_service
from app.services.order_execution_service import OrderExecutionService
from app.utils import market_hours
from app.utils.db_errors import is_db_unavailable
from app.utils.logger import DatabaseUnavailableLogFilter

SECRET = "hunter2-secret"
DOWN_BODY = {"error": {"code": "DATABASE_UNAVAILABLE", "message": "Database unavailable"}}
DOWN_DB_URL = f"postgresql://postgres:{SECRET}@db.invalid:5432/algotrading"


def _refuse_connection() -> NoReturn:
    raise psycopg2.OperationalError(
        f'connection to server at "db.invalid", port 5432 failed: Connection refused {SECRET}'
    )


def _connection_refused() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(f"connection refused {SECRET}"))


@pytest.fixture
def down_engine() -> Generator[Engine, None, None]:
    """A real Postgres engine whose every connect attempt is refused, like a stopped server."""
    engine = create_engine(DOWN_DB_URL, creator=_refuse_connection)
    yield engine
    engine.dispose()


@pytest.fixture
def down_client(down_engine: Engine) -> Generator[TestClient, None, None]:
    """Request-scoped sessions point at the down database. The lifespan is not run, and the
    default raise_server_exceptions=True makes the test fail if an error reaches Starlette's
    ServerErrorMiddleware instead of being answered by a specific exception handler."""
    down_session = sessionmaker(bind=down_engine, autoflush=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = down_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class RecordingBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("place_order", order))
        return {"status": "SUBMITTED", "order_id": "B-1", "message": "ok"}


def test_backtest_status_with_db_down_returns_503_envelope(
    down_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_down() -> NoReturn:
        raise _connection_refused()

    monkeypatch.setattr(daily_history_service, "get_index_coverage_map", raise_down)

    response = down_client.get("/api/backtest/status")

    assert response.status_code == 503
    assert response.json() == DOWN_BODY
    assert SECRET not in response.text


def test_genuine_sql_error_stays_a_500(
    down_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_bug() -> NoReturn:
        raise ProgrammingError("SELECT nope", {}, Exception(f"column nope {SECRET}"))

    monkeypatch.setattr(daily_history_service, "get_index_coverage_map", raise_bug)

    response = down_client.get("/api/backtest/status")

    assert response.status_code == 500
    assert response.json() == {"error": {"code": "INTERNAL_ERROR", "message": "Server error"}}
    assert SECRET not in response.text


def test_paper_order_with_db_down_returns_503_and_leaks_nothing(
    down_client: TestClient,
) -> None:
    response = down_client.post(
        "/api/trading/order",
        headers={"x-user-id": "u1"},
        json={
            "symbol": "SBIN",
            "side": "BUY",
            "quantity": 1,
            "type": "MARKET",
            "product": "INTRADAY",
            "mode": "PAPER",
        },
    )

    assert response.status_code == 503
    assert response.json() == DOWN_BODY
    assert SECRET not in response.text
    assert "psycopg2" not in response.text


def test_live_order_with_db_down_fails_closed_before_the_broker(
    down_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = RecordingBroker()
    monkeypatch.setattr(trading.order_execution_service, "_broker", broker)
    monkeypatch.setattr(market_hours, "is_market_hours", lambda: True)

    response = down_client.post(
        "/api/trading/order",
        headers={"x-user-id": "u1"},
        json={
            "symbol": "SBIN",
            "side": "BUY",
            "quantity": 1,
            "type": "MARKET",
            "product": "INTRADAY",
            "mode": "LIVE",
            "is_live_confirmation_ack": True,
        },
    )

    assert response.status_code == 503
    assert response.json() == DOWN_BODY
    assert broker.calls == []


def test_trading_orders_with_db_down_returns_503_not_an_empty_list(
    down_client: TestClient,
) -> None:
    response = down_client.get("/api/trading/orders")

    assert response.status_code == 503
    assert response.json() == DOWN_BODY


def test_health_with_db_down_reports_down_without_driver_text(
    down_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: None)

    response = down_client.get("/api/system/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "DOWN",
        "postgresql": "DOWN",
        "fyers": "DOWN",
        "last_update": None,
        "total_symbols": 0,
        "recommendations": [
            "PostgreSQL database is unavailable. Start it: net start postgresql-x64-18",
            "Fyers API unavailable. Screener will use cached data only.",
        ],
        **DOWN_BODY,
    }
    assert SECRET not in response.text


def test_screener_preflight_with_db_down_returns_503(
    down_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: None)

    response = down_client.get("/api/system/pre-flight/screener")

    assert response.status_code == 503
    assert response.json() == DOWN_BODY


def test_startup_logs_one_line_without_password(
    down_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(database, "engine", down_engine)

    with caplog.at_level(logging.INFO), pytest.raises(DatabaseUnavailableError):
        asyncio.run(app_main._step_validate_database())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "[ERROR] Database unreachable at db.invalid:5432/algotrading "
        "(start Postgres: net start postgresql-x64-18)"
    ]
    assert errors[0].exc_info is None
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_connection_refused(), True),
        (OperationalError("x", {}, sqlite3.OperationalError("database is locked")), True),
        (OperationalError("x", {}, sqlite3.OperationalError("no such table: t")), False),
        (ProgrammingError("x", {}, Exception("syntax error")), False),
        (IntegrityError("x", {}, Exception("duplicate key")), False),
        (RuntimeError("boom"), False),
    ],
)
def test_is_db_unavailable_matches_connection_loss_only(exc: BaseException, expected: bool) -> None:
    assert is_db_unavailable(exc) is expected


def test_is_db_unavailable_follows_the_cause_chain() -> None:
    try:
        try:
            raise _connection_refused()
        except OperationalError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_db_unavailable(outer) is True


def test_log_filter_drops_traceback_and_throttles_per_site() -> None:
    lines: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(DatabaseUnavailableLogFilter())
    log = logging.getLogger("test_db_unavailable.filter")
    log.propagate = False
    log.addHandler(handler)
    try:
        for _ in range(3):
            try:
                raise _connection_refused()
            except OperationalError as exc:
                log.exception("Failed to load: %s", exc)
        try:
            raise ValueError("unrelated")
        except ValueError:
            log.exception("Other failure")
    finally:
        log.removeHandler(handler)

    assert lines[0] == (
        "ERROR Failed to load: (builtins.Exception) connection refused hunter2-secret "
        "[database unavailable; traceback suppressed]"
    )
    assert len(lines) == 2
    assert lines[1].startswith("ERROR Other failure\nTraceback (most recent call last):")


class RaisingBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(order)
        raise RuntimeError(f"fyers socket reset {SECRET}")


def test_live_submit_exception_returns_fixed_message_and_stores_no_broker_text(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = OrderExecutionService()
    broker = RaisingBroker()
    service._broker = broker
    monkeypatch.setattr(service, "_run_live_risk_checks", lambda params, db: None)

    result = service.place_order(
        {"symbol": "SBIN", "side": "BUY", "quantity": 2, "type": "MARKET", "user_id": "u-leak"},
        db_session,
        mode="LIVE",
    )

    assert result == {"status": "ERROR", "message": "Broker order submission failed"}
    assert len(broker.calls) == 1
    stored = db_session.query(LiveOrder).filter(LiveOrder.user_id == "u-leak").one()
    assert (stored.status, stored.reject_reason) == ("ERROR", "Broker order submission failed")
    db_session.delete(stored)
    db_session.commit()


def test_db_lost_after_broker_accepts_raises_and_logs_the_unrecorded_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = OrderExecutionService()
    broker = RecordingBroker()
    service._broker = broker
    monkeypatch.setattr(service, "_run_live_risk_checks", lambda params, db: None)

    def lost_commit() -> NoReturn:
        raise _connection_refused()

    monkeypatch.setattr(db_session, "commit", lost_commit)

    with pytest.raises(OperationalError):
        service.place_order(
            {"symbol": "SBIN", "side": "BUY", "quantity": 1, "type": "MARKET", "user_id": "u-lost"},
            db_session,
            mode="LIVE",
        )

    assert len(broker.calls) == 1
    assert "LIVE order B-1 accepted by broker but not recorded: database unavailable" in [
        r.getMessage() for r in caplog.records
    ]
    assert db_session.query(LiveOrder).filter(LiveOrder.user_id == "u-lost").count() == 0
