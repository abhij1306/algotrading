"""Live square-off error paths must not return broker or exception text (trading rule 10)."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from app.brokers.plugins import fyers as fyers_plugin
from app.routers import trading

SECRET = "token=abc123 internal broker detail"


class FakeBroker:
    """Rejects the DB symbol; the Fyers-symbol retry raises an exception with private text."""

    calls: list[str] = []

    def exit_position(self, symbol: str) -> dict[str, Any]:
        self.calls.append(symbol)
        if len(self.calls) == 1:
            return {"status": "REJECTED", "message": "Position not found"}
        raise RuntimeError(SECRET)


@pytest.fixture
def fake_broker(monkeypatch: pytest.MonkeyPatch) -> type[FakeBroker]:
    FakeBroker.calls = []
    monkeypatch.setattr(fyers_plugin, "FyersBroker", FakeBroker)
    return FakeBroker


def test_failed_fallback_returns_error_code_without_exception_text(
    fake_broker: type[FakeBroker], caplog: pytest.LogCaptureFixture
) -> None:
    response = trading._square_live_positions(
        trading.SquarePositionRequest(symbol="SBIN", mode="LIVE")
    )

    assert fake_broker.calls == ["SBIN", "NSE:SBIN-EQ"]
    assert response["status"] == "ERROR"
    assert response["message"] == "Position not found"
    assert response["details"]["error_code"] == trading.LIVE_SQUARE_FALLBACK_FAILED
    assert response["details"]["fyers_symbol"] == "NSE:SBIN-EQ"
    assert SECRET not in repr(response)
    assert "fallback_error" not in response["details"]
    # The failure is still logged server-side, sanitized and on one line.
    logged = [r.getMessage() for r in caplog.records if "fallback failed" in r.getMessage()]
    assert len(logged) == 1
    assert "RuntimeError" in logged[0]


def test_square_position_server_error_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDb:
        rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

    def explode(_req: trading.SquarePositionRequest) -> dict[str, Any]:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(trading, "_square_live_positions", explode)
    db = FakeDb()
    req = trading.SquarePositionRequest(symbol="SBIN", mode="LIVE")

    with pytest.raises(HTTPException) as raised:
        trading.square_position(req, db)

    assert raised.value.status_code == 500
    assert raised.value.detail == "Error squaring position"
    assert db.rolled_back is True


class FailingBroker:
    def get_funds(self) -> object:
        raise RuntimeError(SECRET)

    def get_tradebook(self) -> list[dict[str, Any]]:
        raise RuntimeError(SECRET)


@pytest.mark.parametrize(
    ("route", "detail"),
    [
        (trading.get_funds, "Error fetching funds"),
        (trading.get_tradebook, "Error fetching tradebook"),
    ],
)
def test_broker_route_errors_do_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch, route: Callable[[], object], detail: str
) -> None:
    monkeypatch.setattr(fyers_plugin, "FyersBroker", FailingBroker)

    with pytest.raises(HTTPException) as raised:
        route()

    assert raised.value.status_code == 500
    assert raised.value.detail == detail
