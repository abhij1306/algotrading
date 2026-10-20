"""Endpoints never send exception text to the client, and broker-backed reads fail closed.

Every fake failure carries MARKER; a response that contains it leaked the raw error.
"""

from collections.abc import Generator
from types import SimpleNamespace
from typing import NoReturn

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.brokers.plugins import fyers as fyers_plugin
from app.data_layer.exceptions import DataNotFoundError, DataSourceUnavailableError
from app.data_layer.provider import DataProvider
from app.main import app
from app.routers import auth, terminal, upload
from app.services.history_service import daily_history_service
from app.services.live_market_service import live_market
from app.services.risk_manager import RiskManager, RiskStatus
from app.utils.errors import handle_api_error, sanitize_error_message

MARKER = "leak-marker-7f3a password=hunter2"

BROKER_DOWN = {"error": {"code": "BROKER_UNAVAILABLE", "message": "Broker unavailable"}}
BROKER_NOT_CONNECTED = {"error": {"code": "BROKER_UNAVAILABLE", "message": "Broker not connected"}}


def _boom(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(MARKER)


@pytest.fixture
def api() -> Generator[TestClient, None, None]:
    """No lifespan; raise_server_exceptions=True fails the test if an error escapes to
    Starlette's ServerErrorMiddleware instead of a specific handler."""
    yield TestClient(app)


class FakeFyersSdk:
    def __init__(self, reply: object) -> None:
        self.reply = reply

    def positions(self) -> object:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def use_fyers(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    from app.services import fyers_client

    monkeypatch.setattr(fyers_client, "get_fyers_client", lambda: client)


def connected(reply: object) -> SimpleNamespace:
    return SimpleNamespace(validate_token=lambda: True, fyers=FakeFyersSdk(reply))


# --- /api/smart-trader/positions and /pnl: fail closed, no fake zeros -----------------


@pytest.mark.parametrize("path", ["/api/smart-trader/positions", "/api/smart-trader/pnl"])
@pytest.mark.parametrize(
    "client",
    [None, SimpleNamespace(validate_token=lambda: False, fyers=FakeFyersSdk({"s": "ok"}))],
)
def test_broker_reads_answer_503_when_not_connected(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, client: object
) -> None:
    use_fyers(monkeypatch, client)

    response = api.get(path)

    assert response.status_code == 503
    assert response.json() == BROKER_NOT_CONNECTED


@pytest.mark.parametrize("path", ["/api/smart-trader/positions", "/api/smart-trader/pnl"])
@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError(MARKER),
        {"s": "error", "message": MARKER},
        ["not", "a", "dict"],
        {"s": "ok", "netPositions": MARKER},
    ],
)
def test_broker_reads_answer_503_without_broker_text_when_broker_fails(
    api: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, reply: object
) -> None:
    use_fyers(monkeypatch, connected(reply))

    response = api.get(path)

    assert response.status_code == 503
    assert response.json() == BROKER_DOWN
    assert MARKER not in response.text


def test_positions_and_pnl_come_from_the_broker_reply(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [{"symbol": "NSE:SBIN-EQ", "pl": 10.1}, {"symbol": "NSE:TCS-EQ", "pl": 20.2}]
    use_fyers(monkeypatch, connected({"s": "ok", "netPositions": rows}))

    assert api.get("/api/smart-trader/positions").json() == {"positions": rows}
    assert api.get("/api/smart-trader/pnl").json() == {"total_pnl": 30.3}


def test_flat_book_is_a_real_zero(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fyers(monkeypatch, connected({"s": "ok", "netPositions": []}))

    assert api.get("/api/smart-trader/positions").json() == {"positions": []}
    assert api.get("/api/smart-trader/pnl").json() == {"total_pnl": 0.0}


# --- /api/strategies/regime: missing index data is a 503, not a 500 -------------------


def test_regime_answers_503_data_unavailable_when_index_history_missing(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daily_history_service, "load_index_history", lambda *_a, **_k: pd.DataFrame()
    )

    response = api.get("/api/strategies/regime")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "DATA_UNAVAILABLE",
            "message": "Market regime unavailable: NIFTY50 index history is missing",
        }
    }


# --- status endpoints keep their shape but carry a fixed error ------------------------


