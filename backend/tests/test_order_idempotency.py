"""Order idempotency: a client_order_id places at most one order. LIVE rows are committed as
PENDING_SUBMIT before the broker is called; a retry or a concurrent duplicate returns the
existing order and never reaches the broker again."""

import uuid
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, MetaData, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import ensure_live_order_schema, get_db
from app.main import app
from app.models.live_order import CLIENT_ORDER_ID_INDEX, LiveOrder
from app.services import order_execution_service as oes

from fakes.broker import FakeBroker

CLIENT_ORDER_ID = "3f2b8c1e-9d4a-4b6e-8f10-2a7c5e9d1b34"
OTHER_CLIENT_ORDER_ID = "9a1d7c55-2e3f-4b8a-a6c4-71e0d2f8b903"
DUPLICATE_MESSAGE = "Duplicate client_order_id: returning the existing order"


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    db_session.query(LiveOrder).delete()
    db_session.commit()


@pytest.fixture
def broker(fake_broker: FakeBroker) -> FakeBroker:
    """Accepts every place_order as B-<n> with the message "ok"."""
    fake_broker.place_message = "ok"
    return fake_broker


@pytest.fixture
def service(broker: FakeBroker, monkeypatch: pytest.MonkeyPatch) -> oes.OrderExecutionService:
    svc = oes.OrderExecutionService()
    svc._broker = broker
    monkeypatch.setattr(svc, "_run_live_risk_checks", lambda params, db: (None, []))
    # No market-data tick: a PAPER LIMIT order fills against its own limit price.
    monkeypatch.setattr(oes, "latest_tick_price", lambda db_symbol: 0.0)
    return svc


def order_params(**overrides: object) -> dict[str, Any]:
    return {
        "symbol": "SBIN",
        "fyers_symbol": "NSE:SBIN-EQ",
        "side": "BUY",
        "quantity": 2,
        "type": "LIMIT",
        "product": "INTRADAY",
        "price": 810.0,
        "user_id": "u-idem",
        "client_order_id": CLIENT_ORDER_ID,
        **overrides,
    }


def stored_rows(db: Session) -> list[tuple[str, str, str | None, int]]:
    return [
        (row.id, row.status, row.client_order_id, row.is_paper)
        for row in db.query(LiveOrder).order_by(LiveOrder.created_at).all()
    ]


# --- LIVE ---------------------------------------------------------------------


