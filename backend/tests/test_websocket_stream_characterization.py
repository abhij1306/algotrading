"""Characterization tests for the /api/websocket/stream endpoint and
ConnectionManager.broadcast, pinned ahead of a complexity refactor."""

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

from app.routers import websocket as ws_router
from app.services import live_market_service as lms
from app.utils.ws_manager import ConnectionManager


class RecordingLiveMarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def subscribe(self, symbols: list[str]) -> None:
        self.calls.append(("subscribe", symbols))

    async def unsubscribe(self, symbols: list[str]) -> None:
        self.calls.append(("unsubscribe", symbols))


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, RecordingLiveMarket]:
    live = RecordingLiveMarket()
    monkeypatch.setattr(lms, "live_market", live)
    monkeypatch.setattr(ws_router, "manager", ConnectionManager())
    app = FastAPI()
    app.include_router(ws_router.router, prefix="/api/websocket")
    return TestClient(app), live


def test_stream_handshake_ping_subscribe_and_unsubscribe(
    stream: tuple[TestClient, RecordingLiveMarket],
) -> None:
    client, live = stream
    with client.websocket_connect("/api/websocket/stream") as ws:
        assert ws.receive_json() == {
            "type": "connection_established",
            "message": "Connected to SmartTrader WebSocket Stream",
        }
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}

        ws.send_text("{not json")
        ws.send_text(json.dumps({"action": "subscribe", "symbols": []}))
        ws.send_text(json.dumps({"type": "ping"}))
        assert ws.receive_json() == {"type": "pong"}

        ws.send_text(json.dumps({"action": "subscribe", "symbols": ["NSE:SBIN-EQ", "TCS"]}))
        assert ws.receive_json() == {
            "type": "ack",
            "action": "subscribe",
            "count": 2,
            "provider_delta": 2,
        }
        ws.send_text(json.dumps({"type": "subscribe", "symbols": ["SBIN"]}))
        assert ws.receive_json() == {
            "type": "ack",
            "action": "subscribe",
            "count": 1,
            "provider_delta": 0,
        }

        ws.send_text(json.dumps({"action": "unsubscribe", "symbols": ["TCS", "INFY"]}))
        assert ws.receive_json() == {
            "type": "ack",
            "action": "unsubscribe",
            "count": 2,
            "provider_delta": 1,
        }
        ws.send_text(json.dumps({"action": "unsubscribe", "symbols": []}))
        ws.send_text(json.dumps({"action": "noop"}))
        ws.send_text("ping")
        assert ws.receive_json() == {"type": "pong"}

    assert live.calls == [
        ("subscribe", ["SBIN", "TCS"]),
        ("unsubscribe", ["TCS"]),
    ]


def test_stream_subscribe_ack_survives_provider_failure(
    stream: tuple[TestClient, RecordingLiveMarket], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, live = stream

    async def failing_subscribe(_symbols: list[str]) -> None:
        raise RuntimeError("provider down")

    monkeypatch.setattr(live, "subscribe", failing_subscribe)
    with client.websocket_connect("/api/websocket/stream") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"action": "subscribe", "symbols": ["SBIN"]}))
        assert ws.receive_json() == {
            "type": "ack",
            "action": "subscribe",
            "count": 1,
            "provider_delta": 1,
        }


# --- ConnectionManager.broadcast -------------------------------------------------


class RecordingSocket:
    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.json_sent: list[dict[str, Any]] = []
        self.text_sent: list[str] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict[str, Any]) -> None:
        self.json_sent.append(data)

    async def send_text(self, data: str) -> None:
        self.text_sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.client_state = WebSocketState.DISCONNECTED


def run_broadcast(messages: list[dict[str, Any]]) -> tuple[list[RecordingSocket], set[int]]:
    """Two clients: [0] subscribed to SBIN, [1] to TCS. Returns sockets and live client ids."""

    async def scenario() -> tuple[list[RecordingSocket], set[int]]:
        mgr = ConnectionManager()
        sockets = [RecordingSocket(), RecordingSocket()]
        for sock, symbol in zip(sockets, ["SBIN", "TCS"], strict=True):
            assert await mgr.connect(sock)
            await mgr.subscribe(sock, [symbol])
        for message in messages:
            await mgr.broadcast(message)
        await asyncio.sleep(0.05)
        live_ids = {i for i, s in enumerate(sockets) if s in mgr.connections}
        for sock in list(mgr.connections):
            await mgr.disconnect(sock)
        return sockets, live_ids

    return asyncio.run(scenario())


def test_broadcast_filters_ticker_batches_per_client() -> None:
    batch = {
        "type": "ticker_batch",
        "data": [{"symbol": "SBIN", "ltp": 1.0}, {"symbol": "INFY", "ltp": 2.0}],
    }

    sockets, live_ids = run_broadcast([batch])

    assert sockets[0].json_sent[1:] == [
        {"type": "ticker_batch", "data": [{"symbol": "SBIN", "ltp": 1.0}]}
    ]
    assert sockets[1].json_sent[1:] == []
    assert live_ids == {0, 1}


def test_broadcast_single_ticker_goes_to_subscribers_pre_serialized() -> None:
    tick = {"type": "ticker", "data": {"symbol": "TCS", "ltp": 3.0}}
    notice = {"type": "status", "data": {"state": "open"}}

    sockets, _ = run_broadcast([tick, notice])

    assert sockets[0].text_sent == [json.dumps(notice)]
    assert sockets[1].text_sent == [json.dumps(tick), json.dumps(notice)]


def test_broadcast_skips_unserializable_message() -> None:
    sockets, live_ids = run_broadcast([{"type": "status", "data": {1, 2}}])

    assert [s.text_sent for s in sockets] == [[], []]
    assert live_ids == {0, 1}


def test_broadcast_drops_clients_that_are_no_longer_connected() -> None:
    async def scenario() -> tuple[list[str], int]:
        mgr = ConnectionManager()
        gone, alive = RecordingSocket(), RecordingSocket()
        for sock in (gone, alive):
            await mgr.connect(sock)
        gone.client_state = WebSocketState.DISCONNECTED
        await mgr.broadcast({"type": "status"})
        await asyncio.sleep(0.05)
        remaining = len(mgr.connections)
        await mgr.disconnect(alive)
        return alive.text_sent, remaining

    sent, remaining = asyncio.run(scenario())
    assert sent == [json.dumps({"type": "status"})]
    assert remaining == 1
