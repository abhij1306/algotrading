"""Regression tests for live-market subscription lifecycle and Fyers client refresh."""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketState

from app.services import fyers_client as fyers_client_module
from app.services import fyers_websocket
from app.services import live_market_service as lms
from app.services.live.data_socket import SocketCallbacks
from app.services.live.provider_session import RetryPolicy, SessionState
from app.services.subscription_registry import API_OWNER
from app.services.symbol_master import symbol_master
from app.utils.ws_manager import ConnectionManager

from fakes.data_socket import (
    FakeSocketFactory,
    connected_live,
    make_live,
    run_inline,
    wait_until_backing_off,
)
from fakes.market import MARKET_OPEN_INSTANT, FreezeMarket


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
        live, factory = connected_live()
        live.loop = asyncio.get_running_loop()

        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN", "TCS"])

        # Provider dropped and came back as a brand new socket.
        factory.last.drop()
        assert live.session.start() is True
        return factory.last.subscribed

    assert sorted(asyncio.run(scenario())) == sorted([_fyers("SBIN"), _fyers("TCS")])


def test_slow_client_drop_releases_only_unshared_provider_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[FakeClientWebSocket, ConnectionManager, list[str]]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live, factory = connected_live()
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
        return slow, mgr, factory.last.unsubscribed

    slow, mgr, unsubscribed = asyncio.run(scenario())
    assert unsubscribed == [_fyers("TCS")]  # SBIN is still needed by the healthy client
    assert slow.close_code == 1013
    assert mgr.registry.all_symbols() == {"SBIN"}


def test_explicit_disconnect_is_not_undone_by_automatic_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, factory = connected_live()
    live.dev_mode = True
    socket = factory.last
    monkeypatch.setattr(lms, "live_market", live)

    from app.routers.websocket import disconnect_websocket

    assert disconnect_websocket() == {"status": "disconnected"}
    assert socket.closed is True
    assert live.session.state is SessionState.CLOSED

    # A late SDK auto-reconnect of the closed socket does not bring it back.
    socket.sdk_reconnect()
    assert live.session.is_connected() is False

    # The monitor loop and subscribe() call connect() without `explicit`.
    assert live.connect(prevalidated_token=True) == "disconnected"
    assert len(factory.sockets) == 1

    # An explicit connect lifts the disconnect.
    assert live.connect(prevalidated_token=True, explicit=True) == "started"
    assert live.session.is_connected() is True
    assert len(factory.sockets) == 2


def test_reset_fyers_client_drops_class_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_client_module.FyersClient, "_instance", object())
    fyers_client_module.reset_fyers_client()
    assert fyers_client_module.FyersClient._instance is None


def test_fyers_socket_factory_wires_the_token_and_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []

    def make_socket(**kwargs: object) -> object:
        built.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(fyers_websocket, "data_ws", SimpleNamespace(FyersDataSocket=make_socket))
    callbacks = SocketCallbacks(
        on_message=lambda _m: None,
        on_connect=lambda: None,
        on_close=lambda _m: None,
        on_error=lambda _e: None,
    )

    fyers_websocket.fyers_socket_factory("C:T", callbacks)

    assert len(built) == 1
    kwargs = built[0]
    assert kwargs["access_token"] == "C:T"
    assert kwargs["reconnect"] is True
    assert kwargs["on_connect"] is callbacks.on_connect
    assert kwargs["on_message"] is callbacks.on_message
    assert kwargs["on_close"] is callbacks.on_close
    assert kwargs["on_error"] is callbacks.on_error


