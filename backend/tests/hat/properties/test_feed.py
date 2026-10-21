"""PBT feed.* (HAT-DATA-110 to 112, HAT-DATA-123 to 127, HAT-OVR-006; K3): the one provider socket
HAT owns. A state machine drives `ProviderSession` over a fake SDK socket through every connect,
drop, SDK auto-reconnect, stop and stale callback and checks that the process never holds more than
one open socket, replays every wanted symbol on each connect and ignores callbacks from sockets it
let go of; a second one checks the provider's subscriptions converge to the registry under races;
a third that the Fyers login hooks keep the socket on the current token and down after a logout.
Ticks, the registry and the tick transform are checked against their own definitions. No network.
"""

import asyncio
import copy
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from hat.fakes.data_socket import (
    FakeDataSocket,
    FakeSocketFactory,
    make_feed,
    make_session,
    wait_until_backing_off,
)
from hat.fakes.fyers_sdk import RecordingSession
from hat_fyers_support import Ticker, token_store
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app.hat.clock import FixedClock
from app.hat.data.provider.provider_session import RetryPolicy, SessionState
from app.hat.data.provider.ticks import normalize_tick
from app.hat.data.sessions import IST
from app.hat.data.subscriptions import API_OWNER, HAT_OWNER, SubscriptionRegistry
from app.hat.data.symbols import symbol_master
from app.hat.execution.fyers_auth import FyersAuthService, FyersCredentials

OPEN = datetime(2026, 10, 21, 10, 0, tzinfo=IST).astimezone(UTC)  # a Wednesday, mid-session
SATURDAY = datetime(2026, 10, 24, 10, 0, tzinfo=IST).astimezone(UTC)
PROVIDER_SYMBOLS = ["NSE:SBIN-EQ", "NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:RELIANCE-EQ", ""]
DB_SYMBOLS = ["SBIN", "TCS", "INFY", "RELIANCE", "nse:sbin-eq", "NSE:TCS-EQ", "", "LTIM"]


def _current(factory: FakeSocketFactory) -> FakeDataSocket | None:
    """The one socket not yet closed, if any."""
    live = [s for s in factory.sockets if not s.closed]
    assert len(live) <= 1, "more than one open provider socket"
    return live[0] if live else None


# --- the session (HAT-DATA-123) ---


