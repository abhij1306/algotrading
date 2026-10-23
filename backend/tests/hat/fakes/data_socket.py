"""FakeDataSocket: a DataSocket test double driven by the test instead of a network.

The test plays the SDK: `drop()`, `sdk_reconnect()`, `error()` and `emit()` fire the callbacks
the real SDK would fire. `FakeSocketFactory` records every socket and token it was given.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.hat.clock import SYSTEM_CLOCK, Clock
from app.hat.data.feed import LiveFeed
from app.hat.data.provider.data_socket import SYMBOL_UPDATE, SocketCallbacks
from app.hat.data.provider.provider_session import RetryPolicy
from app.hat.data.sessions import IST, SESSION_CLOSE, SESSION_OPEN
from app.hat.risk.sessions import SessionCalendar, SessionDay

# Short waits so runner retries and timeouts finish in milliseconds.
FAST_RETRY = RetryPolicy(
    max_attempts=3, connect_timeout_s=0.05, poll_s=0.005, backoff_base_s=0.01, backoff_max_s=0.02
)


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
        # Every subscribe (+) and unsubscribe (-) in the order the provider received it.
        self.events: list[tuple[str, str]] = []
        # One-shot hooks run before the provider receives the next call: a test plays another
        # thread whose own call reaches the provider first.
        self.before_subscribe: Callable[[], None] | None = None
        self.before_unsubscribe: Callable[[], None] | None = None

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
        hook, self.before_subscribe = self.before_subscribe, None
        if hook is not None:
            hook()
        self.subscribed.extend(symbols)
        self.events.extend(("+", s) for s in symbols)

    def unsubscribe(self, symbols: list[str], data_type: str = SYMBOL_UPDATE) -> None:
        hook, self.before_unsubscribe = self.before_unsubscribe, None
        if hook is not None:
            hook()
        self.unsubscribed.extend(symbols)
        self.events.extend(("-", s) for s in symbols)

    def net_subscribed(self) -> set[str]:
        """The symbols the provider streams now: each symbol's last event was a subscribe."""
        last: dict[str, str] = {}
        for op, symbol in self.events:
            last[symbol] = op
        return {symbol for symbol, op in last.items() if op == "+"}

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
        """The SDK's own auto-reconnect: same socket, on_connect fires again. The provider forgot
        the subscriptions of the lost connection (`net_subscribed` starts again from nothing)."""
        self.events.clear()
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


def weekday_calendar(clock: Clock, span: int = 400) -> SessionCalendar:
    """Calendar rows for every day within `span` days of the clock's IST date: weekdays trade
    09:15-15:30 IST, weekends are closed."""
    today = clock.now().astimezone(IST).date()
    days = {}
    for offset in range(-span, span + 1):
        day = today + timedelta(days=offset)
        days[day] = (
            SessionDay(
                True,
                datetime.combine(day, SESSION_OPEN, tzinfo=IST),
                datetime.combine(day, SESSION_CLOSE, tzinfo=IST),
            )
            if day.weekday() < 5
            else SessionDay(False)
        )
    return SessionCalendar(days)


def make_feed(
    factory: FakeSocketFactory | None = None,
    *,
    spawn: Callable[[Callable[[], None]], None] = run_inline,
    clock: Clock = SYSTEM_CLOCK,
    calendar: SessionCalendar | None = None,
    token_source: Callable[[], str | None] = lambda: "CLIENT:tok",
    token_valid: Callable[[], bool] = lambda: True,
) -> tuple[LiveFeed, FakeSocketFactory]:
    """A LiveFeed whose provider sockets are fakes (runner inline by default) and whose token
    check never calls Fyers."""
    factory = factory or FakeSocketFactory()
    feed = LiveFeed(
        calendar=calendar or weekday_calendar(clock),
        clock=clock,
        socket_factory=factory,
        token_source=token_source,
        validate_token=token_valid,
        retry=FAST_RETRY,
        spawn=spawn,
    )
    return feed, factory
