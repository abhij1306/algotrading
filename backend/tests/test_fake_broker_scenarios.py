"""LIVE place outcomes the shared FakeBroker scripts: an open circuit breaker rejects
without sending, and a lost answer is settled once from the orderbook by its order tag."""

from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.live_order import LiveOrder
from app.services import order_execution_service as oes

from fakes.broker import FakeBroker, accepted, unconfirmed

CLIENT_ORDER_ID = "7d4e2b19-3c5a-4f6e-8b7d-9a0c1e2f3b4d"
ORDER_TAG = "ST7D4E2B193C5A4F6E8B"


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    db_session.query(LiveOrder).delete()
    db_session.commit()


@pytest.fixture
def service(fake_broker: FakeBroker, monkeypatch: pytest.MonkeyPatch) -> oes.OrderExecutionService:
    svc = oes.OrderExecutionService()
    svc._broker = fake_broker
    monkeypatch.setattr(svc, "_run_live_risk_checks", lambda params, db: (None, []))
    return svc


def live_params() -> dict[str, Any]:
    return {
        "symbol": "SBIN",
        "fyers_symbol": "NSE:SBIN-EQ",
        "side": "BUY",
        "quantity": 2,
        "type": "LIMIT",
        "product": "INTRADAY",
        "price": 810.0,
        "user_id": "u-fake",
        "client_order_id": CLIENT_ORDER_ID,
    }


def test_open_circuit_rejects_the_live_order(
    orders: Session, service: oes.OrderExecutionService, fake_broker: FakeBroker
) -> None:
    fake_broker.circuit_open = True

    result = service.place_order(live_params(), orders, mode="LIVE")

    assert result == {
        "status": "REJECTED",
        "message": "Broker rejected the order: Broker unavailable; order not sent",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }
    assert orders.query(LiveOrder).one().status == "REJECTED"
    assert fake_broker.count("find_order_by_tag") == 0


def test_lost_answer_is_confirmed_from_the_orderbook(
    orders: Session, service: oes.OrderExecutionService, fake_broker: FakeBroker
) -> None:
    fake_broker.script("place_order", unconfirmed(), accepted("B-2"))
    fake_broker.tagged_order = {"order_id": "B-1", "status": "SUBMITTED"}

    result = service.place_order(live_params(), orders, mode="LIVE")

    assert result == {
        "status": "SUBMITTED",
        "order_id": "B-1",
        "message": "Confirmed from the broker orderbook",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }
    assert fake_broker.calls_named("find_order_by_tag") == [ORDER_TAG]
    assert fake_broker.count("place_order") == 1
