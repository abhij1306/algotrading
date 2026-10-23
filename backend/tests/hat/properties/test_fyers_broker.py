"""PBT fyers_broker.*: the circuit breaker follows its model and is
per instance, nothing is sent while it is open, only an explicit Fyers error is a rejection (a lost,
garbled or id-less answer is SUBMIT_UNCONFIRMED), and no price that is not a finite positive number
reaches the SDK. Runs on the real `FyersBroker` over a recording SDK stand-in."""

import json
import math
import threading
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hat.fakes.fyers_sdk import READ_LISTS, RecordingSdk, broker_over
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.execution.broker import (
    CODE_RATE_LIMITED,
    BrokerUnavailableError,
    GttModify,
    GttRequest,
    OrderIntent,
    OrderType,
    Side,
)
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    MAX_PAUSE,
    READ,
    READ_SHARE,
    SUBMIT_UNCONFIRMED,
    THROTTLE_PAUSE,
    WRITE,
    CallBudget,
    CallLimits,
    CircuitBreaker,
    FyersBroker,
    FyersUnavailableError,
    limits_from,
)
from app.hat.execution.tags import encode_tag

ORDER = {"symbol": "NSE:TCS-EQ", "quantity": 5, "side": "BUY", "type": "MARKET", "product": "CNC"}
GTT = {"symbol": "NSE:TCS-EQ", "quantity": 5, "side": "SELL", "product": "CNC",
       "trigger_price": 100.0, "price": 99.0}  # fmt: skip
NO_DEADLINE = settings(deadline=None)


class ScriptedSdk(RecordingSdk):
    """Answers exactly `reply` (including None) or raises it when it is an exception."""

    def _answer(self, name: str, data: object) -> object:
        self.calls.append((name, data))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


class Tick:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@given(
    threshold=st.integers(1, 5),
    recovery=st.floats(1, 60),
    steps=st.lists(
        st.one_of(
            st.tuples(st.just("fail"), st.just(0.0)),
            st.tuples(st.just("ok"), st.just(0.0)),
            st.tuples(st.just("wait"), st.floats(0, 90)),
        ),
        max_size=25,
    ),
)
def test_breaker_follows_its_model(
    threshold: int, recovery: float, steps: list[tuple[str, float]]
) -> None:
    """The breaker is open exactly from the `threshold`-th failure in a row until `recovery`
    seconds later, one success closes it at once, and a second breaker never
    moves with the first."""
    clock = Tick()
    breaker = CircuitBreaker(threshold, recovery, clock)
    other = CircuitBreaker(threshold, recovery, clock)
    failures, open_until = 0, 0.0
    for kind, amount in steps:
        if kind == "fail":
            breaker.record_failure()
            failures += 1
            if failures >= threshold:
                open_until = clock.now + recovery
        elif kind == "ok":
            breaker.record_success()
            failures, open_until = 0, 0.0
        else:
            clock.now += amount
        assert breaker.is_open() == (clock.now < open_until)
        assert other.is_open() is False
        assert other.failures == 0


@given(
    method=st.sampled_from(
        ["place_order", "place_gtt", "modify_order", "cancel_order", "cancel_gtt"]
    )
)
@NO_DEADLINE
def test_nothing_is_sent_while_the_circuit_is_open(method: str) -> None:
    """With the breaker open no order, GTT, modify or cancel reaches the SDK, and a place says so
    with a definite rejection (the order did not go out)."""
    sdk = RecordingSdk()
    broker = broker_over(sdk)
    for _ in range(broker.breaker.threshold):
        broker.breaker.record_failure()
    assert broker.breaker.is_open()
    calls = {
        "place_order": lambda: broker.place_order(dict(ORDER)),
        "place_gtt": lambda: broker.place_gtt(dict(GTT)),
        "modify_order": lambda: broker.modify_order("ORD1", {"quantity": 3, "price": 10.0}),
        "cancel_order": lambda: broker.cancel_order("ORD1"),
        "cancel_gtt": lambda: broker.cancel_gtt("GTT1"),
    }
    reply = calls[method]()
    assert sdk.calls == []
    if method.startswith("place"):
        assert reply["status"] == "REJECTED"
        assert reply["message"] == BROKER_CIRCUIT_OPEN
        assert reply["order_id"] == ""


