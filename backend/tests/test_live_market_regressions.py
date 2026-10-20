"""Regression tests for live-market subscription lifecycle and Fyers client refresh."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
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

    def subscribe(self, symbols: list[str], data_type: object = None) -> None:
        self.subscribed.extend(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
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

    def subscribe(self, symbols: list[str]) -> None:
        self.ws.subscribe(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
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

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.stall and data.get("type") != "connection_established":
            await asyncio.Event().wait()

    async def send_text(self, data: str) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


def _fyers(symbol: str) -> str:
    return symbol_master.to_fyers(symbol)


def test_provider_reconnect_replays_client_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> list[str]:
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


def test_slow_client_drop_releases_only_unshared_provider_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[FakeClientWebSocket, ConnectionManager, list[str]]:
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


def test_explicit_disconnect_is_not_undone_by_automatic_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_broker_and_option_chain_use_current_client_after_relogin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_reset_fyers_client_drops_class_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_client_module.FyersClient, "_instance", object())
    fyers_client_module.reset_fyers_client()
    assert fyers_client_module.FyersClient._instance is None


def test_provider_connect_closes_previous_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[FakeSocket] = []

    def make_socket(**_kwargs: object) -> FakeSocket:
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
    assert created[0].closed
    assert not created[1].closed


def test_http_subscribed_symbols_are_replayed_and_not_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        live.ws_service = FakeProviderService()
        mgr.on_symbols_released = live.unsubscribe

        await live.subscribe_api(["SBIN"])
        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN", "TCS"])
        await mgr.disconnect(client)  # releases TCS only; SBIN belongs to the HTTP route
        unsubscribed = list(live.ws_service.ws.unsubscribed)

        live.ws_service = FakeProviderService()  # provider reconnect
        live.on_ws_connected()
        await asyncio.sleep(0.05)
        return unsubscribed, live.ws_service.ws.subscribed

    unsubscribed, replayed = asyncio.run(scenario())
    assert unsubscribed == [_fyers("TCS")]
    assert replayed == [_fyers("SBIN")]


def test_late_unsubscribe_skips_symbol_resubscribed_by_another_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> list[str]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live = lms.LiveMarketService()
        live.ws_service = FakeProviderService()

        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN"])  # re-subscribed before the stale release lands
        await live.unsubscribe(["SBIN", "TCS"])
        return live.ws_service.ws.unsubscribed

    assert asyncio.run(scenario()) == [_fyers("TCS")]


class SlowConnectProvider(FakeProviderService):
    """connect() returns without raising.

    The socket only reports connected if `connect_after` is set.
    """

    def __init__(
        self,
        connect_after: float | None,
        live: lms.LiveMarketService | None = None,
        open_in_connect: bool = False,
    ) -> None:
        super().__init__()
        self.connect_calls = 0
        self.connect_after = connect_after
        self.live = live
        self.open_in_connect = open_in_connect
        self.ws.closed = True  # is_connected() is `not closed`

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_after is not None:
            self.ws.closed = False
        if self.open_in_connect:
            self.live.on_provider_open()  # the SDK calls on_connect from inside connect()


def _fast_runner(monkeypatch: pytest.MonkeyPatch, retries: int = 3) -> None:
    monkeypatch.setattr(lms, "WS_MAX_RETRIES", retries)
    monkeypatch.setattr(lms, "WS_CONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(lms, "WS_CONNECT_POLL_S", 0.01)
    monkeypatch.setattr(lms, "WS_BACKOFF_BASE_S", 0.01)
    monkeypatch.setattr(lms, "WS_BACKOFF_MAX_S", 0.02)


def test_runner_retries_and_fails_when_connect_returns_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_runner(monkeypatch, retries=3)
    live = lms.LiveMarketService()
    live._is_connecting = True
    live.ws_service = SlowConnectProvider(connect_after=None)

    live._run_provider_connect()

    assert live.ws_service.connect_calls == 3
    assert live.ws_connected is False
    assert live._is_connecting is False


def test_runner_connects_once_and_replays_without_double_subscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_runner(monkeypatch)

    async def scenario() -> lms.LiveMarketService:
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        live.dev_mode = True
        live.api_subscriptions = {"SBIN"}
        # connect() fires the SDK on-open hook (first connect); the runner must not replay again.
        live.ws_service = SlowConnectProvider(connect_after=0, live=live, open_in_connect=True)
        await asyncio.to_thread(live._run_provider_connect)
        await asyncio.sleep(0.05)
        return live

    live = asyncio.run(scenario())
    assert live.ws_connected is True
    assert live.ws_service.connect_calls == 1
    assert live.ws_service.ws.subscribed == [_fyers("SBIN")]


def test_runner_replays_itself_when_sdk_open_hook_did_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_runner(monkeypatch)

    async def scenario() -> lms.LiveMarketService:
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        live.dev_mode = True
        live.api_subscriptions = {"TCS"}
        live.ws_service = SlowConnectProvider(connect_after=0)
        await asyncio.to_thread(live._run_provider_connect)
        await asyncio.sleep(0.05)
        return live

    live = asyncio.run(scenario())
    assert live.ws_service.ws.subscribed == [_fyers("TCS")]


def test_explicit_disconnect_stops_runner_during_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    _fast_runner(monkeypatch, retries=10)
    monkeypatch.setattr(lms, "WS_BACKOFF_BASE_S", 30.0)
    monkeypatch.setattr(lms, "WS_BACKOFF_MAX_S", 30.0)
    live = lms.LiveMarketService()
    live.ws_service = SlowConnectProvider(connect_after=None)

    thread = threading.Thread(target=live._run_provider_connect, daemon=True)
    thread.start()
    time.sleep(0.2)  # first attempt timed out, runner is now backing off for 30s
    live.disconnect()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert live.ws_service.connect_calls == 1


def test_sdk_reconnect_open_callback_replays_subscriptions() -> None:
    async def scenario() -> list[str]:
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        live.dev_mode = True
        live.api_subscriptions = {"SBIN"}
        service = fyers_websocket.FyersWebSocketService()
        service.ws = FakeSocket()
        service.on_open_handler = live.on_provider_open
        live.ws_service = service

        # The SDK's own reconnect=True path re-invokes the on_connect callback with no runner.
        await asyncio.to_thread(service._on_open)
        await asyncio.sleep(0.05)
        await asyncio.to_thread(service._on_open)
        await asyncio.sleep(0.05)
        return service.ws.subscribed

    assert asyncio.run(scenario()) == [_fyers("SBIN")] * 2


def test_flush_loop_survives_a_broadcast_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> tuple[list[dict[str, Any]], bool]:
        mgr = ConnectionManager()
        calls: list[dict[str, Any]] = []

        async def flaky_broadcast(msg: dict[str, Any]) -> None:
            calls.append(msg)
            if len(calls) == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(mgr, "broadcast", flaky_broadcast)
        monkeypatch.setattr(lms, "manager", mgr)
        monkeypatch.setattr(lms, "LOOP_ERROR_PAUSE_S", 0.01)
        live = lms.LiveMarketService()
        task = asyncio.create_task(live._flush_loop())
        live.tick_buffer = {"A": {"symbol": "A"}}
        await asyncio.sleep(0.3)
        live.tick_buffer = {"B": {"symbol": "B"}}
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return calls, True
        return calls, False

    calls, cancelled = asyncio.run(scenario())
    assert [c["data"][0]["symbol"] for c in calls] == ["A", "B"]
    assert cancelled is True


def test_monitor_loop_survives_an_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep

    async def scenario() -> tuple[bool, int]:
        async def fast_sleep(_delay: float) -> None:
            await real_sleep(0.001)

        live = lms.LiveMarketService()
        checks = {"n": 0}

        def flaky_is_market_open() -> bool:
            checks["n"] += 1
            if checks["n"] == 1:
                raise RuntimeError("boom")
            return True

        monkeypatch.setattr(live, "is_market_open", flaky_is_market_open)
        monkeypatch.setattr(lms.asyncio, "sleep", fast_sleep)
        task = asyncio.create_task(live._monitor_loop())
        await real_sleep(0.1)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive, checks["n"]

    alive, checks = asyncio.run(scenario())
    assert alive is True
    assert checks > 1


def test_connect_async_validates_token_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    seen: dict[str, int] = {}

    def fake_validate() -> bool:
        seen["thread"] = threading.get_ident()
        return False

    async def scenario() -> tuple[str, int]:
        live = lms.LiveMarketService()
        monkeypatch.setattr(live, "_validate_fyers_token", fake_validate)
        status = await live.connect_async()
        return status, threading.get_ident()

    status, loop_thread = asyncio.run(scenario())
    assert status == "token_invalid"
    assert seen["thread"] != loop_thread


def test_off_hours_cleanup_stops_a_retrying_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #34 review: market-close cleanup must stop a runner that is backing off,
    or it reconnects off-hours."""
    import threading
    import time

    _fast_runner(monkeypatch, retries=10)
    monkeypatch.setattr(lms, "WS_BACKOFF_BASE_S", 30.0)
    monkeypatch.setattr(lms, "WS_BACKOFF_MAX_S", 30.0)
    live = lms.LiveMarketService()
    live.ws_service = SlowConnectProvider(connect_after=None)
    live._is_connecting = True
    monkeypatch.setattr(live, "is_market_open", lambda: False)

    thread = threading.Thread(
        target=live._run_provider_connect, args=(live._runner_stop,), daemon=True
    )
    thread.start()
    time.sleep(0.2)  # first attempt timed out, runner is now backing off for 30s
    asyncio.run(live._check_connection_health())
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert live.ws_service.connect_calls == 1
    assert live._is_connecting is False
    assert live._explicitly_disconnected is False  # next session may still auto-connect


