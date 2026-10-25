"""PBT fyers_broker.*: the circuit breaker follows its model and is
per instance, nothing is sent while it is open, only an explicit Fyers error is a rejection (a lost,
garbled or id-less answer is SUBMIT_UNCONFIRMED), and the adapter's reply parsers read amounts
exactly. Runs on the real `FyersBroker` (the transport) over a recording SDK stand-in."""

import json
import math
import re
import threading
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hat.fakes.broker import funds_reply
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
    SDK_HTTP_TIMEOUT,
    SUBMIT_UNCONFIRMED,
    WRITE,
    CallBudget,
    CallLimits,
    CircuitBreaker,
    FyersBroker,
    FyersUnavailableError,
    HungCalls,
    _with_timeout,
    classify_place_reply,
    day_refused,
    limits_from,
)
from app.hat.execution.fyers_payloads import (
    gtt_payload,
    parse_funds,
    parse_gtt_row,
    parse_order_book,
    parse_positions,
)
from app.hat.execution.tags import encode_tag

# SDK payloads as the adapter builds them (the transport sends them as they are)
ORDER = {"symbol": "NSE:TCS-EQ", "qty": 5, "type": 2, "side": 1, "productType": "CNC",
         "limitPrice": 0, "stopPrice": 0, "validity": "DAY", "disclosedQty": 0,
         "offlineOrder": False}  # fmt: skip
GTT = {"side": -1, "symbol": "NSE:TCS-EQ", "productType": "CNC",
       "orderInfo": {"leg1": {"price": 99.0, "triggerPrice": 100.0, "qty": 5}}}  # fmt: skip
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


WRITES_FIXTURE = Path(__file__).parents[1] / "fixtures" / "fyers_live_writes.json"
RECORDED_WRITES = json.loads(WRITES_FIXTURE.read_text("utf-8"))["calls"]
# A PAN (5 letters, 4 digits, a letter), Fyers' "C-" RMS client code, an IPv4 address
_ACCOUNT_IDS = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b|\bC-[A-Z0-9]{5,}|\b\d{1,3}(\.\d{1,3}){3}\b")


def _recorded(method: str) -> list[dict]:
    return [c for c in RECORDED_WRITES if c["method"] == method]


# The reply Fyers gave a limit buy outside the circuit band (recorded 2026-10-22): an explicit
# error that still carries the ids of the order it created and refused (RMS, status 5).
REFUSED_WITH_ID = _recorded("place_order")[0]["reply"]


def test_recorded_writes__parsed() -> None:
    """The Fyers write replies recorded live on 2026-10-22: the GTT place payload HAT builds for
    each recorded request is the one Fyers accepted (with HAT's `orderTag`) and its reply reads
    as submitted with Fyers' id; the GTT book rows (live keys `tran_side`, `product_type`,
    `ordertag` "1:GTT<tag>" or "2:GTTUntagged") read as resting sell GTTs with their tag, and
    cancelled ones as None; the two orders placed under one tag and refused by RMS (Fyers does
    not enforce tag uniqueness) each read as a rejection. The fixture carries no account id."""
    text = WRITES_FIXTURE.read_text(encoding="utf-8")
    assert {m.group(0) for m in _ACCOUNT_IDS.finditer(text)} <= {"XXXXX0000X"}
    stop_tag = encode_tag(uuid.UUID(int=24), "stop_gtt", 0)
    for call in _recorded("place_gtt_order"):
        sdk = RecordingSdk(call["reply"])
        leg = call["args"]["orderInfo"]["leg1"]
        request = GttRequest(
            "SBIN",
            leg["qty"],
            Decimal(str(leg["triggerPrice"])),
            Decimal(str(leg["price"])),
            tag=stop_tag,
        )
        symbol = call["args"]["symbol"]
        payload = gtt_payload(request, lambda _symbol, symbol=symbol: symbol)
        assert payload == {**call["args"], "orderTag": stop_tag}
        reply = broker_over(sdk).place_gtt(payload)
        assert sdk.calls == [("place_gtt_order", payload)]
        assert (reply["status"], reply["order_id"]) == ("SUBMITTED", call["reply"]["id"])
    tag = encode_tag(uuid.UUID(int=25), "stop_gtt", 0)
    for call in _recorded("gtt_orderbook"):
        for raw in call["reply"]["orderBook"]:
            row = {**raw, "ordertag": raw["ordertag"].replace("HATTEST1", tag)}
            gtt = parse_gtt_row(row)
            if row["ord_status"] == 1:  # cancelled
                assert gtt is None
                continue
            assert gtt is not None
            assert gtt.side.value == "sell"
            assert gtt.qty == row["qty"]
            assert gtt.tag == (tag if "GTT" + tag in row["ordertag"] else None)
    orders = parse_order_book(_recorded("orderbook")[-1]["reply"]["orderBook"])
    assert [(o.tag, o.status) for o in orders] == [("HATTEST2", "rejected")] * 2