def test_live_retry_returns_the_existing_order_without_a_second_broker_call(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    first = service.place_order(order_params(), orders, mode="LIVE")
    retry = service.place_order(order_params(), orders, mode="LIVE")

    assert first == {
        "status": "SUBMITTED",
        "order_id": "B-1",
        "message": "ok",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }
    assert retry == {
        "order_id": "B-1",
        "status": "SUBMITTED",
        "message": DUPLICATE_MESSAGE,
        "average_price": 0.0,
        "filled_qty": 0,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
    }
    assert broker.count("place_order") == 1
    assert broker.calls_named("place_order")[0]["order_tag"] == "ST3F2B8C1E9D4A4B6E8F"
    assert stored_rows(orders) == [("B-1", "SUBMITTED", CLIENT_ORDER_ID, 0)]


def test_live_retry_is_deduplicated_even_when_the_risk_check_would_now_fail(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.place_order(order_params(), orders, mode="LIVE")

    def daily_limit_reached(
        params: dict[str, Any], db: Session
    ) -> tuple[dict[str, Any], list[str]]:
        return {"status": "ERROR", "message": "Risk Check Failed: limit", "code": "X"}, []

    monkeypatch.setattr(service, "_run_live_risk_checks", daily_limit_reached)

    retry = service.place_order(order_params(), orders, mode="LIVE")

    assert (retry["status"], retry["duplicate"], retry["order_id"]) == ("SUBMITTED", True, "B-1")
    assert broker.count("place_order") == 1


def test_new_client_order_id_places_a_new_order(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    service.place_order(order_params(), orders, mode="LIVE")
    second = service.place_order(
        order_params(client_order_id=OTHER_CLIENT_ORDER_ID), orders, mode="LIVE"
    )

    assert (second["order_id"], second["duplicate"]) == ("B-2", False)
    assert [call["order_tag"] for call in broker.calls_named("place_order")] == [
        "ST3F2B8C1E9D4A4B6E8F",
        "ST9A1D7C552E3F4B8AA6",
    ]


@pytest.mark.parametrize(
    ("client_order_id", "message"),
    [
        (None, "LIVE order requires a client_order_id (UUID v4)"),
        ("", "LIVE order requires a client_order_id (UUID v4)"),
        ("not-a-uuid", "client_order_id must be a UUID v4"),
        (str(uuid.UUID(int=1, version=1)), "client_order_id must be a UUID v4"),
    ],
)
def test_live_order_without_a_valid_client_order_id_never_reaches_broker(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    client_order_id: str | None,
    message: str,
) -> None:
    result = service.place_order(order_params(client_order_id=client_order_id), orders, mode="LIVE")

    assert result["status"] == "ERROR"
    assert result["message"] == message
    assert broker.calls == []
    assert stored_rows(orders) == []


def test_pending_row_is_committed_before_the_broker_is_called(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str | None]] = []

    def place_after_checking_the_row(order: dict[str, Any]) -> dict[str, Any]:
        # A separate connection only sees committed data.
        with test_engine.connect() as connection:
            row = connection.execute(text("SELECT status, client_order_id FROM live_orders")).one()
        seen.append((row.status, row.client_order_id))
        return {"status": "SUBMITTED", "order_id": "B-7", "message": "ok"}

    monkeypatch.setattr(broker, "place_order", place_after_checking_the_row)

    service.place_order(order_params(), orders, mode="LIVE")

    assert seen == [("PENDING_SUBMIT", CLIENT_ORDER_ID)]
    assert stored_rows(orders) == [("B-7", "SUBMITTED", CLIENT_ORDER_ID, 0)]


def _connection_refused() -> OperationalError:
    return OperationalError("COMMIT", {}, Exception("connection refused"))


def test_db_down_on_the_pending_commit_raises_and_never_calls_the_broker(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lost_commit() -> NoReturn:
        raise _connection_refused()

    monkeypatch.setattr(orders, "commit", lost_commit)

    params = order_params()
    with pytest.raises(OperationalError):
        service.place_order(params, orders, mode="LIVE")

    assert broker.calls == []
    monkeypatch.undo()
    assert stored_rows(orders) == []


def test_other_pending_commit_failure_returns_fixed_error_and_never_calls_the_broker(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_commit() -> NoReturn:
        raise RuntimeError("secret driver text")

    monkeypatch.setattr(orders, "commit", broken_commit)

    result = service.place_order(order_params(), orders, mode="LIVE")

    assert result == {"status": "ERROR", "message": "Order placement failed"}
    assert broker.calls == []


def test_db_lost_after_broker_accepts_keeps_pending_row_so_a_retry_does_not_reorder(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real_commit = orders.commit
    commits: list[int] = []

    def commit_then_lose_connection() -> None:
        commits.append(1)
        if len(commits) > 1:
            raise _connection_refused()
        real_commit()

    monkeypatch.setattr(orders, "commit", commit_then_lose_connection)

    params = order_params()
    with pytest.raises(OperationalError):
        service.place_order(params, orders, mode="LIVE")
    monkeypatch.undo()

    assert (
        f"LIVE order B-1 (client_order_id {CLIENT_ORDER_ID}) accepted by broker but its "
        "status was not saved; the row stays PENDING_SUBMIT"
    ) in [r.getMessage() for r in caplog.records]
    pending_id = orders.query(LiveOrder).one().internal_id
    assert stored_rows(orders) == [(pending_id, "PENDING_SUBMIT", CLIENT_ORDER_ID, 0)]

    retry = service.place_order(order_params(), orders, mode="LIVE")

    assert (retry["order_id"], retry["status"], retry["duplicate"]) == (
        pending_id,
        "PENDING_SUBMIT",
        True,
    )
    assert broker.count("place_order") == 1


# --- Concurrent duplicates ------------------------------------------------------


def add_existing_order(db: Session, *, is_paper: int, status: str) -> None:
    db.add(
        LiveOrder(
            id="B-EXISTING",
            internal_id="ORD-EXISTING",
            client_order_id=CLIENT_ORDER_ID,
            user_id="u-idem",
            symbol="SBIN",
            fyers_symbol="NSE:SBIN-EQ",
            side="BUY",
            quantity=2,
            order_type="LIMIT",
            product_type="INTRADAY",
            price=810.0,
            status=status,
            filled_qty=0,
            average_price=0.0,
            is_paper=is_paper,
        )
    )
    db.commit()


def lookup_misses_once(
    real: Callable[[str | None, Session], LiveOrder | None],
) -> Callable[[str | None, Session], LiveOrder | None]:
    """The first lookup misses, as when two identical requests check at the same time."""
    calls: list[int] = []

    def lookup(client_order_id: str | None, db: Session) -> LiveOrder | None:
        calls.append(1)
        return None if len(calls) == 1 else real(client_order_id, db)

    return lookup


@pytest.mark.parametrize(("mode", "is_paper"), [("LIVE", 0), ("PAPER", 1)])
def test_concurrent_duplicate_loses_the_insert_race_and_returns_the_winner(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    is_paper: int,
) -> None:
    add_existing_order(orders, is_paper=is_paper, status="PENDING_SUBMIT")
    monkeypatch.setattr(
        service, "_find_by_client_order_id", lookup_misses_once(service._find_by_client_order_id)
    )

    result = service.place_order(order_params(), orders, mode=mode)

    assert result == {
        "order_id": "B-EXISTING",
        "status": "PENDING_SUBMIT",
        "message": DUPLICATE_MESSAGE,
        "average_price": 0.0,
        "filled_qty": 0,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
    }
    assert broker.calls == []
    assert stored_rows(orders) == [("B-EXISTING", "PENDING_SUBMIT", CLIENT_ORDER_ID, is_paper)]


def test_integrity_error_without_a_matching_row_is_a_fixed_error(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unrelated_violation() -> NoReturn:
        raise IntegrityError("INSERT", {}, Exception("internal_id collision"))

    monkeypatch.setattr(orders, "commit", unrelated_violation)

    result = service.place_order(order_params(), orders, mode="LIVE")

    assert result == {"status": "ERROR", "message": "Order placement failed"}
    assert broker.calls == []


# --- PAPER ----------------------------------------------------------------------


def test_paper_retry_returns_the_existing_fill_and_never_touches_the_broker(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    first = service.place_order(order_params(), orders, mode="PAPER")
    retry = service.place_order(order_params(), orders, mode="PAPER")

    assert (first["status"], first["average_price"], first["duplicate"]) == (
        "FILLED",
        810.0,
        False,
    )
    assert retry == {
        "order_id": first["order_id"],
        "status": "FILLED",
        "message": DUPLICATE_MESSAGE,
        "average_price": 810.0,
        "filled_qty": 2,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
    }
    assert broker.calls == []
    assert stored_rows(orders) == [(first["order_id"], "FILLED", CLIENT_ORDER_ID, 1)]


def test_paper_order_without_client_order_id_is_still_accepted(
    orders: Session, service: oes.OrderExecutionService
) -> None:
    result = service.place_order(order_params(client_order_id=None), orders, mode="PAPER")

    assert (result["status"], result["client_order_id"], result["duplicate"]) == (
        "FILLED",
        None,
        False,
    )


# --- HTTP contract --------------------------------------------------------------


@pytest.fixture
def api(orders: Session) -> Generator[TestClient, None, None]:
    def override_get_db() -> Generator[Session, None, None]:
        yield orders

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


ORDER_BODY = {
    "symbol": "SBIN",
    "side": "BUY",
    "quantity": 2,
    "type": "LIMIT",
    "product": "INTRADAY",
    "price": 810.0,
    "mode": "LIVE",
    "is_live_confirmation_ack": True,
}


@pytest.mark.parametrize(
    "extra",
    [{}, {"client_order_id": None}, {"client_order_id": "abc"}],
)
def test_live_order_api_requires_a_uuid4_client_order_id(
    api: TestClient,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, Any],
) -> None:

    response = api.post(
        "/api/trading/order", headers={"x-user-id": "u1"}, json={**ORDER_BODY, **extra}
    )

    assert response.status_code == 422
    assert broker.calls == []


def test_duplicate_of_a_failed_order_answers_200_with_its_state(
    api: TestClient,
    orders: Session,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_existing_order(orders, is_paper=0, status="ERROR")

    response = api.post(
        "/api/trading/order",
        headers={"x-user-id": "u1"},
        json={**ORDER_BODY, "client_order_id": CLIENT_ORDER_ID.upper()},
    )

    assert response.status_code == 200
    assert response.json() == {
        "order_id": "B-EXISTING",
        "status": "ERROR",
        "message": DUPLICATE_MESSAGE,
        "average_price": 0.0,
        "filled_qty": 0,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
    }
    assert broker.calls == []


# --- Schema backfill ------------------------------------------------------------


def test_schema_backfill_adds_the_column_and_unique_index_once(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    legacy = Table(
        "live_orders",
        MetaData(),
        *(
            column._copy()
            for column in LiveOrder.__table__.columns
            if column.name != "client_order_id"
        ),
    )
    legacy.metadata.create_all(engine)

    ensure_live_order_schema(bind=engine)
    ensure_live_order_schema(bind=engine)

    inspector = inspect(engine)
    assert "client_order_id" in {c["name"] for c in inspector.get_columns("live_orders")}
    assert [
        (index["name"], index["column_names"], bool(index["unique"]))
        for index in inspector.get_indexes("live_orders")
        if index["name"] == CLIENT_ORDER_ID_INDEX
    ] == [(CLIENT_ORDER_ID_INDEX, ["client_order_id"], True)]

    def insert(order_id: str) -> None:
        with engine.begin() as connection:
            connection.execute(
                LiveOrder.__table__.insert(),
                {
                    "id": order_id,
                    "internal_id": order_id,
                    "client_order_id": CLIENT_ORDER_ID,
                    "symbol": "SBIN",
                    "side": "BUY",
                    "quantity": 1,
                    "order_type": "MARKET",
                    "product_type": "INTRADAY",
                },
            )

    insert("A")
    with pytest.raises(IntegrityError):
        insert("B")
    engine.dispose()
