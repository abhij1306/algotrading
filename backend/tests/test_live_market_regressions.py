"""Regression tests for live-market subscription lifecycle and Fyers client refresh."""

import asyncio
from types import SimpleNamespace

from starlette.websockets import WebSocketState

from app.services import fyers_client as fyers_client_module
from app.services import fyers_websocket
from app.services import live_market_service as lms
from app.services.symbol_master import symbol_master
from app.utils.ws_manager import ConnectionManager


class FakeSocket:
    """Stands in for fyers FyersDataSocket."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed

    def connect(self) -> None:
        pass

    def subscribe(self, symbols, data_type=None) -> None:
        self.subscribed.extend(symbols)

    def unsubscribe(self, symbols) -> None:
        self.unsubscribed.extend(symbols)

    def close(self) -> None:
        self.closed = True


class FakeProviderService:
    """Stands in for FyersWebSocketService."""

    def __init__(self) -> None:
        self.ws = FakeSocket()
        self.disconnected = False
        self.on_tick_handler = None

    def connect(self) -> None:
        pass

    def subscribe(self, symbols) -> None:
        self.ws.subscribe(symbols)

    def unsubscribe(self, symbols) -> None:
        self.ws.unsubscribe(symbols)

    def disconnect(self) -> None:
        self.disconnected = True
        self.ws.close()


class FakeClientWebSocket:
    """Browser client socket; send_json blocks after the handshake when `stall` is set."""

    def __init__(self, stall: bool = False) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.stall = stall
        self.close_code: int | None = None

    async def accept(self) -> None:
        pass

    async def send_json(self, data) -> None:
        if self.stall and data.get("type") != "connection_established":
            await asyncio.Event().wait()

    async def send_text(self, data) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def _fyers(symbol: str) -> str:
    return symbol_master.to_fyers(symbol)


def test_provider_reconnect_replays_client_subscriptions(monkeypatch):
    async def scenario():
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        live.dev_mode = True

        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN", "TCS"])

        # Provider dropped and came back as a brand new socket.
        live.ws_service = FakeProviderService()
        live.on_ws_connected()
        await asyncio.sleep(0.05)
        return live.ws_service.ws.subscribed

    assert sorted(asyncio.run(scenario())) == sorted([_fyers("SBIN"), _fyers("TCS")])


def test_slow_client_drop_releases_only_unshared_provider_symbols(monkeypatch):
    async def scenario():
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live = lms.LiveMarketService()
        live.ws_service = FakeProviderService()
        mgr.on_symbols_released = live.unsubscribe

        slow = FakeClientWebSocket(stall=True)
        healthy = FakeClientWebSocket()
        assert await mgr.connect(slow)
        assert await mgr.connect(healthy)
        await mgr.subscribe(slow, ["SBIN", "TCS"])
        await mgr.subscribe(healthy, ["SBIN"])

        for _ in range(30):
            await mgr.broadcast({"type": "ticker_batch", "data": [{"symbol": "TCS", "ltp": 1}]})
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        return slow, mgr, live.ws_service.ws.unsubscribed

    slow, mgr, unsubscribed = asyncio.run(scenario())
    assert unsubscribed == [_fyers("TCS")]  # SBIN is still needed by the healthy client
    assert slow.close_code == 1013
    assert mgr.get_all_subscribed_symbols() == {"SBIN"}


def test_explicit_disconnect_is_not_undone_by_automatic_connect(monkeypatch):
    live = lms.LiveMarketService()
    live.dev_mode = True
    provider = FakeProviderService()
    live.ws_service = provider
    live.ws_connected = True
    monkeypatch.setattr(lms, "live_market", live)

    from app.routers.websocket import disconnect_websocket

    assert disconnect_websocket() == {"status": "disconnected"}
    assert provider.disconnected
    assert live.ws_connected is False

    # The monitor loop and subscribe() call connect() without `explicit`.
    assert live.connect(prevalidated_token=True) == "disconnected"

    # An explicit connect lifts the disconnect.
    monkeypatch.setattr(fyers_websocket, "get_websocket_service", lambda: provider)
    assert live.connect(prevalidated_token=True, explicit=True) == "started"


def test_broker_and_option_chain_use_current_client_after_relogin(monkeypatch):
    from app.brokers.plugins import fyers as broker_module
    from app.services.option_chain_service import OptionChainService

    holder = SimpleNamespace(current=SimpleNamespace(fyers="model-old-token"))
    monkeypatch.setattr(broker_module, "get_fyers_client", lambda: holder.current)
    monkeypatch.setattr(fyers_client_module, "get_fyers_client", lambda: holder.current)

    broker = broker_module.FyersBroker()
    chain = OptionChainService()
    assert broker.client == "model-old-token"
    assert chain._get_fyers_client().fyers == "model-old-token"

    holder.current = SimpleNamespace(fyers="model-new-token")  # re-login
    assert broker.client == "model-new-token"
    assert chain._get_fyers_client().fyers == "model-new-token"


def test_reset_fyers_client_drops_class_singleton(monkeypatch):
    monkeypatch.setattr(fyers_client_module.FyersClient, "_instance", object())
    fyers_client_module.reset_fyers_client()
    assert fyers_client_module.FyersClient._instance is None


def test_provider_connect_closes_previous_socket(monkeypatch):
    created: list[FakeSocket] = []

    def make_socket(**_kwargs):
        sock = FakeSocket()
        created.append(sock)
        return sock

    monkeypatch.setattr(fyers_websocket, "data_ws", SimpleNamespace(FyersDataSocket=make_socket))
    monkeypatch.setattr(
        fyers_client_module,
        "get_fyers_client",
        lambda: SimpleNamespace(client_id="C", access_token="T"),
    )

    service = fyers_websocket.FyersWebSocketService()
    service.connect()
    service.connect()

    assert len(created) == 2
    assert created[0].closed and not created[1].closed