@pytest.mark.parametrize(
    ("reply", "status", "order_id"),
    [
        (None, SUBMIT_UNCONFIRMED, ""),  # timed out or raised: no answer
        ({"s": "ok", "message": "placed"}, SUBMIT_UNCONFIRMED, ""),  # accepted, no id
        ({"s": "ok", "id": ""}, SUBMIT_UNCONFIRMED, ""),
        ({"s": "error", "code": -50, "message": "bad price"}, "REJECTED", ""),
        ({"code": 200, "id": "X1"}, SUBMIT_UNCONFIRMED, ""),  # neither ok nor an error
        ("ok", SUBMIT_UNCONFIRMED, ""),
        ({"s": "error", "code": 429, "message": "place_order rate limited"}, "REJECTED", ""),
        ({"s": "ok", "id": "2410230001"}, "SUBMITTED", "2410230001"),
    ],
)
def test_classify_place_reply(reply: object, status: str, order_id: str) -> None:
    """A place's reply is SUBMITTED only with an id, REJECTED only on an explicit error (a 429
    keeps its reply, so the adapter reads it as rate limited, nothing placed), and
    SUBMIT_UNCONFIRMED for anything else."""
    got = classify_place_reply(reply)
    assert (got["status"], got["order_id"]) == (status, order_id)
    if status == "REJECTED":
        assert got["details"] == reply


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
    "fetch_positions": "positions",
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
    for _ in range(5):  # the reads' share is 4 calls
        if budget.take(READ, time.monotonic(), BUDGET_DAY) != 0:
            break
    else:
        pytest.fail("reads were never refused")
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
    deadline = time.monotonic() + 5
    while not gated._writers:  # the write is waiting
        assert time.monotonic() < deadline, "the write never waited"
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
NOT_AMOUNTS = st.sampled_from([math.nan, math.inf, -math.inf, "1,5", "abc", True, [1], None])


@given(
    buy=st.floats(min_value=0.01, max_value=1e9),
    available=AMOUNTS,
    used=AMOUNTS,
    bad=st.none() | NOT_AMOUNTS,
)
@example(buy=0.1, available=0.1, used=0.2, bad=None)
@NO_DEADLINE
def test_reply_amounts_exact(buy: float, available: float, used: float, bad: object) -> None:
    """PBT fyers_broker.reply_amounts_exact (trading-safety rules 7 and 8): the adapter's parsers
    read a position's average and the funds rows (JSON floats) as the Decimal each float's
    shortest text names, "Total Balance" is never needed (Q1: not equity), and an amount that
    is missing or not a finite number raises, while a fund row HAT does not read never does.
    Harm: 0.1 + 0.2 in float puts 0.30000000000000004 of free cash into sizing, a missing
    average reads as a cost of 0, or an odd collateral row blocks every entry."""
    row = {"symbol": "NSE:TCS-EQ", "netQty": 5, "buyAvg": buy, "productType": "CNC"}
    unread = {"title": "Collaterals", "equityAmount": "NaN"}  # a row HAT does not read
    funds = funds_reply(available, used)
    funds["fund_limit"] = [*funds["fund_limit"][:2], unread]  # no "Total Balance" row
    if bad is not None:
        row["buyAvg"] = funds["fund_limit"][1]["equityAmount"] = bad
        with pytest.raises((ValueError, TypeError, ArithmeticError)):
            parse_positions([row])
        with pytest.raises((ValueError, TypeError, ArithmeticError)):
            parse_funds(funds)
        return
    [position] = parse_positions([row])
    assert position.avg_price == Decimal(repr(buy))
    got = parse_funds(funds)
    assert (got.available, got.used) == (Decimal(repr(available)), Decimal(repr(used)))