def test_fyers_socket_factory_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fyers_websocket, "data_ws", None)
    callbacks = SocketCallbacks(lambda _m: None, lambda: None, lambda _m: None, lambda _e: None)

    with pytest.raises(fyers_websocket.FyersWebSocketError):
        fyers_websocket.fyers_socket_factory("C:T", callbacks)


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (SimpleNamespace(client_id="C", access_token="T"), "C:T"),
        (SimpleNamespace(client_id="C", access_token=None), None),
        (SimpleNamespace(client_id=None, access_token="T"), None),
        (None, None),
    ],
)
def test_fyers_token_reads_the_current_client(
    monkeypatch: pytest.MonkeyPatch, client: object, expected: str | None
) -> None:
    monkeypatch.setattr(fyers_client_module, "get_fyers_client", lambda: client)

    assert fyers_websocket.fyers_token() == expected


def test_http_subscribed_symbols_are_replayed_and_not_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live, factory = connected_live()
        live.loop = asyncio.get_running_loop()
        mgr.on_symbols_released = live.unsubscribe

        await live.subscribe_api(["SBIN"])
        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN", "TCS"])
        await mgr.disconnect(client)  # releases TCS only; SBIN belongs to the HTTP route
        unsubscribed = list(factory.last.unsubscribed)

        factory.last.drop()  # provider reconnect on a new socket
        assert live.session.start() is True
        return unsubscribed, factory.last.subscribed

    unsubscribed, replayed = asyncio.run(scenario())
    assert unsubscribed == [_fyers("TCS")]
    assert replayed == [_fyers("SBIN")]


def test_late_unsubscribe_skips_symbol_resubscribed_by_another_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> list[str]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        live, factory = connected_live()

        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["SBIN"])  # re-subscribed before the stale release lands
        await live.unsubscribe(["SBIN", "TCS"])
        return factory.last.unsubscribed

    assert asyncio.run(scenario()) == [_fyers("TCS")]


def test_sdk_auto_reconnect_replays_subscriptions_on_the_same_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lms, "manager", ConnectionManager())
    live, factory = make_live()
    live.registry.add(API_OWNER, ["SBIN"])
    assert live.session.start() is True

    # The SDK's own reconnect=True path re-invokes on_connect on the same socket.
    factory.last.drop()
    factory.last.sdk_reconnect()

    assert len(factory.sockets) == 1
    assert factory.last.subscribed == [_fyers("SBIN")] * 2
    assert live.session.is_connected() is True


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


def test_off_hours_cleanup_stops_a_retrying_runner(freeze_market: FreezeMarket) -> None:
    """PR #34 review: market-close cleanup must stop a runner that is backing off,
    or it reconnects off-hours."""
    runner: list[Any] = []
    live, factory = make_live(FakeSocketFactory(comes_up=False), spawn=runner.append)
    live.session.retry = RetryPolicy(
        max_attempts=10, connect_timeout_s=0.02, poll_s=0.005, backoff_base_s=30.0
    )
    assert live.session.start() is True
    thread = threading.Thread(target=runner[0], daemon=True)
    thread.start()
    wait_until_backing_off(factory)
    freeze_market(False, live)

    asyncio.run(live._check_connection_health())
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(factory.sockets) == 1
    assert live.session.state is SessionState.IDLE
    assert live._explicitly_disconnected is False  # next session may still auto-connect


def test_next_session_connects_after_the_previous_off_hours_cleanup(
    freeze_market: FreezeMarket,
) -> None:
    live, factory = connected_live()
    clock = freeze_market(False, live)
    asyncio.run(live._check_connection_health())  # market closed: socket closed
    assert factory.last.closed is True

    clock.set(MARKET_OPEN_INSTANT)  # the next session

    assert live.connect(prevalidated_token=True) == "started"
    assert live.session.is_connected() is True
    assert len(factory.sockets) == 2


def _tick_at(ts: float) -> dict[str, Any]:
    return {"symbol": "SBIN", "ltp": 100.0, "exch_feed_time": ts}