def test_new_runner_is_not_stopped_by_the_previous_off_hours_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    started: list[threading.Event] = []

    class RecordingThread:
        def __init__(self, target: object, args: tuple[Any, ...], daemon: bool) -> None:
            started.append(args[0])

        def start(self) -> None:
            return None

    monkeypatch.setattr(lms.threading, "Thread", RecordingThread)
    live = lms.LiveMarketService()
    live.ws_service = SlowConnectProvider(connect_after=None)
    live._runner_stop.set()  # an earlier off-hours cleanup

    assert live._start_provider_connection() == "started"
    assert len(started) == 1
    assert started[0] is live._runner_stop
    assert not started[0].is_set()


def _tick_at(ts: float) -> dict[str, Any]:
    return {"symbol": "SBIN", "ltp": 100.0, "exch_feed_time": ts}


@pytest.mark.parametrize(
    ("market_open", "age", "fresh"),
    [(True, 5.0, True), (True, 120.0, False), (False, 5.0, False)],
)
def test_get_fresh_tick_requires_open_market_and_recent_tick(
    monkeypatch: pytest.MonkeyPatch, market_open: bool, age: float, fresh: bool
) -> None:
    import time

    monkeypatch.delenv("PAPER_TICK_MAX_AGE_SEC", raising=False)
    live = lms.LiveMarketService()
    monkeypatch.setattr(live, "is_market_open", lambda: market_open)
    live.latest_values = {"SBIN": _tick_at(time.time() - age), "TCS": {"ltp": 1.0}}

    assert (live.get_fresh_tick("SBIN") is not None) is fresh
    assert live.get_fresh_tick("TCS") is None  # no exchange time: stale
    assert live.get_fresh_tick("INFY") is None
    expected = {"SBIN"} if fresh else set()
    assert set(live.get_fresh_ticks(["SBIN", "TCS", "INFY"])) == expected


