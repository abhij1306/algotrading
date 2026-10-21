"""Endpoints never send exception text to the client, and broker-backed reads fail closed.

Every fake failure carries MARKER; a response that contains it leaked the raw error.
"""

from collections.abc import Generator
from types import SimpleNamespace
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from app.main import app

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
