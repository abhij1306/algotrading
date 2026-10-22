"""PBT fyers_broker.* (HAT-BRK-081, HAT-DEC-BD2, K2): the circuit breaker follows its model and is
per instance, nothing is sent while it is open, only an explicit Fyers error is a rejection (a lost,
garbled or id-less answer is SUBMIT_UNCONFIRMED), and no price that is not a finite positive number
reaches the SDK. Runs on the real `FyersBroker` over a recording SDK stand-in."""

import json
import math
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from hat.fakes.fyers_sdk import READ_LISTS, RecordingSdk, broker_over
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.execution.broker import GttRequest, OrderIntent, OrderType, Side
from app.hat.execution.fyers_adapter import FyersHatBroker
from app.hat.execution.fyers_broker import (
    BROKER_CIRCUIT_OPEN,
    SUBMIT_UNCONFIRMED,
    CircuitBreaker,
    FyersBroker,
    FyersUnavailableError,
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
    """HAT-BRK-081: an explicit error stays a rejection though it carries the id of the order
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
    """HAT-BRK-081: an ok reply that does not carry its list under the documented key (missing,
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