@given(
    place=st.sampled_from(["place_order", "place_gtt"]),
    reply=st.one_of(
        st.just(None),
        st.just([]),
        st.just("ok"),
        st.just({}),
        st.just({"s": "ok"}),
        st.just({"s": "ok", "id": ""}),
        st.just({"s": "ok", "id": None}),
        st.just({"s": "pending", "id": "X1"}),
        st.just({"id": "X1"}),
        st.just({"s": "error", "message": "no funds"}),
        st.just({"s": "error"}),
        st.just({"s": "ok", "id": "ORD-77", "message": "done"}),
        st.just(RuntimeError("socket closed")),
        st.just(TimeoutError("read timed out")),
    ),
)
@NO_DEADLINE
def test_only_an_explicit_error_is_a_rejection(place: str, reply: object) -> None:
    """For any answer the SDK gives or raises, a place is SUBMITTED only with ok and an id, REJECTED
    only on an explicit error, and SUBMIT_UNCONFIRMED otherwise (the order may have landed), and an
    unconfirmed or rejected place carries no order id."""
    sdk = ScriptedSdk(reply)
    broker = broker_over(sdk)
    result = (
        broker.place_order(dict(ORDER)) if place == "place_order" else broker.place_gtt(dict(GTT))
    )
    explicit_error = isinstance(reply, dict) and reply.get("s") == "error"
    confirmed = isinstance(reply, dict) and reply.get("s") == "ok" and bool(reply.get("id"))
    assert result["status"] == (
        "SUBMITTED" if confirmed else "REJECTED" if explicit_error else SUBMIT_UNCONFIRMED
    )
    assert (result["order_id"] != "") == confirmed
    assert len(sdk.calls) == 1


# The reply Fyers gave a limit buy outside the circuit band (recorded 2026-10-22): an explicit
# error that still carries the ids of the order it created and refused (RMS, status 5).
REFUSED_WITH_ID = next(
    call["reply"]
    for call in json.loads(
        (Path(__file__).parents[1] / "fixtures" / "fyers_live_writes.json").read_text("utf-8")
    )["calls"]
    if call["method"] == "place_order"
)


@given(
    place=st.sampled_from(["place_order", "place_gtt", "adapter_order", "adapter_gtt"]),
    order_id=st.text(alphabet="0123456789", min_size=1, max_size=16),
    code=st.integers(-1000, -1),
    message=st.text(max_size=40),
)
@example(place="place_order", order_id="26102200316678", code=-99, message="")
@NO_DEADLINE
def test_a_rejection_with_an_id_stays_rejected(
    place: str, order_id: str, code: int, message: str
) -> None:
    """An explicit error stays a rejection though it carries the id of the order
    Fyers created and refused (`id`, `id_fyers`, as recorded for an RMS refusal): the broker
    answers REJECTED with no order id, and the adapter `rejected` with no broker id, never placed
    or unconfirmed. Harm: a refused order read as placed leaves the signal waiting on an order
    that never fills, or a refused stop read as resting leaves the position unprotected."""
    reply = {**REFUSED_WITH_ID, "id": order_id, "code": code, "message": message}
    sdk = ScriptedSdk(reply)
    broker = broker_over(sdk)
    if place.startswith("adapter"):
        adapter = FyersHatBroker(broker, instruments=lambda: [("TCS", "NSE:TCS-EQ")])
        if place == "adapter_order":
            tag = encode_tag(uuid.UUID(int=4), "entry", 0)
            result = adapter.place_order(OrderIntent("TCS", Side.BUY, 5, OrderType.MARKET, tag))
            assert (result.status, result.broker_order_id) == ("rejected", None)
        else:
            tag = encode_tag(uuid.UUID(int=4), "stop_gtt", 0)
            gtt = adapter.place_gtt_stop(
                GttRequest("TCS", 5, Decimal("100"), Decimal("99"), tag=tag)
            )
            assert (gtt.status, gtt.gtt_id) == ("rejected", None)
        assert len(sdk.calls) == 1
        return
    result = (
        broker.place_order(dict(ORDER)) if place == "place_order" else broker.place_gtt(dict(GTT))
    )
    assert (result["status"], result["order_id"]) == ("REJECTED", "")
    assert len(sdk.calls) == 1