def test_hung_calls_replace_at_limit() -> None:
    """The pool is replaced once `limit` timed-out calls of one generation still hold workers;
    a call that returned frees its count, and a call of a replaced generation counts toward
    nothing. Harm: hung SDK calls fill every worker and each later call, exits included, times
    out in the queue until a restart."""
    hung = HungCalls(limit=3)
    assert [hung.timed_out(0), hung.timed_out(0)] == [False, False]
    hung.returned(0)
    assert hung.timed_out(0) is False  # one returned: two still hung
    assert hung.timed_out(0) is True  # three hung: replace
    assert (hung.generation, hung.count) == (1, 0)
    hung.returned(0)  # an old-pool call finally returned
    assert hung.count == 0
    assert hung.timed_out(0) is False  # and an old-pool timeout counts toward nothing
    assert hung.count == 0


def test_sdk_requests_get_a_timeout() -> None:
    """Every SDK request carries `SDK_HTTP_TIMEOUT` unless the SDK set its own (it sets none).
    Harm: a stalled socket holds a worker thread until kernel keepalive."""
    sent: list[object] = []
    bounded = _with_timeout(lambda _method, _url, **kwargs: sent.append(kwargs.get("timeout")))
    bounded("POST", "https://example.invalid/orders")
    bounded("GET", "https://example.invalid/orders", timeout=None)
    bounded("GET", "https://example.invalid/orders", timeout=1)
    assert sent == [SDK_HTTP_TIMEOUT, SDK_HTTP_TIMEOUT, 1]


def test_the_day_cap_spends_the_day_until_the_next() -> None:
    """The first call the day cap refuses marks the day spent (Q11), and the next day starts
    unspent: entries stop for the rest of the day, never for the next."""
    today = BUDGET_DAY
    budget = CallBudget(CallLimits(100, 100, 5), clock=lambda: 0.0, day=lambda: today)
    for _ in range(4):  # the reads' share of 5
        assert budget.take(READ, 0.0, today) == 0
    assert not budget.day_spent()
    assert math.isinf(budget.take(READ, 0.0, today))
    assert budget.day_spent()
    assert not budget.writes_spent()  # a sale is not held back while writes remain
    assert budget.take(WRITE, 0.0, today) == 0  # the writes' share still goes out
    assert budget.writes_spent()  # the day's last write
    assert math.isinf(budget.take(WRITE, 0.0, today))
    today = BUDGET_DAY + timedelta(days=1)
    assert not budget.day_spent()
    assert not budget.writes_spent()


def test_a_minus_360_reply_spends_the_day() -> None:
    """Fyers' -360 (the plan's daily requests spent), answered to a real read through the
    transport, marks the budget's day spent; a 429 (a per-second or per-minute limit) and an
    ok reply do not."""
    for reply, spent in (
        ({"s": "error", "code": -360, "message": "limit"}, True),
        ({"s": "error", "code": 429, "message": "limit"}, False),
        (funds_reply(1000), False),
    ):
        budget = CallBudget(CallLimits(), day=lambda: BUDGET_DAY)
        sdk = RecordingSdk(reply)
        broker = FyersBroker(lambda s=sdk: s, breaker=CircuitBreaker(), budget=budget)
        try:
            broker.fetch_funds()
        except FyersUnavailableError:
            pass
        assert budget.day_spent() is spent, reply
        assert budget.writes_spent() is spent, reply  # Fyers refuses the writes too
    code = FyersBroker.day_refused_code
    assert not day_refused({"s": "error", "code": 429}, code)
    assert not day_refused({"s": "ok", "code": 200}, code)
    assert day_refused({"s": "error", "code": -360}, code)
    assert not day_refused({"s": "error", "code": -360}, None)  # a broker without such a code