@pytest.mark.parametrize(
    ("market_open", "age", "fresh"),
    [(True, 5.0, True), (True, 120.0, False), (False, 5.0, False)],
)
def test_get_fresh_tick_requires_open_market_and_recent_tick(
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
    market_open: bool,
    age: float,
    fresh: bool,
) -> None:

    monkeypatch.delenv("PAPER_TICK_MAX_AGE_SEC", raising=False)
    live = lms.LiveMarketService()
    freeze_market(market_open, live)
    live.latest_values = {"SBIN": _tick_at(time.time() - age), "TCS": {"ltp": 1.0}}

    assert (live.get_fresh_tick("SBIN") is not None) is fresh
    assert live.get_fresh_tick("TCS") is None  # no exchange time: stale
    assert live.get_fresh_tick("INFY") is None
    expected = {"SBIN"} if fresh else set()
    assert set(live.get_fresh_ticks(["SBIN", "TCS", "INFY"])) == expected


def test_hot_path_tracebacks_are_rate_limited_and_counted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.services.live import provider_session
    from app.utils import logger as logger_module
    from app.utils.runtime_metrics import runtime_metrics

    monkeypatch.setattr(logger_module, "_last_traceback_at", {})
    metric = "logging.suppressed_tracebacks.provider_session.on_message"
    before = runtime_metrics.snapshot()["counters"].get(metric, 0)
    live, factory = connected_live()

    def broken_handler(_message: dict[str, Any]) -> None:
        raise ValueError("bad tick")

    monkeypatch.setattr(live.session, "_on_message", broken_handler)
    with caplog.at_level(logging.DEBUG, logger=provider_session.logger.name):
        for _ in range(3):
            factory.last.emit({"symbol": "NSE:SBIN-EQ"})

    with_traceback = [r for r in caplog.records if r.exc_info]
    assert len(with_traceback) == 1
    assert runtime_metrics.snapshot()["counters"].get(metric, 0) - before == 2

    # After the interval the next failure logs a full traceback again.
    monkeypatch.setattr(logger_module, "TRACEBACK_INTERVAL_S", 0.0)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=provider_session.logger.name):
        factory.last.emit({"symbol": "NSE:SBIN-EQ"})
    assert [r for r in caplog.records if r.exc_info]


class FixedStatusLiveMarket:
    """A live_market whose connect always reports one status."""

    def __init__(self, status: str) -> None:
        self.status = status

    async def connect_async(self, **_kwargs: object) -> str:
        await asyncio.sleep(0)
        return self.status


def test_connect_route_and_startup_log_report_the_same_status_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(lms, "live_market", FixedStatusLiveMarket("token_invalid"))

    from app.main import _log_live_market_status
    from app.routers.websocket import connect_websocket

    assert asyncio.run(connect_websocket()) == {
        "status": "token_invalid",
        "message": "WebSocket deferred because Fyers token is invalid/expired",
    }

    with caplog.at_level(logging.INFO, logger="smarttrader.main"):
        _log_live_market_status("token_invalid")
    assert [r.getMessage() for r in caplog.records if r.name == "smarttrader.main"] == [
        "[WARN] Live market: WebSocket deferred because Fyers token is invalid/expired "
        "(token_invalid)"
    ]


def test_connect_route_reports_a_generic_message_for_an_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lms, "live_market", FixedStatusLiveMarket("something_new"))

    from app.routers.websocket import connect_websocket

    assert asyncio.run(connect_websocket()) == {
        "status": "something_new",
        "message": "WebSocket initialization completed",
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("idle", False),
        ("connecting", True),  # a connect in flight builds a socket with the current token
        ("connected", True),
        ("dropped", True),  # the dropped socket is still held (the SDK may reconnect it)
        ("closed", False),
        ("failed", False),
    ],
)
def test_has_provider_socket_follows_the_session(state: str, expected: bool) -> None:
    factory = FakeSocketFactory(comes_up=state != "failed")
    spawn = (lambda _target: None) if state == "connecting" else run_inline
    live, factory = make_live(factory, spawn=spawn)
    if state != "idle":
        assert live.session.start() is True
    if state == "dropped":
        factory.last.drop()
    elif state == "closed":
        live.disconnect()

    assert live.has_provider_socket() is expected