BAD_PRICES = st.one_of(
    st.sampled_from(
        [
            math.nan,
            math.inf,
            -math.inf,
            0.0,
            -0.0,
            -1.0,
            "nan",
            "inf",
            "-5",
            "abc",
            None,
            "",
            [],
            {},
        ]
    ),
    st.floats(max_value=0, allow_nan=False),
)
GOOD_PRICES = st.floats(min_value=0.05, max_value=1e7, allow_nan=False, allow_infinity=False)


@given(order_type=st.sampled_from(["LIMIT", "SL", "SL-M"]), bad=BAD_PRICES, good=GOOD_PRICES)
@NO_DEADLINE
def test_a_price_that_is_not_finite_and_positive_never_reaches_the_sdk(
    order_type: str, bad: object, good: float
) -> None:
    """A limit or trigger price that is NaN, infinite, zero, negative or not a number makes a
    definite rejection and no SDK call; the same order with a finite positive price is sent with
    exactly that price."""
    sdk = RecordingSdk()
    broker = broker_over(sdk)
    needs_limit = order_type in {"LIMIT", "SL"}
    needs_trigger = order_type in {"SL", "SL-M"}
    base = {**ORDER, "type": order_type, "price": good, "trigger_price": good}
    for field in [
        f for f, needed in (("price", needs_limit), ("trigger_price", needs_trigger)) if needed
    ]:
        result = broker.place_order({**base, field: bad})
        assert result["status"] == "REJECTED"
        assert sdk.calls == []
    sent = broker.place_order(base)
    assert sent["status"] == "SUBMITTED"
    (_, payload), = sdk.calls  # fmt: skip
    assert payload["limitPrice"] == (good if needs_limit else 0)
    assert payload["stopPrice"] == (good if needs_trigger else 0)


@given(bad=BAD_PRICES, good=GOOD_PRICES, field=st.sampled_from(["price", "trigger_price"]))
@NO_DEADLINE
def test_a_gtt_with_a_bad_price_never_reaches_the_sdk(bad: object, good: float, field: str) -> None:
    """A GTT place or modify with a non-finite or non-positive price is refused before the SDK."""
    sdk = RecordingSdk()
    broker = broker_over(sdk)
    placed = broker.place_gtt({**GTT, "price": good, "trigger_price": good, field: bad})
    modified = broker.modify_gtt("GTT1", {"price": good, "trigger_price": good, field: bad})
    assert placed["status"] == "REJECTED"
    assert modified["status"] == "REJECTED"
    assert sdk.calls == []


@given(threshold=st.integers(1, 4))
@NO_DEADLINE
def test_brokers_do_not_share_a_breaker_unless_given_one(threshold: int) -> None:
    """Failures recorded through one broker never open another's circuit (they used to share class
    state); two brokers given the same breaker do share it."""
    first, second = FyersBroker(lambda: RecordingSdk()), FyersBroker(lambda: RecordingSdk())
    for _ in range(max(first.breaker.threshold, threshold)):
        first.breaker.record_failure()
    assert first.breaker.is_open()
    assert not second.breaker.is_open()
    shared = CircuitBreaker()
    one, two = (
        FyersBroker(lambda: RecordingSdk(), breaker=shared),
        FyersBroker(lambda: RecordingSdk(), breaker=shared),
    )
    for _ in range(shared.threshold):
        one.breaker.record_failure()
    assert two.breaker.is_open()
    with pytest.raises(AttributeError):
        _ = CircuitBreaker.failures  # no class-level state to leak


# The list reads, by the FyersBroker method and the SDK call behind it.
LIST_READS = {
    "fetch_orderbook": "orderbook",
    "fetch_tradebook": "tradebook",
    "fetch_holdings": "holdings",
    "fetch_gtt_orders": "gtt_orderbook",
    "get_positions": "positions",
}


