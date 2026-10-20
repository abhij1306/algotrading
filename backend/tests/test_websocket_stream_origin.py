"""The Origin check on WS /api/websocket/stream: only the configured frontend origins (and
non-browser clients that send no Origin) get through the handshake."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import config, parse_origins
from app.routers import websocket as ws_router
from app.services import live_market_service as lms
from app.utils.ws_manager import ConnectionManager

STREAM = "/api/websocket/stream"
ESTABLISHED = {
    "type": "connection_established",
    "message": "Connected to SmartTrader WebSocket Stream",
}


class NoLiveMarket:
    async def subscribe(self, symbols: list[str]) -> None:
        raise AssertionError("not reached")

    async def unsubscribe(self, symbols: list[str]) -> None:
        raise AssertionError("not reached")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(lms, "live_market", NoLiveMarket())
    monkeypatch.setattr(ws_router, "manager", ConnectionManager())
    monkeypatch.setattr(
        config,
        "ALLOWED_ORIGINS",
        ("http://localhost:3000", "http://127.0.0.1:3000"),
    )
    app = FastAPI()
    app.include_router(ws_router.router, prefix="/api/websocket")
    return TestClient(app)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": "http://localhost:3000"}, id="localhost"),
        pytest.param({"origin": "http://127.0.0.1:3000"}, id="loopback"),
        pytest.param({}, id="no-origin"),
    ],
)
def test_allowed_handshake_is_accepted(client: TestClient, headers: dict[str, str]) -> None:
    with client.websocket_connect(STREAM, headers=headers) as ws:
        assert ws.receive_json() == ESTABLISHED
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://evil.example", id="other-site"),
        pytest.param("http://localhost:3999", id="other-port"),
        pytest.param("https://localhost:3000", id="other-scheme"),
        pytest.param("null", id="null"),
    ],
)
def test_disallowed_origin_is_refused_before_accept(
    client: TestClient, origin: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.DEBUG, logger=ws_router.logger.name),
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(STREAM, headers={"origin": origin}),
    ):
        pytest.fail("handshake should have been refused")

    assert refused.value.code == 1008
    assert origin not in (refused.value.reason or "")
    assert any("disallowed Origin" in r.getMessage() for r in caplog.records)


def test_null_origin_is_refused_even_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ("null", "http://localhost:3000"))
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(STREAM, headers={"origin": "null"}),
    ):
        pytest.fail("handshake should have been refused")

    assert refused.value.code == 1008


def test_parse_origins_drops_blanks_and_whitespace() -> None:
    assert parse_origins(" http://localhost:3000, ,http://127.0.0.1:3000,") == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )
