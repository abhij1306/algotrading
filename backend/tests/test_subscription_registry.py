"""SubscriptionRegistry: refcounted symbol ownership and the provider deltas it returns."""

import asyncio
import threading
from typing import Any

import pytest
from starlette.websockets import WebSocketState

from app.routers import websocket as ws_router
from app.services import live_market_service as lms
from app.services.subscription_registry import API_OWNER, SubscriptionRegistry
from app.services.symbol_master import symbol_master
from app.utils.ws_manager import ConnectionManager


def test_first_owner_of_a_symbol_needs_it_and_later_owners_do_not() -> None:
    registry = SubscriptionRegistry()

    assert registry.add("ws-1", ["SBIN", "TCS"]) == {"SBIN", "TCS"}
    assert registry.add("ws-1", ["SBIN"]) == set()  # same owner again: no new reference
    assert registry.add("ws-2", ["SBIN", "INFY"]) == {"INFY"}
    assert registry.all_symbols() == {"SBIN", "TCS", "INFY"}


def test_shared_symbol_stays_needed_until_its_last_owner_lets_go() -> None:
    registry = SubscriptionRegistry()
    registry.add("ws-1", ["SBIN"])
    registry.add("ws-2", ["SBIN"])

    assert registry.remove("ws-1", ["SBIN"]) == set()
    assert registry.owners_of("SBIN") == {"ws-2"}
    assert registry.remove("ws-2", ["SBIN"]) == {"SBIN"}
    assert registry.owners_of("SBIN") == set()
    assert registry.all_symbols() == set()


def test_removing_a_symbol_twice_or_one_never_held_releases_nothing() -> None:
    registry = SubscriptionRegistry()
    registry.add("ws-1", ["SBIN"])
    registry.add("ws-2", ["SBIN"])

    assert registry.remove("ws-1", ["SBIN", "TCS"]) == set()
    assert registry.remove("ws-1", ["SBIN"]) == set()  # ws-1 no longer holds it
    assert registry.remove("ws-9", ["SBIN"]) == set()  # unknown owner
    assert registry.owners_of("SBIN") == {"ws-2"}


def test_drop_owner_releases_only_symbols_no_other_owner_holds() -> None:
    registry = SubscriptionRegistry()
    registry.add("ws-1", ["SBIN", "TCS", "INFY"])
    registry.add("ws-2", ["SBIN"])
    registry.add(API_OWNER, ["TCS"])

    assert registry.drop_owner("ws-1") == {"INFY"}
    assert registry.symbols_of("ws-1") == frozenset()
    assert registry.drop_owner("ws-1") == set()  # already gone
    assert registry.all_symbols() == {"SBIN", "TCS"}


def test_symbols_are_canonicalised_to_db_format() -> None:
    registry = SubscriptionRegistry()

    assert registry.add("ws-1", ["NSE:SBIN-EQ", "", "nse:tcs-eq"]) == {"SBIN", "TCS"}
    assert registry.add("ws-2", ["SBIN"]) == set()
    assert registry.owners_of("NSE:SBIN-EQ") == {"ws-1", "ws-2"}
    assert registry.remove("ws-1", ["SBIN", "TCS"]) == {"TCS"}
    assert registry.remove("ws-2", ["NSE:SBIN-EQ"]) == {"SBIN"}


def test_replay_set_is_the_union_of_every_owners_symbols() -> None:
    registry = SubscriptionRegistry()
    registry.add("ws-1", ["SBIN", "TCS"])
    registry.add("ws-2", ["TCS", "INFY"])
    registry.add(API_OWNER, ["RELIANCE"])
    registry.remove("ws-2", ["INFY"])

    union = set().union(*(registry.symbols_of(o) for o in ("ws-1", "ws-2", API_OWNER)))
    assert registry.all_symbols() == union == {"SBIN", "TCS", "RELIANCE"}