@given(
    method=st.sampled_from(sorted(LIST_READS)),
    shape=st.sampled_from(["missing", "null", "dict", "nested", "string"]),
)
@NO_DEADLINE
def test_an_ok_reply_without_its_list_is_unreadable(method: str, shape: str) -> None:
    """An ok reply that does not carry its list under the documented key (missing,
    null, the wrong type, nested elsewhere) raises `FyersUnavailableError`, never reads as an
    empty book; the reply with its list, empty, reads as empty. Harm: after a lost answer the
    tag lookup reads "not found" and sends the order again; a session end ends a working entry."""
    key = READ_LISTS[LIST_READS[method]]
    body = {
        "missing": {},
        "null": {key: None},
        "dict": {key: {}},
        "nested": {"data": {key: []}},
        "string": {key: "[]"},
    }[shape]
    read = getattr(broker_over(ScriptedSdk({"s": "ok", "code": 200, **body})), method)
    with pytest.raises(FyersUnavailableError):
        read()
    assert getattr(broker_over(RecordingSdk()), method)() == []


class OpensMidway(CircuitBreaker):
    """Closed at the caller's check, open by the time the guarded call checks again."""

    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def is_open(self) -> bool:
        self.checks += 1
        return self.checks > 1


@given(method=st.sampled_from(["place_order", "place_gtt"]))
@NO_DEADLINE
def test_a_circuit_opening_mid_place_is_not_sent(method: str) -> None:
    """A place whose circuit opens between its own check and the guarded call sends nothing and
    says so with the definite circuit-open rejection, never `SUBMIT_UNCONFIRMED` (which would
    leave a GTT that can never be looked up). Harm: a stop that never went out is read as one that
    may rest at the broker, and nothing protects the position."""
    sdk = RecordingSdk()
    broker = FyersBroker(lambda: sdk, breaker=OpensMidway())
    reply = (
        broker.place_order(dict(ORDER)) if method == "place_order" else broker.place_gtt(dict(GTT))
    )
    assert sdk.calls == []
    assert reply["status"] == "REJECTED"
    assert reply["message"] == BROKER_CIRCUIT_OPEN


# --- the call budget ---------------------------------

BUDGET_DAY = date(2026, 10, 23)


def _read_cap(limit: int) -> int:
    return max(1, math.floor(limit * READ_SHARE))


def _read_day_cap(limit: int) -> int:
    return math.floor(limit * READ_SHARE)  # no minimum: the day's last call is a write's


@settings(max_examples=150, deadline=None)
@given(
    per_second=st.integers(1, 10),
    per_minute=st.integers(1, 40),
    per_day=st.one_of(st.none(), st.integers(1, 12), st.integers(13, 60)),
    steps=st.lists(
        st.tuples(
            st.sampled_from([READ, WRITE, READ, WRITE, "pause", "next_day"]),
            # seconds since the last step: mostly bursts, so the windows fill
            st.one_of(st.just(0.0), st.floats(0, 0.5), st.floats(0, 3, allow_nan=False)),
            st.floats(0, 120, allow_nan=False),  # a pause's Retry-After
        ),
        max_size=80,
    ),
)
@example(per_second=8, per_minute=180, per_day=5000, steps=[(WRITE, 0.0, 0.0)] * 10)
@example(per_second=2, per_minute=3, per_day=None, steps=[(READ, 0.0, 0.0)] * 4)
@example(per_second=10, per_minute=40, per_day=2, steps=[(WRITE, 1.0, 0.0)] * 3)
@example(per_second=10, per_minute=40, per_day=1, steps=[(READ, 1.0, 0.0), (WRITE, 1.0, 0.0)])
@example(  # a wait whose float sum lands a hair inside the minute (found by the search)
    per_second=2,
    per_minute=3,
    per_day=None,
    steps=[("pause", 0.0, 1.805155854092929)] + [(READ, 0.0, 0.0)] * 3 + [(WRITE, 0.0, 0.0)],
)
@example(  # a pause longer than the second's wait
    per_second=1, per_minute=40, per_day=None, steps=[(WRITE, 0.0, 0.0), ("pause", 0.0, 5.0)] * 2
)
def test_budget_windows_hold(
    per_second: int,
    per_minute: int,
    per_day: int | None,
    steps: list[tuple[str, float, float]],
) -> None:
    """: for every limit and sequence of calls, pauses and day changes, no
    second holds more than `per_second` granted calls, no minute more than `per_minute`, no day
    more than `per_day`; a read is granted only while the window (and the day) leaves the share
    kept for writes free; nothing is granted inside a pause (at most `MAX_PAUSE`); a call is
    refused only for one of those reasons, and a refused one asked again after the wait it was
    told is granted (none after the day's calls are spent). Harm: the 15:25 swap and a burst of
    fill syncs exceed Fyers' per-second or per-minute limit, every write is refused, and stops
    are not placed; or a read takes the last call a stop needed."""
    now, today = 1000.0, BUDGET_DAY
    budget = CallBudget(
        CallLimits(per_second, per_minute, per_day), clock=lambda: now, day=lambda: today
    )
    granted: list[tuple[float, date]] = []
    paused_until = 0.0

    def in_window(at: float, span: float) -> int:
        return sum(1 for t, d in granted if at - span < t <= at)

    def day_count() -> int:
        return sum(1 for _, d in granted if d == today)

    def check(kind: str) -> None:
        write = kind == WRITE
        sec = per_second if write else _read_cap(per_second)
        minute = per_minute if write else _read_cap(per_minute)
        day_cap = None if per_day is None else (per_day if write else _read_day_cap(per_day))
        reasons = (
            now < paused_until,
            in_window(now, 1.0) >= sec,
            in_window(now, 60.0) >= minute,
            day_cap is not None and day_count() >= day_cap,
        )
        wait = budget.take(kind, now, today)
        if wait == 0:
            assert not any(reasons)
            granted.append((now, today))
            assert in_window(now, 1.0) <= per_second
            assert in_window(now, 60.0) <= per_minute
            assert per_day is None or day_count() <= per_day
        else:
            assert any(reasons)
            if math.isinf(wait):
                assert reasons[3]
            else:
                assert not reasons[3]
                assert budget.take(kind, now + wait, today) == 0
                granted.append((now + wait, today))

    for action, gap, pause in steps:
        now += gap
        if action == "next_day":
            today += timedelta(days=1)
        elif action == "pause":
            budget.pause(pause)
            paused_until = max(paused_until, now + min(pause, MAX_PAUSE))
        else:
            check(action)
            now = max([now] + [t for t, _ in granted])


@settings(max_examples=40, deadline=None)
@given(
    op=st.sampled_from(["place_order", "place_gtt", "modify_gtt", "cancel_gtt", "orders", "gtts"]),
    source=st.sampled_from(["budget", "http429_json", "http429_raw"]),
    retry_after=st.sampled_from([None, "2", "0.5", "120", "soon", "-1"]),
)
@example(op="place_gtt", source="budget", retry_after=None)
@example(op="orders", source="http429_json", retry_after="2")
def test_throttled_call_not_sent(op: str, source: str, retry_after: str | None) -> None:
    """: a call the budget cannot grant is never sent, and one Fyers answers
    with HTTP 429 (a JSON error body, or none) is sent once and not again; either way a read
    fails closed with `broker_rate_limited`, a write is refused with `broker_rate_limited` as not
    sent (burning no retry), the circuit counts nothing, and a 429 pauses the whole budget for
    its Retry-After in seconds (at most `MAX_PAUSE`; `THROTTLE_PAUSE` when absent or unreadable).
    Harm: a throttled stop reads as a broker rejection and spends its tag's sequence, or an
    outage is declared and every call refused for 20 s when Fyers only asked HAT to slow down."""
    clock = Tick()
    budget = CallBudget(CallLimits(8, 180, None), clock=clock, day=lambda: BUDGET_DAY)
    sdk = Http429Sdk(raw=source == "http429_raw", retry_after=retry_after)
    if source == "budget":
        budget = CallBudget(CallLimits(8, 180, 1), clock=clock, day=lambda: BUDGET_DAY)
        assert budget.take(WRITE, clock(), BUDGET_DAY) == 0  # the day's one call is spent
        sdk.throttle = False
    breaker = CircuitBreaker()
    broker = FyersBroker(lambda: sdk, breaker=breaker, budget=budget)
    adapter = _adapter_over(broker)
    call = BUDGET_OPS[op]
    if op in ("orders", "gtts"):
        with pytest.raises(BrokerUnavailableError) as caught:
            call(adapter)
        assert caught.value.code == CODE_RATE_LIMITED
    else:
        result = call(adapter)
        assert result.code == CODE_RATE_LIMITED
        assert getattr(result, "status", "rejected") == "rejected"
        assert getattr(result, "ok", False) is False
    assert breaker.failures == 0
    assert not breaker.is_open()
    if source == "budget":
        assert sdk.calls == []
        return
    assert len(sdk.calls) == 1
    if retry_after is None or retry_after in ("soon", "-1"):
        expected = THROTTLE_PAUSE
    else:
        expected = min(float(retry_after), MAX_PAUSE)
    assert budget.take(WRITE, clock() + expected - 0.01, BUDGET_DAY) > 0
    assert budget.take(WRITE, clock() + expected, BUDGET_DAY) == 0


