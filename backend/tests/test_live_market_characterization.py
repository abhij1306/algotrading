"""Characterization tests pinning LiveMarketService behaviour (monitor loop, tick
normalisation, connect outcomes, subscribe/unsubscribe) ahead of a refactor."""

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from app.services import live_market_service as lms
from app.services.live.provider_session import SessionState
from app.services.subscription_registry import API_OWNER
from app.services.symbol_master import symbol_master
from app.utils.ws_manager import ConnectionManager

from fakes.data_socket import FakeSocketFactory, make_live
from fakes.market import FreezeMarket


def setup_session(live: lms.LiveMarketService, factory: FakeSocketFactory, how: str) -> None:
    """Put the live service's provider session into the state named by `how`."""
    if how == "idle":
        return
    if how == "connecting":
        live.session._spawn = lambda _target: None  # a runner that never finishes
        assert live.session.start() is True
        return
    assert live.session.start() is True
    if how == "dropped":
        factory.last.drop()
    elif how == "silently_dropped":
        factory.last.connected = False  # the socket died without a callback


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
    ("market_open", "session", "expected"),
    [
        # (connect_async calls, session state after, provider socket closed)
        (True, "connected", (0, SessionState.CONNECTED, False)),
        (True, "silently_dropped", (1, SessionState.CONNECTED, False)),
        (True, "dropped", (1, SessionState.DROPPED, False)),
        (True, "connecting", (0, SessionState.CONNECTING, None)),
        (True, "idle", (1, SessionState.IDLE, None)),
        (False, "connected", (0, SessionState.IDLE, True)),
        (False, "dropped", (0, SessionState.IDLE, True)),
        (False, "connecting", (0, SessionState.IDLE, None)),
        (False, "idle", (0, SessionState.IDLE, None)),
    ],
)
def test_monitor_iteration_outcome(
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
    market_open: bool,
    session: str,
    expected: tuple[int, SessionState, bool | None],
) -> None:
    live, factory = make_live()
    setup_session(live, factory, session)
    freeze_market(market_open, live)

    connects = run_one_monitor_iteration(monkeypatch, live)

    closed = factory.last.closed if factory.sockets else None
    assert (len(connects), live.session.state, closed) == expected


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


@pytest.mark.parametrize("session", ["connecting", "connected"])
def test_connect_reports_already_connected(session: str) -> None:
    live, factory = make_live()
    setup_session(live, factory, session)

    assert live.connect(prevalidated_token=True) == "already_connected"
    assert len(factory.sockets) == (1 if session == "connected" else 0)


def test_connect_reports_invalid_token() -> None:
    live = lms.LiveMarketService()

    assert live.connect(prevalidated_token=False) == "token_invalid"
    assert live._last_connect_block_reason == "token_invalid"


def test_connect_reports_market_closed(
    monkeypatch: pytest.MonkeyPatch, freeze_market: FreezeMarket
) -> None:
    live = lms.LiveMarketService()
    freeze_market(False, live)

    assert live.connect(prevalidated_token=True) == "market_closed"
    assert live._last_connect_block_reason == "market_closed"


def test_connect_starts_the_provider_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lms, "manager", ConnectionManager())
    live, factory = make_live()
    live.dev_mode = True
    live.registry.add(API_OWNER, ["SBIN"])

    assert live.connect(prevalidated_token=True) == "started"
    assert live.session.is_connected() is True
    assert factory.last.subscribed == [_fyers("SBIN")]  # replayed on connect


def test_connect_reports_error_when_the_runner_cannot_start() -> None:
    live, _factory = make_live()
    live.dev_mode = True

    def broken(_target: object) -> None:
        raise RuntimeError("can't start new thread")

    live.session._spawn = broken

    assert live.connect(prevalidated_token=True) == "error"
    assert live.session.state is SessionState.FAILED
    assert live.session.busy is False


def test_connect_starts_monitor_and_flush_tasks_on_its_loop() -> None:
    async def scenario() -> tuple[bool, bool]:
        live, factory = make_live()
        setup_session(live, factory, "connecting")
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


def _live_with_fresh_registry(
    monkeypatch: pytest.MonkeyPatch, session: str = "idle"
) -> tuple[lms.LiveMarketService, FakeSocketFactory]:
    monkeypatch.setattr(lms, "manager", ConnectionManager())
    live, factory = make_live()
    setup_session(live, factory, session)
    return live, factory


def _pending_count(live: lms.LiveMarketService) -> int:
    return asyncio.run(live.get_status())["pending_subscriptions"]


def test_subscribe_when_market_closed_queues_symbols(
    monkeypatch: pytest.MonkeyPatch, freeze_market: FreezeMarket
) -> None:
    live, factory = _live_with_fresh_registry(monkeypatch)
    freeze_market(False, live)

    asyncio.run(live.subscribe_api(["NSE:SBIN-EQ", "TCS", ""]))

    assert live.session.state is SessionState.IDLE
    assert factory.sockets == []
    assert live.registry.all_symbols() == {"SBIN", "TCS"}  # replayed on provider connect
    assert _pending_count(live) == 2


def test_subscribe_when_open_without_provider_connects_then_queues(
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
) -> None:
    live, _factory = _live_with_fresh_registry(monkeypatch)
    calls: list[str] = []

    async def fake_connect_async(**_kwargs: object) -> str:
        calls.append("connect")
        return "token_invalid"

    freeze_market(True, live)
    monkeypatch.setattr(live, "connect_async", fake_connect_async)

    asyncio.run(live.subscribe_api(["SBIN"]))

    assert calls == ["connect"]
    assert live.registry.all_symbols() == {"SBIN"}
    assert _pending_count(live) == 1


def test_subscribe_sends_fyers_symbols_to_connected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, factory = _live_with_fresh_registry(monkeypatch, "connected")

    asyncio.run(live.subscribe_api(["SBIN", "NSE:TCS-EQ"]))

    assert sorted(factory.last.subscribed) == sorted([_fyers("SBIN"), _fyers("TCS")])
    assert _pending_count(live) == 0


def test_subscribe_with_disconnected_provider_queues_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, factory = _live_with_fresh_registry(monkeypatch, "dropped")

    asyncio.run(live.subscribe_api(["SBIN"]))

    assert factory.last.subscribed == []
    assert factory.last.connect_calls == 1  # no reconnect from subscribe
    assert live.registry.all_symbols() == {"SBIN"}
    assert _pending_count(live) == 1


def test_unsubscribe_releases_only_unwanted_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    live, factory = _live_with_fresh_registry(monkeypatch, "connected")
    live.registry.add(API_OWNER, ["TCS"])

    asyncio.run(live.unsubscribe(["SBIN", "TCS", ""]))

    assert factory.last.unsubscribed == [_fyers("SBIN")]
    assert live.registry.all_symbols() == {"TCS"}


def test_unsubscribe_with_disconnected_provider_only_updates_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, factory = _live_with_fresh_registry(monkeypatch, "dropped")
    live.registry.add(API_OWNER, ["SBIN"])
    assert live.registry.remove(API_OWNER, ["SBIN"]) == {"SBIN"}

    asyncio.run(live.unsubscribe(["SBIN"]))

    assert factory.last.unsubscribed == []
    assert live.registry.all_symbols() == set()
    assert _pending_count(live) == 0
