"""Characterization tests pinning LiveMarketService behaviour (monitor loop, tick
normalisation, connect outcomes, subscribe/unsubscribe) ahead of a refactor."""

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from app.services import fyers_websocket
from app.services import live_market_service as lms
from app.services.subscription_registry import API_OWNER
from app.services.symbol_master import symbol_master
from app.utils.ws_manager import ConnectionManager


class FakeSocket:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def is_connected(self) -> bool:
        return self.connected


class FakeProvider:
    def __init__(self, connected: bool = True) -> None:
        self.ws = FakeSocket(connected)
        self.disconnect_calls = 0
        self.on_tick_handler = None
        self.on_open_handler = None

    def connect(self) -> None:
        pass

    def subscribe(self, symbols: list[str]) -> None:
        self.ws.subscribed.extend(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
        self.ws.unsubscribed.extend(symbols)

    def disconnect(self) -> None:
        self.disconnect_calls += 1


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stub run_coroutine_threadsafe: close each coroutine (no "never awaited" warning)
    and record (coroutine name, loop) for the test to check."""
    calls: list[Any] = []

    def fake_run_coroutine_threadsafe(
        coro: Coroutine[Any, Any, None], loop: asyncio.AbstractEventLoop
    ) -> None:
        calls.append((coro.cr_code.co_name, loop))
        coro.close()

    monkeypatch.setattr(lms.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)
    return calls


def _fyers(symbol: str) -> str:
    return symbol_master.to_fyers(symbol)


# --- Monitor loop: one health check per iteration ------------------------------


def run_one_monitor_iteration(
    monkeypatch: pytest.MonkeyPatch, live: lms.LiveMarketService
) -> list[str]:
    """Run _monitor_loop for exactly one check; return the connect_async calls made."""
    sleeps = {"n": 0}
    connects: list[str] = []

    async def one_shot_sleep(_delay: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError

    async def fake_connect_async(**_kwargs: object) -> str:
        connects.append("connect")
        return "started"

    monkeypatch.setattr(lms.asyncio, "sleep", one_shot_sleep)
    monkeypatch.setattr(live, "connect_async", fake_connect_async)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await live._monitor_loop()

    asyncio.run(scenario())
    return connects


@pytest.mark.parametrize(
    ("market_open", "provider_connected", "ws_connected", "connecting", "expected"),
    [
        # (connect_async calls, ws_connected after, _is_connecting after, provider disconnects)
        (True, False, True, False, (1, False, False, 0)),
        (True, False, False, False, (1, False, False, 0)),
        (True, False, False, True, (0, False, True, 0)),
        (True, True, False, False, (0, True, False, 0)),
        (True, True, True, False, (0, True, False, 0)),
        (False, True, True, False, (0, False, False, 1)),
        (False, False, False, True, (0, False, False, 1)),
        (False, False, False, False, (0, False, False, 0)),
    ],
)
def test_monitor_iteration_outcome(
    monkeypatch: pytest.MonkeyPatch,
    market_open: bool,
    provider_connected: bool,
    ws_connected: bool,
    connecting: bool,
    expected: tuple[int, bool, bool, int],
) -> None:
    live = lms.LiveMarketService()
    provider = FakeProvider(connected=provider_connected)
    live.ws_service = provider
    live.ws_connected = ws_connected
    live._is_connecting = connecting
    monkeypatch.setattr(live, "is_market_open", lambda: market_open)

    connects = run_one_monitor_iteration(monkeypatch, live)

    observed = (len(connects), live.ws_connected, live._is_connecting, provider.disconnect_calls)
    assert observed == expected


# --- Tick normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("tick", "expected"),
    [
        (
            {"symbol": "NSE:SBIN-EQ", "ltp": 812.5, "chp": 1.2, "ch": 9.6, "v": 1000},
            {
                "symbol": "SBIN",
                "ltp": 812.5,
                "chp": 1.2,
                "change_pct": 1.2,
                "ch": 9.6,
                "change": 9.6,
                "v": 1000,
                "volume": 1000,
            },
        ),
        (
            {"symbol": "NSE:SBIN-EQ", "ltp": 110.0, "prev_close_price": 100.0},
            {
                "symbol": "SBIN",
                "ltp": 110.0,
                "prev_close_price": 100.0,
                "change_pct": 10.0,
            },
        ),
        (
            {"symbol": "NSE:SBIN-EQ", "ltp": 110.0, "prev_close_price": 0},
            {"symbol": "SBIN", "ltp": 110.0, "prev_close_price": 0},
        ),
        (
            {"symbol": "NSE:SBIN-EQ", "ch": 1.0, "change": 2.0, "vol_traded_today": 50},
            {
                "symbol": "SBIN",
                "ch": 1.0,
                "change": 2.0,
                "vol_traded_today": 50,
                "volume": 50,
            },
        ),
        (
            {"symbol": "NSE:SBIN-EQ", "v": 7, "volume": 9},
            {"symbol": "SBIN", "v": 7, "volume": 9},
        ),
    ],
)
def test_incoming_tick_is_normalised_into_latest_values(
    tick: dict[str, Any], expected: dict[str, Any], scheduled: list[Any]
) -> None:
    live = lms.LiveMarketService()
    loop = asyncio.new_event_loop()
    try:
        live.loop = loop
        live.handle_tick_incoming(tick)
    finally:
        loop.close()

    assert live.latest_values == {"SBIN": expected}
    assert scheduled == [("_update_buffer", loop)]


def test_tick_without_symbol_or_loop_is_ignored(scheduled: list[Any]) -> None:
    live = lms.LiveMarketService()
    live.handle_tick_incoming({"symbol": "NSE:SBIN-EQ", "ltp": 1.0})  # no loop yet
    loop = asyncio.new_event_loop()
    try:
        live.loop = loop
        live.handle_tick_incoming({"ltp": 1.0})
    finally:
        loop.close()

    assert live.latest_values == {}
    assert scheduled == []


# --- connect() outcomes --------------------------------------------------------


def test_connect_reports_already_connected() -> None:
    live = lms.LiveMarketService()
    live._is_connecting = True

    assert live.connect(prevalidated_token=True) == "already_connected"


def test_connect_reports_invalid_token() -> None:
    live = lms.LiveMarketService()

    assert live.connect(prevalidated_token=False) == "token_invalid"
    assert live._last_connect_block_reason == "token_invalid"


def test_connect_reports_market_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    live = lms.LiveMarketService()
    monkeypatch.setattr(live, "is_market_open", lambda: False)

    assert live.connect(prevalidated_token=True) == "market_closed"
    assert live._last_connect_block_reason == "market_closed"


def test_connect_adopts_an_already_connected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    live = lms.LiveMarketService()
    live.dev_mode = True
    provider = FakeProvider(connected=True)
    monkeypatch.setattr(fyers_websocket, "get_websocket_service", lambda: provider)

    assert live.connect(prevalidated_token=True) == "already_connected"
    assert live.ws_connected is True
    assert live._is_connecting is False
    assert provider.on_tick_handler == live.handle_tick_incoming
    assert provider.on_open_handler == live.on_provider_open


def test_connect_reports_error_when_provider_setup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    live = lms.LiveMarketService()
    live.dev_mode = True

    def broken() -> FakeProvider:
        raise RuntimeError("sdk missing")

    monkeypatch.setattr(fyers_websocket, "get_websocket_service", broken)

    assert live.connect(prevalidated_token=True) == "error"
    assert live._is_connecting is False


def test_connect_starts_monitor_and_flush_tasks_on_its_loop() -> None:
    async def scenario() -> tuple[bool, bool]:
        live = lms.LiveMarketService()
        live._is_connecting = True
        live.connect(prevalidated_token=True)
        tasks = (live.monitor_task, live.broadcast_task)
        started = all(t is not None and not t.done() for t in tasks)
        same_loop = live.loop is asyncio.get_running_loop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return started, same_loop

    assert asyncio.run(scenario()) == (True, True)


# --- subscribe / unsubscribe ---------------------------------------------------


def _live_with_fresh_registry(monkeypatch: pytest.MonkeyPatch) -> lms.LiveMarketService:
    monkeypatch.setattr(lms, "manager", ConnectionManager())
    return lms.LiveMarketService()


def _pending_count(live: lms.LiveMarketService) -> int:
    return asyncio.run(live.get_status())["pending_subscriptions"]


def test_subscribe_when_market_closed_queues_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    monkeypatch.setattr(live, "is_market_open", lambda: False)

    asyncio.run(live.subscribe_api(["NSE:SBIN-EQ", "TCS", ""]))

    assert live.ws_service is None
    assert live.registry.all_symbols() == {"SBIN", "TCS"}  # replayed on provider connect
    assert _pending_count(live) == 2


def test_subscribe_when_open_without_provider_connects_then_queues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    calls: list[str] = []

    async def fake_connect_async(**_kwargs: object) -> str:
        calls.append("connect")
        return "token_invalid"

    monkeypatch.setattr(live, "is_market_open", lambda: True)
    monkeypatch.setattr(live, "connect_async", fake_connect_async)

    asyncio.run(live.subscribe_api(["SBIN"]))

    assert calls == ["connect"]
    assert live.registry.all_symbols() == {"SBIN"}
    assert _pending_count(live) == 1


def test_subscribe_sends_fyers_symbols_to_connected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    live.ws_service = FakeProvider(connected=True)

    asyncio.run(live.subscribe_api(["SBIN", "NSE:TCS-EQ"]))

    assert sorted(live.ws_service.ws.subscribed) == sorted([_fyers("SBIN"), _fyers("TCS")])
    assert _pending_count(live) == 0


def test_subscribe_with_disconnected_provider_queues_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    live.ws_service = FakeProvider(connected=False)

    asyncio.run(live.subscribe_api(["SBIN"]))

    assert live.ws_service.ws.subscribed == []
    assert live.registry.all_symbols() == {"SBIN"}
    assert _pending_count(live) == 1


def test_unsubscribe_releases_only_unwanted_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    live.ws_service = FakeProvider(connected=True)
    live.registry.add(API_OWNER, ["TCS"])

    asyncio.run(live.unsubscribe(["SBIN", "TCS", ""]))

    assert live.ws_service.ws.unsubscribed == [_fyers("SBIN")]
    assert live.registry.all_symbols() == {"TCS"}


def test_unsubscribe_with_disconnected_provider_only_updates_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _live_with_fresh_registry(monkeypatch)
    live.ws_service = FakeProvider(connected=False)
    live.registry.add(API_OWNER, ["SBIN"])
    assert live.registry.remove(API_OWNER, ["SBIN"]) == {"SBIN"}

    asyncio.run(live.unsubscribe(["SBIN"]))

    assert live.ws_service.ws.unsubscribed == []
    assert live.registry.all_symbols() == set()
    assert _pending_count(live) == 0