@settings(max_examples=3, deadline=None)
@given(retry_after=st.sampled_from([None, "2", "30"]))
def test_late_429_still_pauses(retry_after: str | None) -> None:
    """: a write Fyers answers HTTP 429 only after the guard gave up
    waiting is an unknown outcome for its caller (`SUBMIT_UNCONFIRMED`, one breaker failure, as
    any timeout), and the 429 still pauses the budget for its Retry-After when it lands. Harm:
    a slow broker's 429 is lost and HAT keeps calling into the limit."""
    clock = Tick()
    budget = CallBudget(CallLimits(8, 180, None), clock=clock, day=lambda: BUDGET_DAY)
    landed = threading.Event()

    class SlowHttp429Sdk(Http429Sdk):
        def _answer(self, name: str, data: object) -> object:
            time.sleep(1.7)  # past the write's 1.5 s guard
            try:
                return super()._answer(name, data)
            finally:
                landed.set()

    sdk = SlowHttp429Sdk(raw=False, retry_after=retry_after)
    breaker = CircuitBreaker()
    broker = FyersBroker(lambda: sdk, breaker=breaker, budget=budget)
    reply = broker.place_order(dict(ORDER))
    assert reply["status"] == SUBMIT_UNCONFIRMED
    assert breaker.failures == 1
    assert landed.wait(5)
    time.sleep(0.05)  # the worker pauses the budget right after the answer
    expected = THROTTLE_PAUSE if retry_after is None else float(retry_after)
    assert budget.take(WRITE, clock() + expected - 0.01, BUDGET_DAY) > 0
    assert budget.take(WRITE, clock() + expected, BUDGET_DAY) == 0


@settings(max_examples=12, deadline=None)
@given(
    op=st.sampled_from(["place_order", "place_gtt", "modify_gtt", "cancel_gtt", "orders", "gtts"])
)
def test_breaker_opened_while_waiting_sends_nothing(op: str) -> None:
    """: a call that waited for the budget while another call opened
    the circuit sends nothing: a read fails closed and a write is answered as when the circuit
    was open before it asked. Harm: calls queued behind the budget reach a broker the breaker
    has just declared down."""
    breaker = CircuitBreaker()

    class OpeningBudget(CallBudget):
        def acquire(self, kind: str, max_wait: float) -> bool:
            for _ in range(breaker.threshold):
                breaker.record_failure()
            return True

    sdk = RecordingSdk()
    adapter = _adapter_over(
        FyersBroker(lambda: sdk, breaker=breaker, budget=OpeningBudget(CallLimits()))
    )
    if op in ("orders", "gtts"):
        with pytest.raises(BrokerUnavailableError):
            BUDGET_OPS[op](adapter)
    else:
        result = BUDGET_OPS[op](adapter)
        assert getattr(result, "ok", False) is False
    assert sdk.calls == []


