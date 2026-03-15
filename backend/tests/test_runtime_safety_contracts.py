from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.risk_manager import risk_manager


def test_trading_order_uses_request_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_place_order(
        params: dict[str, Any], db: Session, mode: str | None = None
    ) -> dict[str, str]:
        captured["mode"] = mode
        captured["params"] = params
        return {"status": "SUBMITTED", "order_id": "ORD-TEST"}

    monkeypatch.setattr(
        "app.routers.trading.order_execution_service.place_order",
        fake_place_order,
    )

    response = client.post(
        "/api/trading/order?x_user_id=test_user",
        json={
            "mode": "LIVE",
            "symbol": "SBIN",
            "side": "BUY",
            "quantity": 1,
            "product": "INTRADAY",
            "type": "MARKET",
            "price": 0,
            "trigger_price": 0,
            "instrument_type": "EQ",
        },
    )

    assert response.status_code == 200
    assert captured["mode"] == "LIVE"
    assert captured["params"]["mode"] == "LIVE"


def test_universe_historical_mode_is_explicitly_blocked(client: TestClient) -> None:
    response = client.get(
        "/api/universe/constituents/NIFTY50?mode=historical&target_date=2025-01-01"
    )

    assert response.status_code == 501
    assert "Historical universe lookup is disabled" in response.json()["detail"]


def test_risk_checks_do_not_close_caller_owned_session(db_session: Session) -> None:
    close_spy = Mock(wraps=db_session.close)
    db_session.close = close_spy

    result = risk_manager._check_order_frequency(db_session)

    assert result.code == "ORDER_FREQUENCY_OK"
    close_spy.assert_not_called()