def test_concurrent_add_and_remove_keep_refcounts_consistent() -> None:
    registry = SubscriptionRegistry()
    symbols = [f"SYM{i}" for i in range(50)]
    needed: list[set[str]] = []
    released: list[set[str]] = []
    record = threading.Lock()

    def churn(owner: str) -> None:
        for _ in range(200):
            added = registry.add(owner, symbols)
            dropped = registry.remove(owner, symbols)
            with record:
                needed.append(added)
                released.append(dropped)

    threads = [threading.Thread(target=churn, args=(f"ws-{n}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.all_symbols() == set()
    # Every symbol was needed exactly as often as it was released: no lost refcounts.
    for symbol in symbols:
        assert sum(symbol in s for s in needed) == sum(symbol in s for s in released)


# --- Slow client at the router/service level --------------------------------------


class FakeSocket:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    def is_connected(self) -> bool:
        return True


class FakeProvider:
    def __init__(self) -> None:
        self.ws = FakeSocket()

    def subscribe(self, symbols: list[str]) -> None:
        self.ws.subscribed.extend(symbols)

    def unsubscribe(self, symbols: list[str]) -> None:
        self.ws.unsubscribed.extend(symbols)


class FakeClientWebSocket:
    """Browser client; send_json blocks after the handshake when `stall` is set."""

    def __init__(self, stall: bool = False) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.stall = stall
        self.close_code: int | None = None
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.stall and data.get("type") not in ("connection_established", "ack"):
            await asyncio.Event().wait()
        self.sent.append(data)

    async def send_text(self, data: str) -> None:
        pass

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.client_state = WebSocketState.DISCONNECTED


def _fyers(symbols: list[str]) -> list[str]:
    return [symbol_master.to_fyers(s) for s in symbols]


def test_slow_client_drop_releases_its_symbols_exactly_when_no_other_owner_holds_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[FakeClientWebSocket, list[str], list[str], list[str]]:
        mgr = ConnectionManager()
        live = lms.LiveMarketService()
        live.ws_service = FakeProvider()
        mgr.on_symbols_released = live.unsubscribe
        monkeypatch.setattr(lms, "manager", mgr)
        monkeypatch.setattr(ws_router, "manager", mgr)

        slow = FakeClientWebSocket(stall=True)
        healthy = FakeClientWebSocket()
        assert await mgr.connect(slow)
        assert await mgr.connect(healthy)
        await live.subscribe_api(["RELIANCE"])
        await ws_router._stream_subscribe(slow, ["NSE:SBIN-EQ", "TCS", "RELIANCE", "INFY"], live)
        await ws_router._stream_subscribe(healthy, ["SBIN"], live)
        subscribed = list(live.ws_service.ws.subscribed)

        for _ in range(30):
            await mgr.broadcast({"type": "ticker_batch", "data": [{"symbol": "TCS", "ltp": 1}]})
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        released_on_drop = list(live.ws_service.ws.unsubscribed)

        # SBIN is released only once its last owner (the healthy client) lets go.
        await ws_router._stream_unsubscribe(healthy, ["SBIN"], live)
        return slow, subscribed, released_on_drop, live.ws_service.ws.unsubscribed

    slow, subscribed, released_on_drop, unsubscribed = asyncio.run(scenario())
    assert sorted(subscribed) == sorted(_fyers(["RELIANCE", "INFY", "SBIN", "TCS"]))
    assert slow.close_code == 1013
    assert sorted(released_on_drop) == sorted(_fyers(["INFY", "TCS"]))
    assert unsubscribed[len(released_on_drop) :] == _fyers(["SBIN"])
    assert lms.manager.registry.all_symbols() == {"RELIANCE"}


def test_symbols_subscribed_before_the_provider_connects_are_replayed_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> list[str]:
        mgr = ConnectionManager()
        monkeypatch.setattr(lms, "manager", mgr)
        monkeypatch.setattr(ws_router, "manager", mgr)
        live = lms.LiveMarketService()
        live.loop = asyncio.get_running_loop()
        monkeypatch.setattr(live, "is_market_open", lambda: False)

        client = FakeClientWebSocket()
        assert await mgr.connect(client)
        await ws_router._stream_subscribe(client, ["SBIN", "TCS"], live)  # no provider yet
        await ws_router._stream_unsubscribe(client, ["TCS"], live)
        await live.subscribe_api(["INFY"])

        live.ws_service = FakeProvider()  # provider comes up
        live.on_ws_connected()
        await asyncio.sleep(0.05)
        return live.ws_service.ws.subscribed

    assert sorted(asyncio.run(scenario())) == sorted(_fyers(["INFY", "SBIN"]))


def test_symbol_re_subscribed_while_its_release_is_in_flight_is_subscribed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = ConnectionManager()
    monkeypatch.setattr(lms, "manager", mgr)
    live = lms.LiveMarketService()

    class RacingProvider(FakeProvider):
        def unsubscribe(self, symbols: list[str]) -> None:
            super().unsubscribe(symbols)
            mgr.registry.add("ws-2", ["SBIN"])  # another client re-subscribes meanwhile

    live.ws_service = RacingProvider()
    asyncio.run(live.unsubscribe(["SBIN"]))

    assert live.ws_service.ws.unsubscribed == _fyers(["SBIN"])
    assert live.ws_service.ws.subscribed == _fyers(["SBIN"])


def test_client_dropped_by_its_own_sender_still_completes_the_release() -> None:
    class BrokenSendSocket(FakeClientWebSocket):
        async def send_text(self, data: str) -> None:
            raise RuntimeError("socket gone")

    async def scenario() -> list[list[str]]:
        mgr = ConnectionManager()
        released: list[list[str]] = []

        async def on_released(symbols: list[str]) -> None:
            await asyncio.sleep(0.01)  # a cancelled sender task would stop here
            released.append(symbols)

        mgr.on_symbols_released = on_released
        client = BrokenSendSocket()
        assert await mgr.connect(client)
        await mgr.subscribe(client, ["TCS"])
        await mgr.broadcast({"type": "ticker", "data": {"symbol": "TCS", "ltp": 1.0}})
        await asyncio.sleep(0.1)
        return released

    assert asyncio.run(scenario()) == [["TCS"]]