@settings(max_examples=12, deadline=None)
@given(
    op=st.sampled_from(["place_order", "place_gtt", "modify_gtt", "cancel_gtt", "orders", "gtts"])
)
def test_writes_go_ahead_of_reads(op: str) -> None:
    """: reads may use only `READ_SHARE` of a window, so once they have spent
    it every write (an order, a stop, a GTT) is still sent and every read is refused unsent; and
    while a write waits for a call, a read is not granted one even when the window has room.
    Harm: the 5-second fill sync's reads take the calls a stop placement waits for, and the
    position sits unprotected."""
    budget = CallBudget(CallLimits(100, 5, None))  # reads get 4 of the minute's 5 calls
    while budget.take(READ, time.monotonic(), BUDGET_DAY) == 0:
        pass
    sdk = RecordingSdk()
    adapter = _adapter_over(FyersBroker(lambda: sdk, breaker=CircuitBreaker(), budget=budget))
    if op in ("orders", "gtts"):
        with pytest.raises(BrokerUnavailableError) as caught:
            BUDGET_OPS[op](adapter)
        assert caught.value.code == CODE_RATE_LIMITED
        assert sdk.calls == []
    else:
        BUDGET_OPS[op](adapter)
        assert len(sdk.calls) == 1

    now = 0.0
    gated = CallBudget(CallLimits(1, 100, None), clock=lambda: now, day=lambda: BUDGET_DAY)
    assert gated.take(WRITE, now, BUDGET_DAY) == 0  # this second's call is spent
    granted: list[bool] = []
    writer = threading.Thread(target=lambda: granted.append(gated.acquire(WRITE, 3.0)), daemon=True)
    writer.start()
    while not gated._writers:  # the write is waiting
        time.sleep(0.001)
    now = 1.0  # the next second's call is free
    read_granted = gated.acquire(READ, 0.0)
    with gated._cond:
        gated._cond.notify_all()
    writer.join(timeout=3)
    if writer.is_alive():  # the read took the call: let the write's wait end
        now = 10.0
        writer.join(timeout=3)
    assert not read_granted
    assert granted == [True]
    assert gated.take(READ, now, BUDGET_DAY) > 0  # the write took it


@given(
    per_second=st.one_of(st.none(), st.integers(-5, 50).map(str), st.just("eight")),
    per_minute=st.one_of(st.none(), st.integers(-5, 500).map(str), st.just("")),
    per_day=st.one_of(st.none(), st.integers(-5, 20000).map(str), st.sampled_from(["off", "x"])),
)
def test_limits_from_settings(
    per_second: str | None, per_minute: str | None, per_day: str | None
) -> None:
    """: the budget's limits come from the settings, so a plan with higher
    limits (Fyers Prime) is a configuration change only: a positive whole number is taken as
    given, `0` or `off` lifts the day cap, and anything unset or unreadable is the default (8,
    180, 5,000). Harm: a typo in the environment stops every broker call, or Prime's limits
    need a code change."""
    env = {
        name: value
        for name, value in (
            ("HAT_BROKER_CALLS_PER_SECOND", per_second),
            ("HAT_BROKER_CALLS_PER_MINUTE", per_minute),
            ("HAT_BROKER_CALLS_PER_DAY", per_day),
        )
        if value is not None
    }
    limits = limits_from(env)

    def expected(raw: str | None, default: int) -> int:
        return (
            int(raw) if raw is not None and raw.lstrip("-").isdigit() and int(raw) > 0 else default
        )

    assert limits.per_second == expected(per_second, 8)
    assert limits.per_minute == expected(per_minute, 180)
    if per_day in ("0", "off"):
        assert limits.per_day is None
    else:
        assert limits.per_day == expected(per_day, 5000)


class Http429Sdk(RecordingSdk):
    """A model whose requests session runs its response hooks with HTTP 429 (and Retry-After)
    on every call, then answers Fyers' JSON error body, or raises as the SDK does when the body
    is not JSON. With `throttle` off it answers as `RecordingSdk`."""

    def __init__(self, *, raw: bool, retry_after: str | None) -> None:
        super().__init__()
        self.raw = raw
        self.retry_after = retry_after
        self.throttle = True
        self.service = type("Service", (), {})()
        self.service.session = type("Session", (), {"hooks": {"response": []}})()

    def _answer(self, name: str, data: object) -> object:
        if not self.throttle:
            return super()._answer(name, data)
        self.calls.append((name, data))
        headers = {} if self.retry_after is None else {"Retry-After": self.retry_after}
        response = type("Response", (), {"status_code": 429, "headers": headers})()
        for hook in self.service.session.hooks["response"]:
            hook(response)
        if self.raw:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return {"s": "error", "code": 429, "message": "request limit reached"}


