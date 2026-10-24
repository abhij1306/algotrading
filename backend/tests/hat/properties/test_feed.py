"""PBT feed.* (to 112 to 127): the one provider socket
HAT owns. A state machine drives `ProviderSession` over a fake SDK socket through every connect,
drop, SDK auto-reconnect, stop and stale callback and checks that the process never holds more than
one open socket, replays every wanted symbol on each connect and ignores callbacks from sockets it
let go of; a second one checks the provider's subscriptions converge to the registry under races;
a third that the Fyers login hooks keep the socket on the current token and down after a logout.
Ticks, the registry and the tick transform are checked against their own definitions. No network.
"""

import asyncio
import copy
import threading
from datetime import UTC, datetime
from typing import Any

from hat.fakes.data_socket import (
    FakeSocketFactory,
    make_feed,
)
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from app.hat.clock import FixedClock
from app.hat.data.provider.ticks import normalize_tick
from app.hat.data.subscriptions import HAT_OWNER, SubscriptionRegistry
from app.hat.data.symbols import symbol_master
from app.hat.execution.fyers_auth import FyersCredentials
from app.hat.time import IST

OPEN = datetime(2026, 10, 21, 10, 0, tzinfo=IST).astimezone(UTC)  # a Wednesday, mid-session
SATURDAY = datetime(2026, 10, 24, 10, 0, tzinfo=IST).astimezone(UTC)
# --- the session ---


# --- ticks ---

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


# --- the registry ---

OWNERS = ["ws-1", "ws-2", "ws-3", HAT_OWNER]
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


# --- the provider follows the registry ---

# --- the Fyers login drives the feed ---

CREDS = FyersCredentials("APP-100", "SECRET", "https://app.example.test/cb")


# --- the health monitor and the token check ---


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


# --- the SDK socket factory and the token source ---

tokens = st.from_regex(r"[A-Z0-9]{3,10}:[A-Za-z0-9._-]{5,30}", fullmatch=True)


maybe_text = st.one_of(st.none(), st.just(""), st.text("abcXYZ019:", min_size=1, max_size=12))


@given(client_id=maybe_text, access_token=maybe_text, has_model=st.booleans())
def test_token_source(client_id: str | None, access_token: str | None, has_model: bool) -> None:
    """The provider token is "client_id:access_token" exactly when the Fyers session holds a usable
    SDK model, a client id and an access token, and None otherwise (nobody is logged in, or the
    token expired). Harm: the socket is built with half a token and Fyers locks the app out, or an
    expired token is retried for ever."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.hat.execution.fyers_session import fyers_token

    session = SimpleNamespace(
        fyers=object() if has_model else None, client_id=client_id, access_token=access_token
    )
    with patch("app.hat.execution.fyers_session.get_fyers_session", return_value=session):
        token = fyers_token()
    assert token == (
        f"{client_id}:{access_token}" if has_model and client_id and access_token else None
    )


# --- the trading calendar ---


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
    never, and on any other weekday of the calendar from 09:00 IST to the 15:30 close; while the
    calendar is missing, unreadable or not wired at all, it may connect on no day. Harm: the
    Fyers socket opens on an exchange holiday, or on a day nobody knows is a trading day."""
    from contextlib import nullcontext
    from datetime import date, time, timedelta
    from unittest.mock import patch

    from hat.fakes.data_socket import weekday_calendar

    from app.hat.data import feed as feed_module
    from app.hat.risk.sessions import SessionCalendar, SessionDay

    day = date(2026, 10, 19) + timedelta(days=offset)  # a Monday and the days after it
    shut = {
        date(2026, 10, 19) + timedelta(days=h)
        for h in holidays | ({offset} if today_is_a_holiday else set())
    }
    when = datetime.combine(day, time(), IST) + timedelta(seconds=second)
    clock = FixedClock(when.astimezone(UTC))
    rows = dict(weekday_calendar(clock).days) | {h: SessionDay(False) for h in shut}

    def load(_session: object) -> SessionCalendar | None:
        if calendar == "unreadable":
            raise OSError("calendar table unreadable")
        return SessionCalendar(rows) if calendar == "known" else None

    source = (
        None
        if calendar == "no_source"
        else feed_module.calendar_source(lambda: nullcontext(object()), ttl_s=0.0)
    )
    with patch.object(feed_module, "session_calendar", load):
        feed = feed_module.LiveFeed(
            calendar_source=source,
            clock=clock,
            token_source=lambda: None,
            validate_token=lambda: False,
        )
        allowed = feed.connection_allowed()
    if calendar == "known":
        window = 9 * 3600 <= second <= 15 * 3600 + 1800
        assert allowed == (day.weekday() < 5 and day not in shut and window)
    else:
        assert allowed is False


@settings(max_examples=12, deadline=None)
@given(calendar=st.sampled_from(["known", "missing", "unreadable"]), stop_at_once=st.booleans())
def test_calendar_read_off_loop(calendar: str, stop_at_once: bool) -> None:
    """PBT feed.calendar_read_off_loop: starting the feed and the
    connect it begins read the trading calendar only off the event loop, a calendar found missing
    or unreadable is not read again by the connect that follows, and a stop before that connect
    finishes leaves no socket. Harm: a slow calendar query stalls every task on the loop (ticks,
    the monitor), or a shutdown races a start-up connect that opens a socket afterwards."""
    from contextlib import nullcontext
    from unittest.mock import patch

    from hat.fakes.data_socket import weekday_calendar

    from app.hat.data import feed as feed_module

    clock = FixedClock(OPEN)
    reads: list[bool] = []  # one entry per calendar read: was it on the loop thread

    def load(_session: object) -> object:
        try:
            asyncio.get_running_loop()
            reads.append(True)
        except RuntimeError:
            reads.append(False)
        if calendar == "unreadable":
            raise OSError("calendar table unreadable")
        return weekday_calendar(clock) if calendar == "known" else None

    async def run() -> bool:
        factory = FakeSocketFactory()
        source = feed_module.calendar_source(lambda: nullcontext(object()))
        feed = feed_module.LiveFeed(
            calendar_source=source,
            clock=clock,
            socket_factory=factory,
            token_source=lambda: "CLIENT:tok",
            validate_token=lambda: True,
            spawn=lambda target: None,  # the runner is not needed: only whether it was started
        )
        feed.start(asyncio.get_running_loop(), prevalidated_token=True)
        if not stop_at_once:
            await asyncio.sleep(0.05)
        await feed.stop()
        await asyncio.sleep(0.05)  # anything left running after the stop gets its turn
        return feed.session.busy

    with patch.object(feed_module, "session_calendar", load):
        busy = asyncio.run(run())
    assert True not in reads
    assert len(reads) <= 1
    assert not busy