class SessionMachine(RuleBasedStateMachine):
    """Any order of start, stop, close, drop, SDK auto-reconnect, errors, token changes and
    late callbacks from sockets the session already let go of."""

    def __init__(self) -> None:
        super().__init__()
        self.factory = FakeSocketFactory()
        self.wanted: list[str] = []
        self.token: str | None = "CLIENT:t0"
        self.tokens = 0
        self.received: list[dict[str, Any]] = []
        self.replay_raises = False
        self.sink_raises = False
        self.session, _ = make_session(
            self.factory,
            desired=self._desired,
            on_message=self._sink,
            token_source=lambda: self.token,
        )

    def _desired(self) -> list[str]:
        if self.replay_raises:
            raise RuntimeError("registry unavailable")
        return list(self.wanted)

    def _sink(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        if self.sink_raises:
            raise ValueError("bad tick")

    def _wanted_now(self) -> set[str]:
        return {s for s in self.wanted if s}

    @rule()
    def start(self) -> None:
        before = {id(s): len(s.subscribed) for s in self.factory.sockets}
        if not self.session.start():
            assert self.session.busy
            return
        if self.token is None or self.factory.fail is not None:
            assert self.session.state is SessionState.FAILED
            assert not self.session.has_socket
            return
        socket = self.factory.last
        assert self.session.state is SessionState.CONNECTED
        assert socket.token == self.token  # a re-login reaches the next socket
        if not self.replay_raises:
            assert self._wanted_now() <= set(socket.subscribed[before.get(id(socket), 0) :])

    @rule()
    def stop(self) -> None:
        self.session.stop()
        assert self.session.state is SessionState.IDLE

    @rule()
    def close(self) -> None:
        self.session.close()
        assert self.session.state is SessionState.CLOSED

    @precondition(lambda self: _current(self.factory) is not None)
    @rule()
    def drop(self) -> None:
        socket = _current(self.factory)
        assert socket is not None
        was = self.session.state
        socket.drop()
        assert self.session.state is (
            SessionState.DROPPED if was is SessionState.CONNECTED else was
        )

    @precondition(lambda self: _current(self.factory) is not None)
    @rule()
    def sdk_reconnect(self) -> None:
        socket = _current(self.factory)
        assert socket is not None
        before = len(socket.subscribed)
        socket.sdk_reconnect()
        assert self.session.state is SessionState.CONNECTED
        if not self.replay_raises:
            assert self._wanted_now() <= set(socket.subscribed[before:])

    @precondition(lambda self: any(s.closed for s in self.factory.sockets))
    @rule(data=st.data())
    def late_callbacks_of_a_closed_socket(self, data: st.DataObject) -> None:
        stale = data.draw(st.sampled_from([s for s in self.factory.sockets if s.closed]))
        state = self.session.state
        counts = [len(s.subscribed) + len(s.unsubscribed) for s in self.factory.sockets]
        received = len(self.received)
        stale.sdk_reconnect()
        stale.emit({"symbol": "NSE:SBIN-EQ"})
        stale.drop()
        stale.error()
        assert self.session.state is state
        assert len(self.received) == received
        assert counts == [len(s.subscribed) + len(s.unsubscribed) for s in self.factory.sockets]
        stale.connected = False

    @precondition(lambda self: _current(self.factory) is not None)
    @rule(still_connected=st.booleans())
    def error(self, still_connected: bool) -> None:
        socket = _current(self.factory)
        assert socket is not None
        was = self.session.state
        socket.error(still_connected=still_connected)
        if was is SessionState.CONNECTED and not still_connected:
            assert self.session.state is SessionState.DROPPED
        else:
            assert self.session.state is was  # a non-fatal SDK error (a bad symbol) changes nothing

    @precondition(lambda self: _current(self.factory) is not None)
    @rule()
    def silent_drop(self) -> None:
        socket = _current(self.factory)
        assert socket is not None
        socket.connected = False  # no callback at all
        assert self.session.is_connected() is False
        assert self.session.busy is False

    @rule()
    def emit(self) -> None:
        socket = _current(self.factory)
        if socket is None:
            return
        before = len(self.received)
        socket.emit({"symbol": "NSE:SBIN-EQ", "n": before})
        assert len(self.received) == before + 1

    @rule()
    def subscribe_directly(self) -> None:
        socket = _current(self.factory)
        sent = self.session.subscribe(["NSE:ZZZ-EQ"])
        assert sent == (self.session.state is SessionState.CONNECTED and socket is not None)
        if sent and socket is not None:
            assert "NSE:ZZZ-EQ" in socket.subscribed

    @rule(wanted=st.lists(st.sampled_from(PROVIDER_SYMBOLS), max_size=6))
    def want(self, wanted: list[str]) -> None:
        self.wanted = wanted

    @rule(logged_in=st.booleans())
    def log_in_or_out(self, logged_in: bool) -> None:
        self.tokens += 1
        self.token = f"CLIENT:t{self.tokens}" if logged_in else None

    @rule(broken=st.booleans())
    def sdk_unavailable(self, broken: bool) -> None:
        self.factory.fail = RuntimeError("fyers-apiv3 not installed") if broken else None

    @rule(replay=st.booleans(), sink=st.booleans())
    def break_callbacks(self, replay: bool, sink: bool) -> None:
        self.replay_raises = replay
        self.sink_raises = sink

    @invariant()
    def at_most_one_open_socket_and_none_leaked(self) -> None:
        """The session holds one socket exactly when it says so; every other one is closed."""
        open_sockets = [s for s in self.factory.sockets if not s.closed]
        assert len(open_sockets) == (1 if self.session.has_socket else 0)

    @invariant()
    def state_agrees_with_the_socket(self) -> None:
        state = self.session.state
        if state in (SessionState.IDLE, SessionState.CLOSED, SessionState.FAILED):
            assert not self.session.has_socket
        if state is SessionState.CONNECTED:
            assert self.session.has_socket
        if self.session.is_connected():
            assert self.session.has_socket


def test_session_one_socket() -> None:
    """HAT-OVR-006, HAT-DATA-123: for every order of start, stop, close, drop, SDK auto-reconnect,
    error, token change and late callback, the session never holds more than one open socket and
    leaks none, replays every wanted symbol on each connect (a failing replay never fails the
    connect), builds each socket from the current token, delivers messages only from its current
    socket and keeps its state consistent with the socket. Harm: a second socket to the same broker
    breaks the session limit, or a symbol is silently never streamed after a reconnect."""
    run_state_machine_as_test(
        SessionMachine,
        settings=settings(max_examples=40, stateful_step_count=25, deadline=None),
    )


@settings(max_examples=30, deadline=None)
@given(
    mode=st.sampled_from(["never_up", "factory_fails", "logged_out"]),
    attempts=st.integers(1, 4),
)
def test_session_gives_up(mode: str, attempts: int) -> None:
    """The connect runner makes exactly `max_attempts` attempts, then the session is FAILED with
    no socket left open and not busy, whether the socket never comes up, the SDK is missing or
    nobody is logged in. Harm: a dead provider keeps a runner or a socket alive for ever, or the
    feed gives up too early."""
    factory = FakeSocketFactory(comes_up=mode != "never_up")
    if mode == "factory_fails":
        factory.fail = RuntimeError("fyers-apiv3 not installed")
    calls: list[int] = []

    def token_source() -> str | None:
        calls.append(1)
        return None if mode == "logged_out" else "CLIENT:tok"

    retry = RetryPolicy(
        max_attempts=attempts,
        connect_timeout_s=0.02,
        poll_s=0.002,
        backoff_base_s=0.001,
        backoff_max_s=0.002,
    )
    session, _ = make_session(factory, token_source=token_source, retry=retry)
    assert session.start() is True
    assert session.state is SessionState.FAILED
    assert len(calls) == attempts
    assert len(factory.sockets) == (attempts if mode == "never_up" else 0)
    assert all(s.closed for s in factory.sockets)
    assert session.busy is False
    assert session.has_socket is False


@settings(max_examples=8, deadline=None)
@given(late_reconnects=st.integers(0, 3))
def test_session_close_stops_a_backing_off_runner(late_reconnects: int) -> None:
    """An explicit close while the runner backs off stops it: no further socket is built, a late SDK
    auto-reconnect of the failed socket neither connects nor replays, and the state ends CLOSED.
    Harm: after a logout a runner in flight reconnects with the old token."""
    factory = FakeSocketFactory(comes_up=False)
    runner: list[Callable[[], None]] = []
    retry = RetryPolicy(max_attempts=10, connect_timeout_s=0.02, poll_s=0.005, backoff_base_s=30.0)
    session, _ = make_session(
        factory, spawn=runner.append, desired=lambda: ["NSE:SBIN-EQ"], retry=retry
    )
    assert session.start() is True
    thread = threading.Thread(target=runner[0], daemon=True)
    thread.start()
    wait_until_backing_off(factory)
    for _ in range(late_reconnects):
        factory.last.sdk_reconnect()
        assert session.state is SessionState.CONNECTING
        assert factory.last.subscribed == []
    session.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(factory.sockets) == 1
    assert session.state is SessionState.CLOSED


# --- ticks (HAT-DATA-111, HAT-DATA-124) ---

numbers = st.one_of(
    st.integers(-10_000, 10_000),
    st.floats(-10_000, 10_000, allow_nan=False, allow_infinity=False),
)
TICK_FIELDS = [
    "ltp",
    "chp",
    "ch",
    "v",
    "vol_traded_today",
    "volume",
    "change",
    "change_pct",
    "prev_close_price",
    "other",
]
ticks = st.dictionaries(st.sampled_from(TICK_FIELDS), numbers, max_size=10).map(
    lambda fields: {"symbol": "NSE:SBIN-EQ", **fields}
)


@given(tick=ticks)
def test_tick_normalised(tick: dict[str, Any]) -> None:
    """For every Fyers tick: the input is not modified; the output is keyed by the database
    symbol; every other input field is kept, and only change_pct, change and volume are ever added;
    change_pct follows chp when Fyers sends it, else is derived from the previous close when that
    is positive, else is left as it was; change and volume only fill what is missing (v before
    vol_traded_today).
    Harm: a consumer sees a wrong percentage or a missing volume, or the transform mutates the tick
    other listeners hold."""
    original = copy.deepcopy(tick)
    out = normalize_tick(tick, "SBIN")
    assert tick == original
    assert out["symbol"] == "SBIN"
    assert {
        k: v for k, v in out.items() if k not in ("symbol", "change_pct", "change", "volume")
    } == {k: v for k, v in tick.items() if k not in ("symbol", "change_pct", "change", "volume")}
    assert set(out) - set(tick) <= {"change_pct", "change", "volume"}
    previous = tick.get("prev_close_price")
    if "chp" in tick:
        assert out["change_pct"] == tick["chp"]
    elif "ltp" in tick and previous and previous > 0:
        assert out["change_pct"] == ((tick["ltp"] - previous) / previous) * 100
    else:
        assert out.get("change_pct") == tick.get("change_pct")
    assert out.get("change") == (tick["change"] if "change" in tick else tick.get("ch"))
    for name in ("volume", "v", "vol_traded_today"):
        if name in tick:
            assert out["volume"] == tick[name]
            break
    else:
        assert "volume" not in out


provider_symbols = st.sampled_from(
    ["NSE:SBIN-EQ", "nse:tcs-eq", "SBIN", "LTIM", "NSE:NIFTYBANK-INDEX", "NSE:RELIANCE-EQ"]
)
tick_messages = st.builds(
    lambda symbol, ltp, seq: {"symbol": symbol, "ltp": ltp, "v": seq, "seq": seq},
    provider_symbols,
    st.floats(1, 5000, allow_nan=False),
    st.integers(0, 10**6),
)
ack_messages = st.sampled_from(
    [{"type": "cn", "code": 200, "message": "authentication done"}, {"symbol": ""}, {}]
)


@settings(max_examples=25, deadline=None)
@given(
    messages=st.lists(st.one_of(tick_messages, ack_messages), max_size=12),
    with_loop=st.booleans(),
)
def test_tick_flow(messages: list[dict[str, Any]], with_loop: bool) -> None:
    """From the provider socket to the listeners, for every message sequence: each message with a
    symbol reaches every listener once, in order, normalised and keyed by database symbol, on the
    event-loop thread although the SDK thread emitted it; acks and symbol-less messages are dropped;
    with no loop nothing is delivered and nothing raises. Harm: a trigger sees ticks out of order,
    under a symbol HAT does not know, or on a thread that is not the loop's."""

    async def scenario() -> None:
        feed, factory = make_feed(clock=FixedClock(OPEN))
        feed.loop = asyncio.get_running_loop() if with_loop else None
        seen: list[dict[str, Any]] = []
        threads: set[int] = set()
        feed.add_tick_listener(lambda t: (seen.append(t), threads.add(threading.get_ident())))
        assert feed.connect(explicit=True) == "started"
        socket = factory.last
        await asyncio.to_thread(lambda: [socket.emit(m) for m in messages])
        await asyncio.sleep(0.01)
        expected = [
            normalize_tick(m, symbol_master.to_db(m["symbol"]))
            for m in messages
            if m.get("symbol") and with_loop
        ]
        assert seen == expected
        assert threads <= {threading.get_ident()}
        assert all(":" not in t["symbol"] for t in seen)

    asyncio.run(scenario())


# --- the registry (HAT-DATA-112, HAT-DATA-125) ---

OWNERS = ["ws-1", "ws-2", API_OWNER, HAT_OWNER]
REGISTRY_SYMBOLS = ["SBIN", "TCS", "INFY", "nse:sbin-eq", "NSE:TCS-EQ", "", "reliance"]


class RegistryMachine(RuleBasedStateMachine):
    """The registry against a model of who holds what, and a model provider that applies each
    returned delta."""

    def __init__(self) -> None:
        super().__init__()
        self.registry = SubscriptionRegistry(symbol_master.to_db)
        self.held: dict[str, set[str]] = {}
        self.provider: set[str] = set()

    @staticmethod
    def _canonical(symbols: list[str]) -> set[str]:
        return {symbol_master.to_db(s) for s in symbols if s} - {""}

    def _others(self, owner: str) -> set[str]:
        return set().union(*(v for o, v in self.held.items() if o != owner))

    @rule(owner=st.sampled_from(OWNERS), symbols=st.lists(st.sampled_from(REGISTRY_SYMBOLS)))
    def add(self, owner: str, symbols: list[str]) -> None:
        wanted = self._canonical(symbols)
        everyone = set().union(set(), *self.held.values())
        expected = wanted - everyone
        delta = self.registry.add(owner, symbols)
        assert delta == expected
        assert not delta & self.provider  # never subscribe what the provider already streams
        self.provider |= delta
        self.held.setdefault(owner, set()).update(wanted)

    @rule(owner=st.sampled_from(OWNERS), symbols=st.lists(st.sampled_from(REGISTRY_SYMBOLS)))
    def remove(self, owner: str, symbols: list[str]) -> None:
        unwanted = self._canonical(symbols)
        mine = self.held.get(owner, set())
        expected = (mine & unwanted) - self._others(owner)
        delta = self.registry.remove(owner, symbols)
        assert delta == expected
        assert delta <= self.provider  # never unsubscribe what the provider does not stream
        self.provider -= delta
        mine -= unwanted
        if not mine:
            self.held.pop(owner, None)

    @rule(owner=st.sampled_from(OWNERS))
    def drop_owner(self, owner: str) -> None:
        mine = self.held.pop(owner, set())
        expected = mine - self._others(owner)
        delta = self.registry.drop_owner(owner)
        assert delta == expected
        assert delta <= self.provider
        self.provider -= delta

    @invariant()
    def registry_equals_the_model(self) -> None:
        union = set().union(set(), *self.held.values())
        assert self.registry.all_symbols() == union == self.provider
        for owner in OWNERS:
            assert self.registry.symbols_of(owner) == frozenset(self.held.get(owner, set()))
        for symbol in ("SBIN", "NSE:TCS-EQ", "nse:sbin-eq", "INFY", "RELIANCE"):
            canonical = symbol_master.to_db(symbol)
            owners = {o for o, held in self.held.items() if canonical in held}
            assert self.registry.owners_of(symbol) == owners
            assert self.registry.is_held(symbol) == bool(owners)


def test_registry_model() -> None:
    """For every sequence of add, remove and drop-owner by any owners, in any symbol format: the
    registry's symbol set is the union of the owners' sets, each returned delta is exactly the
    symbols whose owner count went from zero to one (subscribe) or one to zero (unsubscribe), so a
    provider that applies the deltas always streams exactly the registry's set and is never told
    to subscribe a streaming symbol or to drop an unstreamed one. Harm: releasing HAT's symbols
    unsubscribes one a websocket client still holds, or a symbol is never subscribed."""
    run_state_machine_as_test(
        RegistryMachine, settings=settings(max_examples=60, stateful_step_count=30, deadline=None)
    )


@settings(max_examples=6, deadline=None)
@given(threads=st.integers(2, 6), rounds=st.integers(50, 300))
def test_registry_concurrent(threads: int, rounds: int) -> None:
    """With any number of threads adding and removing the same symbols, the registry ends empty and
    each symbol was returned as needed exactly as often as it was returned as released: no lost
    reference. Harm: a symbol is stuck subscribed for ever, or released while an owner holds it."""
    registry = SubscriptionRegistry(symbol_master.to_db)
    symbols = [f"SYM{i}" for i in range(12)]
    needed: list[set[str]] = []
    released: list[set[str]] = []
    record = threading.Lock()

    def churn(owner: str) -> None:
        for _ in range(rounds):
            added = registry.add(owner, symbols)
            dropped = registry.remove(owner, symbols)
            with record:
                needed.append(added)
                released.append(dropped)

    workers = [threading.Thread(target=churn, args=(f"ws-{n}",)) for n in range(threads)]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads often, so an unguarded update is caught
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    finally:
        sys.setswitchinterval(interval)
    assert registry.all_symbols() == set()
    for symbol in symbols:
        assert sum(symbol in s for s in needed) == sum(symbol in s for s in released)


# --- the provider follows the registry (HAT-DATA-126) ---

CONVERGE_SYMBOLS = ["SBIN", "TCS", "INFY", "RELIANCE", "BAD#SYM"]


def _provider_set(symbols: set[str]) -> set[str]:
    out = set()
    for symbol in symbols:
        try:
            out.add(symbol_master.to_fyers(symbol))
        except ValueError:
            continue  # a symbol with no provider form is never sent
    return out


class ConvergeMachine(RuleBasedStateMachine):
    """Owners add and remove symbols while the socket connects, drops and reconnects, and another
    thread's registry change and provider call reach the provider just before ours."""

    def __init__(self) -> None:
        super().__init__()
        self.feed, self.factory = make_feed(clock=FixedClock(OPEN))

    def _race(self, owner: str, symbols: list[str], *, release: bool) -> Callable[[], None]:
        feed = self.feed

        def racer() -> None:
            if release:
                delta = feed.registry.remove(owner, symbols)
                if delta:
                    feed.session.unsubscribe(sorted(_provider_set(delta)))
            else:
                delta = feed.registry.add(owner, symbols)
                if delta:
                    feed.session.subscribe(sorted(_provider_set(delta)))

        return racer

    @rule()
    def connect(self) -> None:
        self.feed.connect(explicit=True)

    @precondition(lambda self: _current(self.factory) is not None)
    @rule()
    def drop(self) -> None:
        socket = _current(self.factory)
        assert socket is not None
        socket.drop()

    @precondition(lambda self: _current(self.factory) is not None)
    @rule()
    def sdk_reconnect(self) -> None:
        socket = _current(self.factory)
        assert socket is not None
        socket.sdk_reconnect()

    @rule(owner=st.sampled_from(OWNERS), symbols=st.lists(st.sampled_from(CONVERGE_SYMBOLS)))
    def add(self, owner: str, symbols: list[str]) -> None:
        delta = self.feed.registry.add(owner, symbols)
        if delta:
            asyncio.run(self.feed.subscribe(sorted(delta)))

    @rule(owner=st.sampled_from(OWNERS), symbols=st.lists(st.sampled_from(CONVERGE_SYMBOLS)))
    def remove(self, owner: str, symbols: list[str]) -> None:
        delta = self.feed.registry.remove(owner, symbols)
        if delta:
            asyncio.run(self.feed.unsubscribe(sorted(delta)))

    @rule(owner=st.sampled_from(OWNERS))
    def drop_owner(self, owner: str) -> None:
        delta = self.feed.registry.drop_owner(owner)
        if delta:
            asyncio.run(self.feed.unsubscribe(sorted(delta)))

    @precondition(lambda self: _current(self.factory) is not None)
    @rule(
        owner=st.sampled_from(OWNERS),
        symbols=st.lists(st.sampled_from(CONVERGE_SYMBOLS), min_size=1),
        release=st.booleans(),
        during=st.sampled_from(["subscribe", "unsubscribe"]),
    )
    def arm_race(self, owner: str, symbols: list[str], release: bool, during: str) -> None:
        """The next provider call of this kind is overtaken by another owner's change."""
        socket = _current(self.factory)
        assert socket is not None
        racer = self._race(owner, symbols, release=release)
        if during == "subscribe":
            socket.before_subscribe = racer
        else:
            socket.before_unsubscribe = racer

    @invariant()
    def the_provider_streams_what_the_registry_holds(self) -> None:
        if not self.feed.is_connected():
            return
        socket = _current(self.factory)
        assert socket is not None
        assert socket.net_subscribed() == _provider_set(self.feed.registry.all_symbols())


def test_provider_converges() -> None:
    """For every interleaving of owners adding and removing symbols with the socket connecting,
    dropping and auto-reconnecting, and with another owner's registry change and provider call
    overtaking ours (a release during a subscribe, a re-subscribe during an unsubscribe, either
    during a replay), a connected provider streams exactly the symbols the registry holds. Harm:
    an open position stops receiving prices, or the symbol limit fills with symbols nobody wants."""
    run_state_machine_as_test(
        ConvergeMachine, settings=settings(max_examples=40, stateful_step_count=20, deadline=None)
    )


# --- the Fyers login drives the feed (HAT-DATA-127) ---

CREDS = FyersCredentials("APP-100", "SECRET", "https://app.example.test/cb")


class AuthMachine(RuleBasedStateMachine):
    """Real `FyersAuthService` login, logout and token-change hooks, a token file, market hours
    and the monitor's reconnect, in any order."""

    def __init__(self) -> None:
        super().__init__()
        self._store_cm = token_store()
        self.store = self._store_cm.__enter__()
        self.clock = FixedClock(OPEN)
        self.logins = 0
        self.reply: dict[str, Any] = {}
        self.service = FyersAuthService(
            self.store,
            session_factory=lambda **kw: RecordingSession(self.reply, **kw),
            credentials=lambda: CREDS,
            on_token_change=lambda: None,
            clock=Ticker(),
        )
        self.feed, self.factory = make_feed(
            clock=self.clock,
            token_source=self._token,
            token_valid=lambda: self._token() is not None,
        )
        self.feed.install_auth_hooks(self.service)
        self.feed.install_auth_hooks(self.service)  # twice: each hook still runs once

    def teardown(self) -> None:
        self._store_cm.__exit__(None, None, None)

    def _token(self) -> str | None:
        stored = self.store.load()
        return f"{stored.client_id}:{stored.access_token}" if stored else None

    @rule()
    def login(self) -> None:
        self.logins += 1
        token = f"TKN{self.logins:04d}abcdef0123456789"
        self.reply = {"s": "ok", "code": 200, "access_token": token, "fy_id": "XA1234"}
        self.service.exchange("AUTHCODE0123456789")
        assert self.feed.explicitly_disconnected is False
        if self.feed.connection_allowed():
            socket = _current(self.factory)
            assert socket is not None
            assert socket.token == f"APP-100:{token}"
            assert self.feed.state is SessionState.CONNECTED

    @rule()
    def logout(self) -> None:
        self.service.disconnect()
        assert _current(self.factory) is None
        assert self.feed.explicitly_disconnected is True

    @rule()
    def token_changed_alone(self) -> None:
        self.service._run_hooks("token_change")  # noqa: SLF001 - a refresh with no login
        assert _current(self.factory) is None  # the socket carried the old token

    @rule()
    def monitor_connect(self) -> None:
        before = len(self.factory.sockets)
        status = self.feed.connect()
        if self.feed.explicitly_disconnected:
            assert status == "disconnected"
            assert len(self.factory.sockets) == before

    @rule(is_open=st.booleans())
    def market_hours_change(self, is_open: bool) -> None:
        self.clock.set(OPEN if is_open else SATURDAY)
        asyncio.run(self.feed.check_health())
        if not is_open:
            assert _current(self.factory) is None

    @invariant()
    def one_socket_on_the_current_token_and_none_after_a_logout(self) -> None:
        socket = _current(self.factory)
        if self.feed.explicitly_disconnected:
            assert socket is None
        if socket is not None:
            assert socket.token == self._token()


def test_auth_hooks() -> None:
    """For every order of Fyers login, logout, token change, monitor reconnect and market-hours
    change: the process holds at most one provider socket and it is built from the token on file
    (never an earlier one); a login with the market open leaves it connected; a logout closes it and
    nothing reconnects until the next login; a token change alone drops the old socket; each hook
    runs once however often it is registered. Harm: after a re-login the feed streams on a dead
    token, or after a logout it reconnects on its own."""
    run_state_machine_as_test(
        AuthMachine, settings=settings(max_examples=30, stateful_step_count=20, deadline=None)
    )


# --- the health monitor and the token check (HAT-DATA-118, HAT-DATA-127) ---


@settings(max_examples=8, deadline=None)
@given(late_reconnects=st.integers(0, 2))
def test_off_hours_stops_a_backing_off_runner(late_reconnects: int) -> None:
    """When the market closes while the connect runner backs off, the monitor's check stops it: no
    further socket is built, the state is IDLE and the feed is not marked explicitly disconnected,
    so the next session connects again. Harm: the runner reconnects all night, or the next morning
    the feed stays down."""
    factory = FakeSocketFactory(comes_up=False)
    runner: list[Callable[[], None]] = []
    clock = FixedClock(OPEN)
    feed, _ = make_feed(factory, spawn=runner.append, clock=clock)
    feed.session.retry = RetryPolicy(
        max_attempts=10, connect_timeout_s=0.02, poll_s=0.005, backoff_base_s=30.0
    )
    assert feed.connect(explicit=True) == "started"
    thread = threading.Thread(target=runner[0], daemon=True)
    thread.start()
    wait_until_backing_off(factory)
    for _ in range(late_reconnects):
        factory.last.sdk_reconnect()  # a failed socket the runner let go of
    clock.set(SATURDAY)
    asyncio.run(feed.check_health())
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(factory.sockets) == 1
    assert feed.state is SessionState.IDLE
    assert feed.explicitly_disconnected is False
    clock.set(OPEN)  # the next session
    runner.clear()
    assert feed.connect(prevalidated_token=True) == "started"


@settings(max_examples=10, deadline=None)
@given(failing=st.sets(st.integers(0, 7), max_size=8))
def test_monitor_survives_errors(failing: set[int]) -> None:
    """For every set of monitor checks that raise, the monitor loop keeps running and keeps
    checking. Harm: one unexpected error silently ends reconnects for the rest of the day."""
    import time
    from unittest.mock import patch

    from app.hat.data import feed as feed_module

    feed, _ = make_feed()
    calls: list[int] = []

    async def check() -> None:
        index = len(calls)
        calls.append(index)
        if index in failing:
            raise RuntimeError("boom")

    feed.check_health = check  # type: ignore[method-assign]

    async def scenario() -> bool:
        task = asyncio.create_task(feed._monitor_loop())  # noqa: SLF001
        deadline = time.monotonic() + 5
        while len(calls) < 10 and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        alive = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return alive

    with (
        patch.object(feed_module, "MONITOR_INTERVAL_S", 0.001),
        patch.object(feed_module, "LOOP_ERROR_PAUSE_S", 0.001),
    ):
        alive = asyncio.run(scenario())
    assert alive
    assert len(calls) >= 10


@settings(max_examples=20, deadline=None)
@given(valid=st.booleans(), is_open=st.booleans(), explicit=st.booleans(), raises=st.booleans())
def test_token_check_off_loop(valid: bool, is_open: bool, explicit: bool, raises: bool) -> None:
    """`connect_async` runs the token check off the event loop, and the status is token_invalid when
    the check says so or raises, market_closed when the token is fine but the market is shut, and
    started otherwise. Harm: a network call to Fyers blocks every tick listener, or a failing
    check crashes the monitor."""
    seen: list[int] = []

    def check() -> bool:
        seen.append(threading.get_ident())
        if raises:
            raise TimeoutError("fyers is slow")
        return valid

    feed, _ = make_feed(clock=FixedClock(OPEN if is_open else SATURDAY), token_valid=check)

    async def scenario() -> tuple[str, int]:
        return await feed.connect_async(explicit=explicit), threading.get_ident()

    status, loop_thread = asyncio.run(scenario())
    assert seen
    assert set(seen) == {seen[0]}
    assert seen[0] != loop_thread
    if raises or not valid:
        assert status == "token_invalid"
    else:
        assert status == ("started" if is_open else "market_closed")


# --- the SDK socket factory and the token source (HAT-DATA-123) ---

tokens = st.from_regex(r"[A-Z0-9]{3,10}:[A-Za-z0-9._-]{5,30}", fullmatch=True)


@given(token=tokens)
def test_socket_factory(token: str) -> None:
    """For every token, the factory builds the SDK socket with that token, the SDK's own
    auto-reconnect on, no SDK log files, and exactly the session's four callbacks; without the SDK
    it raises `FyersWebSocketError` and builds nothing. Harm: a reconnect storm, files written
    next to the code, or a socket whose events never reach the session."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.hat.data.provider import fyers_websocket
    from app.hat.data.provider.data_socket import SocketCallbacks

    built: list[dict[str, object]] = []

    def make_socket(**kwargs: object) -> object:
        built.append(kwargs)
        return SimpleNamespace()

    callbacks = SocketCallbacks(
        on_message=lambda _m: None,
        on_connect=lambda: None,
        on_close=lambda _m: None,
        on_error=lambda _e: None,
    )
    with patch.object(fyers_websocket, "data_ws", SimpleNamespace(FyersDataSocket=make_socket)):
        fyers_websocket.fyers_socket_factory(token, callbacks)
    (kwargs,) = built
    assert kwargs["access_token"] == token
    assert kwargs["reconnect"] is True
    assert kwargs["write_to_file"] is False
    assert kwargs["log_path"] == ""
    assert kwargs["on_message"] is callbacks.on_message
    assert kwargs["on_connect"] is callbacks.on_connect
    assert kwargs["on_close"] is callbacks.on_close
    assert kwargs["on_error"] is callbacks.on_error
    with patch.object(fyers_websocket, "data_ws", None):
        with pytest.raises(fyers_websocket.FyersWebSocketError):
            fyers_websocket.fyers_socket_factory(token, callbacks)
        assert len(built) == 1


maybe_text = st.one_of(st.none(), st.just(""), st.text("abcXYZ019:", min_size=1, max_size=12))


@given(client_id=maybe_text, access_token=maybe_text, has_model=st.booleans())
def test_token_source(client_id: str | None, access_token: str | None, has_model: bool) -> None:
    """The provider token is "client_id:access_token" exactly when the Fyers session holds a usable
    SDK model, a client id and an access token, and None otherwise (nobody is logged in, or the
    token expired). Harm: the socket is built with half a token and Fyers locks the app out, or an
    expired token is retried for ever."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.hat.data.provider.fyers_websocket import fyers_token

    session = SimpleNamespace(
        fyers=object() if has_model else None, client_id=client_id, access_token=access_token
    )
    with patch("app.hat.execution.fyers_session.get_fyers_session", return_value=session):
        token = fyers_token()
    assert token == (
        f"{client_id}:{access_token}" if has_model and client_id and access_token else None
    )


@given(times=st.lists(st.floats(0, 10_000), max_size=30).map(sorted))
def test_traceback_throttle(times: list[float]) -> None:
    """For every sequence of failures at any times, a full traceback is written for the first and
    then at most once per interval for a site, every other failure is one debug line, and the
    suppressed ones are counted. Harm: a failing tick handler writes a traceback per tick and fills
    the disk, or the first failure of the day is never shown."""
    import logging
    from uuid import uuid4

    from app.hat.data.provider.metrics import (
        TRACEBACK_INTERVAL_S,
        feed_metrics,
        log_exception_throttled,
    )

    site = f"test.{uuid4()}"
    counter = f"logging.suppressed_tracebacks.{site}"
    last: float | None = None
    suppressed = 0
    for moment in times:
        try:
            raise ValueError("bad tick")
        except ValueError:
            wrote = log_exception_throttled(
                logging.getLogger("test_feed"), site, "failed", clock=lambda m=moment: m
            )
        due = last is None or moment - last >= TRACEBACK_INTERVAL_S
        assert wrote == due
        if due:
            last = moment
        else:
            suppressed += 1
    assert feed_metrics.snapshot()["counters"].get(counter, 0) == suppressed


# --- the trading calendar (HAT-DATA-127) ---


@example(offset=0, holidays=set(), today_is_a_holiday=True, second=10 * 3600, calendar="known")
@example(offset=1, holidays=set(), today_is_a_holiday=False, second=10 * 3600, calendar="known")
@example(offset=1, holidays=set(), today_is_a_holiday=False, second=10 * 3600, calendar="missing")
@example(offset=1, holidays=set(), today_is_a_holiday=False, second=10 * 3600, calendar="no_source")
@settings(max_examples=60, deadline=None)
@given(
    offset=st.integers(0, 20),
    holidays=st.sets(st.integers(0, 20), max_size=10),
    today_is_a_holiday=st.booleans(),
    second=st.one_of(st.integers(0, 24 * 3600 - 1), st.integers(9 * 3600, 15 * 3600 + 1800)),
    calendar=st.sampled_from(["known", "known", "known", "missing", "unreadable", "no_source"]),
)
def test_calendar_closed(
    offset: int, holidays: set[int], today_is_a_holiday: bool, second: int, calendar: str
) -> None:
    """With HAT's trading calendar the feed may connect on a day the calendar lists as a holiday
    never, and on any other weekday from 09:00 IST to the 15:30 close; while the calendar is
    missing, unreadable or not wired at all, it may connect on no day. Harm: the Fyers socket
    opens on an exchange holiday, or on a day nobody knows is a trading day."""
    from contextlib import nullcontext
    from datetime import date, time, timedelta
    from unittest.mock import patch

    from app.hat.data import feed as feed_module
    from app.hat.data.calendar import CalendarDay, MarketCalendar

    day = date(2026, 10, 19) + timedelta(days=offset)  # a Monday and the days after it
    shut = {
        date(2026, 10, 19) + timedelta(days=h)
        for h in holidays | ({offset} if today_is_a_holiday else set())
    }
    rows = {h: CalendarDay(h, False, None, None, "holiday") for h in shut}
    when = datetime.combine(day, time(), IST) + timedelta(seconds=second)
    clock = FixedClock(when.astimezone(UTC))

    def load(_session: object) -> MarketCalendar | None:
        if calendar == "unreadable":
            raise OSError("calendar table unreadable")
        return MarketCalendar(time(9, 15), time(15, 30), rows) if calendar == "known" else None

    source = (
        None
        if calendar == "no_source"
        else feed_module.calendar_policy_source(lambda: nullcontext(object()), clock, ttl_s=0.0)
    )
    with patch.object(feed_module, "load_calendar", load):
        feed = feed_module.LiveFeed(policy_source=source)
        allowed = feed.connection_allowed()
    if calendar == "known":
        window = 9 * 3600 <= second <= 15 * 3600 + 1800
        assert allowed == (day.weekday() < 5 and day not in shut and window)
    else:
        assert allowed is False