def _adapter_over(broker: FyersBroker) -> FyersHatBroker:
    return FyersHatBroker(
        broker,
        client=lambda: type("C", (), {"fyers": object()})(),  # type: ignore[arg-type,return-value]
        instruments=lambda: [("TCS", "NSE:TCS-EQ")],
    )


BUDGET_OPS = {
    "place_order": lambda a: a.place_order(
        OrderIntent(
            "TCS",
            Side.SELL,
            5,
            OrderType.SL_M,
            encode_tag(uuid.UUID(int=7), "stop_day", 0),
            trigger_price=Decimal("95"),
        )
    ),
    "place_gtt": lambda a: a.place_gtt_stop(
        GttRequest(
            "TCS",
            5,
            Decimal("95"),
            Decimal("94.5"),
            tag=encode_tag(uuid.UUID(int=7), "stop_gtt", 0),
        )
    ),
    "modify_gtt": lambda a: a.modify_gtt(
        GttModify("26102200000001", 5, Decimal("96"), Decimal("95.5"))
    ),
    "cancel_gtt": lambda a: a.cancel_gtt("26102200000001"),
    "orders": lambda a: a.orders(),
    "gtts": lambda a: a.gtts(),
}


AMOUNTS = st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False)
NOT_AMOUNTS = st.sampled_from([math.nan, math.inf, -math.inf, "1,5", "abc", True, [1]])


@given(
    buy=AMOUNTS,
    ltp=AMOUNTS,
    pnl=AMOUNTS,
    available=AMOUNTS,
    used=AMOUNTS,
    bad=st.none() | NOT_AMOUNTS,
    where=st.sampled_from(["positions", "funds"]),
)
@example(buy=0.1, ltp=0.2, pnl=0.3, available=0.1, used=0.2, bad=None, where="funds")
@NO_DEADLINE
def test_reply_amounts_exact(
    buy: float, ltp: float, pnl: float, available: float, used: float, bad: object, where: str
) -> None:
    """PBT fyers_broker.reply_amounts_exact (HAT-SCH-014, trading-safety rules 7 and 8; PR 75c): the
    amounts of a positions or funds reply (JSON floats) are read exactly by `money.exact`, as
    the Decimal each float's shortest text names, and a total Fyers leaves out is the exact sum
    of the available and used amounts; an amount that is not a finite number fails the read
    closed. Harm: 0.1 + 0.2 in float puts 0.30000000000000004 of free cash into sizing, or a NaN
    average price reads as a position's cost."""
    row = {"symbol": "NSE:TCS-EQ", "netQty": 5, "buyAvg": buy, "ltp": ltp, "pl": pnl}
    funds = {"available": available, "used": used, "fund_limit": []}
    if bad is not None:
        (row if where == "positions" else funds)["buyAvg" if where == "positions" else "used"] = bad
    reply = {"s": "ok", "netPositions": [row]} if where == "positions" else {"s": "ok", **funds}
    adapter = FyersHatBroker(broker_over(ScriptedSdk(reply)))
    read = adapter.positions if where == "positions" else adapter.funds
    if bad is not None:
        with pytest.raises(BrokerUnavailableError):
            read()
        return
    if where == "positions":
        [position] = adapter.positions()
        assert position.avg_price == Decimal(repr(buy))
        [raw] = broker_over(ScriptedSdk(reply)).get_positions()
        assert (raw["current_price"], raw["pnl"]) == (Decimal(repr(ltp)), Decimal(repr(pnl)))
        return
    got = adapter.funds()
    assert (got.available, got.used) == (Decimal(repr(available)), Decimal(repr(used)))
    assert got.total == Decimal(repr(available)) + Decimal(repr(used))