def test_hot_path_tracebacks_are_rate_limited_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from app.utils import logger as logger_module
    from app.utils.runtime_metrics import runtime_metrics

    monkeypatch.setattr(logger_module, "_last_traceback_at", {})
    metric = "logging.suppressed_tracebacks.fyers_ws.tick_handler"
    before = runtime_metrics.snapshot()["counters"].get(metric, 0)

    def broken_handler(_message: dict[str, Any]) -> None:
        raise ValueError("bad tick")

    ws = fyers_websocket.FyersWebSocketService.__new__(fyers_websocket.FyersWebSocketService)
    ws.on_tick_handler = broken_handler
    with caplog.at_level(logging.DEBUG, logger=fyers_websocket.logger.name):
        for _ in range(3):
            ws._on_message({"symbol": "NSE:SBIN-EQ"})

    with_traceback = [r for r in caplog.records if r.exc_info]
    assert len(with_traceback) == 1
    assert runtime_metrics.snapshot()["counters"].get(metric, 0) - before == 2

    # After the interval the next failure logs a full traceback again.
    monkeypatch.setattr(logger_module, "TRACEBACK_INTERVAL_S", 0.0)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=fyers_websocket.logger.name):
        ws._on_message({"symbol": "NSE:SBIN-EQ"})
    assert [r for r in caplog.records if r.exc_info]