def test_websocket_status_error_is_fixed_text(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_market, "get_status", _boom)

    response = api.get("/api/websocket/status")

    assert response.status_code == 200
    assert response.json() == {"connected": False, "error": "WebSocket status unavailable"}


def test_fyers_status_error_is_fixed_text(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(auth._status_cache, "cached_at", 0)
    monkeypatch.setattr(auth.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(auth, "open", _boom, raising=False)

    response = api.get("/api/auth/fyers/status")

    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "user_id": None,
        "error": "Could not read Fyers login status",
    }


def test_upload_status_error_is_fixed_text(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload, "UPLOAD_DIR", SimpleNamespace(glob=_boom))

    response = api.get("/api/upload/upload/status")

    assert response.status_code == 200
    assert response.json() == {"files": [], "count": 0, "error": "Could not list uploaded files"}


# --- handle_api_error / sanitize_error_message: fixed text in every environment -------


@pytest.mark.parametrize("environment", ["development", "production"])
def test_handle_api_error_never_includes_exception_text(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)

    error = handle_api_error(RuntimeError(MARKER), "Failed to do the thing")

    assert sanitize_error_message(RuntimeError(MARKER), "Nope") == "Nope"
    assert error.status_code == 500
    assert error.detail == "Failed to do the thing"


def test_websocket_disconnect_failure_detail_is_fixed(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_market, "disconnect", _boom)

    response = api.post("/api/websocket/disconnect")

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to disconnect WebSocket"}


# --- terminal options preview: validation text kept, internal errors hidden -----------


PREVIEW_BODY = {"symbol": "NSE:NIFTY25JAN24000CE", "side": "BUY", "quantity": 75}


def test_options_preview_internal_failure_is_500_with_fixed_text(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal, "_build_option_symbols", _boom)

    response = api.post("/api/terminal/options/preview-order", json=PREVIEW_BODY)

    assert response.status_code == 500
    assert response.json() == {"detail": "Failed to preview options order"}


def test_options_preview_validation_message_is_kept(api: TestClient) -> None:
    body = {"side": "BUY", "quantity": 75}

    response = api.post("/api/terminal/options/preview-order", json=body)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Provide symbol, or (underlying + expiry + strike + option_type)"
    }


# --- services that hand messages back to API callers ----------------------------------


def test_margin_check_failure_message_has_no_exception_text() -> None:
    manager = RiskManager()
    manager._broker = SimpleNamespace(get_funds=_boom)

    result = manager._check_available_margin({"quantity": 1, "price": 100.0})

    assert result.status == RiskStatus.FAIL
    assert result.code == "MARGIN_CHECK_ERROR"
    assert result.message == "Could not verify margin. Trade denied for safety."


class RaisingReply:
    def get(self, *_args: object) -> NoReturn:
        raise RuntimeError(MARKER)


def test_fyers_place_order_exception_message_is_fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = SimpleNamespace(place_order=lambda data: RaisingReply())
    monkeypatch.setattr(fyers_plugin, "get_fyers_client", lambda: SimpleNamespace(fyers=sdk))
    monkeypatch.setattr(fyers_plugin.FyersBroker, "_circuit_open_until", 0.0)
    broker = fyers_plugin.FyersBroker()

    result = broker.place_order(
        {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1, "type": "MARKET"}
    )

    assert result == {
        "order_id": "",
        "status": "ERROR",
        "message": "Broker order request failed",
        "details": None,
    }


def test_live_quote_failure_is_503_with_fixed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import data_fetcher

    monkeypatch.setattr(data_fetcher, "fetch_fyers_quotes", _boom)

    with pytest.raises(DataSourceUnavailableError) as raised:
        DataProvider(db=None).get_quote("NSE:SBIN-EQ")

    assert raised.value.status_code == 503
    assert raised.value.detail == (
        "Data source 'Fyers' is currently unavailable. Please check connection."
    )


def test_missing_live_quote_stays_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import data_fetcher

    monkeypatch.setattr(data_fetcher, "fetch_fyers_quotes", lambda _symbols: {})

    with pytest.raises(HTTPException) as raised:
        DataProvider(db=None).get_quote("NSE:SBIN-EQ")

    assert isinstance(raised.value, DataNotFoundError)
    assert raised.value.status_code == 404
