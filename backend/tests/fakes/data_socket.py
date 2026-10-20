"""FakeDataSocket: a DataSocket test double driven by the test instead of a network.

The test plays the SDK: `drop()`, `sdk_reconnect()`, `error()` and `emit()` fire the callbacks
the real SDK would fire. `FakeSocketFactory` records every socket and token it was given.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.services.live.data_socket import SYMBOL_UPDATE, SocketCallbacks
from app.services.live.provider_session import ProviderSession, RetryPolicy
from app.services.live_market_service import LiveMarketService
from app.utils.clock import SYSTEM_CLOCK, Clock

# Short waits so runner retries and timeouts finish in milliseconds.
FAST_RETRY = RetryPolicy(
    max_attempts=3, connect_timeout_s=0.05, poll_s=0.005, backoff_base_s=0.01, backoff_max_s=0.02
)


def wait_until_backing_off(factory: FakeSocketFactory, timeout_s: float = 5.0) -> None:
    """Block until the runner's first attempt failed and released its socket (it then backs
    off). Fails the test if that does not happen in time."""
    deadline = time.monotonic() + timeout_s
    while not (factory.sockets and factory.sockets[0].closed):
        if time.monotonic() >= deadline:
            raise AssertionError("the connect runner never failed its first attempt")
        time.sleep(0.005)


def run_inline(target: Callable[[], None]) -> None:
    """Spawn for tests: run the connect runner on the calling thread."""
    target()


class FakeDataSocket:
    def __init__(
        self,
        token: str,
        callbacks: SocketCallbacks,
        *,
        comes_up: bool = True,
        fires_on_connect: bool = True,
    ) -> None:
        self.token = token
        self.callbacks = callbacks
        self.comes_up = comes_up
        self.fires_on_connect = fires_on_connect
        self.connected = False
        self.closed = False
        self.connect_calls = 0
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    # --- DataSocket ---

    def connect(self) -> None:
        self.connect_calls += 1
        self.connected = self.comes_up
        if self.fires_on_connect:
            self.callbacks.on_connect()  # the SDK fires on_connect from inside connect()

    def close_connection(self) -> None:
        self.closed = True
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(self, symbols: list[str], data_type: str = SYMBOL_UPDATE) -> None:
        self.subscribed.extend(symbols)

    def unsubscribe(self, symbols: list[str], data_type: str = SYMBOL_UPDATE) -> None:
        self.unsubscribed.extend(symbols)

    # --- the test plays the SDK ---

    def emit(self, message: dict[str, Any]) -> None:
        self.callbacks.on_message(message)

    def drop(self) -> None:
        """The connection closes for good (the SDK fires on_close)."""
        self.connected = False
        self.callbacks.on_close({"code": 200, "message": "Connection closed"})

    def error(self, message: str = "socket error", *, still_connected: bool = False) -> None:
        self.connected = still_connected
        self.callbacks.on_error({"code": -1, "message": message})

    def sdk_reconnect(self) -> None:
        """The SDK's own auto-reconnect: same socket, on_connect fires again."""
        self.connected = True
        self.callbacks.on_connect()


class FakeSocketFactory:
    """A SocketFactory recording each socket it builds; `fail` makes building raise."""

    def __init__(self, *, comes_up: bool = True, fires_on_connect: bool = True) -> None:
        self.comes_up = comes_up
        self.fires_on_connect = fires_on_connect
        self.fail: Exception | None = None
        self.sockets: list[FakeDataSocket] = []

    def __call__(self, token: str, callbacks: SocketCallbacks) -> FakeDataSocket:
        if self.fail is not None:
            raise self.fail
        socket = FakeDataSocket(
            token, callbacks, comes_up=self.comes_up, fires_on_connect=self.fires_on_connect
        )
        self.sockets.append(socket)
        return socket

    @property
    def last(self) -> FakeDataSocket:
        return self.sockets[-1]


def make_session(
    factory: FakeSocketFactory | None = None,
    *,
    desired: Callable[[], list[str]] = list,
    on_message: Callable[[dict[str, Any]], None] = lambda _m: None,
    token_source: Callable[[], str | None] = lambda: "CLIENT:tok",
    spawn: Callable[[Callable[[], None]], None] = run_inline,
) -> tuple[ProviderSession, FakeSocketFactory]:
    factory = factory or FakeSocketFactory()
    session = ProviderSession(
        socket_factory=factory,
        token_source=token_source,
        desired_symbols=desired,
        on_message=on_message,
        retry=FAST_RETRY,
        spawn=spawn,
    )
    return session, factory


def make_live(
    factory: FakeSocketFactory | None = None,
    *,
    spawn: Callable[[Callable[[], None]], None] = run_inline,
    clock: Clock = SYSTEM_CLOCK,
) -> tuple[LiveMarketService, FakeSocketFactory]:
    """A LiveMarketService whose provider sockets are fakes (runner inline by default)."""
    factory = factory or FakeSocketFactory()
    live = LiveMarketService(
        clock,
        socket_factory=factory,
        token_source=lambda: "CLIENT:tok",
        retry=FAST_RETRY,
        spawn=spawn,
    )
    return live, factory


def connected_live(clock: Clock = SYSTEM_CLOCK) -> tuple[LiveMarketService, FakeSocketFactory]:
    """make_live() with the provider session already connected (market hours not checked)."""
    live, factory = make_live(clock=clock)
    assert live.session.start() is True
    return live, factory
