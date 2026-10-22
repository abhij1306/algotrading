"""PBT for order tags, the Fyers payload mapping, fail-closed broker reads and idempotent
submission (HAT-BRK-081, -090, -100, -101, -102, -112, HAT-DEC-I10, HAT-EXEC-013, HAT-SCH-053).

Every property runs the real adapter, tag encoder and order path on `FakeBroker` or a recording
stand-in for the Fyers SDK model. Nothing reaches a real broker.
"""

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from hat.fakes.broker import FakeBroker, accepted, rejected, unconfirmed
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from hat_exec_support import SYMBOLS, adapter_over, exec_env, live_session
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.execution import submit
from app.hat.execution.broker import (
    BrokerGtt,
    BrokerOrder,
    BrokerUnavailableError,
    GttModify,
    GttRequest,
    InvalidIntentError,
    OrderIntent,
    OrderType,
    SessionStatus,
    Side,
)
from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker, map_fyers_status
from app.hat.execution.fyers_broker import BROKER_CIRCUIT_OPEN, FYERS_ORDER_TAG_PATTERN
from app.hat.execution.reconcile import Mismatch, find_mismatches
from app.hat.execution.submit import (
    OrderRefusedError,
    reserve_order,
    send_order,
)
from app.hat.execution.tags import (
    MAX_SEQUENCE,
    PURPOSE_LETTERS,
    TAG_LENGTH,
    TAG_PATTERN,
    TagExhaustedError,
    decode_tag,
    encode_tag,
    is_hat_tag,
    signal_fragment,
)
from app.hat.ids import Uuid7Generator, uuid7
from app.hat.models.runtime import ORDER_PURPOSES, ORDER_STATUSES, HatOrder

PURPOSES = st.sampled_from(sorted(PURPOSE_LETTERS))
SEQUENCES = st.integers(min_value=0, max_value=MAX_SEQUENCE)
ANY_UUID = st.integers(min_value=0, max_value=(1 << 128) - 1).map(lambda n: uuid.UUID(int=n))
UUID7 = st.builds(
    lambda ms, count: Uuid7Generator(clock_ms=lambda: ms).next() if count else uuid7(),
    st.integers(min_value=0, max_value=(1 << 48) - 1),
    st.integers(min_value=0, max_value=1),
)
SIGNALS = st.one_of(ANY_UUID, UUID7)
FYERS_ALLOWED = re.compile(r"^[A-Za-z0-9]{1,20}$")


# --- tag shape ------------------------------------------------------------------------------


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_shape_and_roundtrip(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """HAT-BRK-100: the shape regex, length 20, and the decode of what was encoded."""
    tag = encode_tag(signal, purpose, sequence)
    assert re.fullmatch(r"HT[EDGPFL][0-9A-Z][0-9A-HJKMNP-TV-Z]{16}", tag)
    assert len(tag) == TAG_LENGTH == 20
    decoded = decode_tag(tag)
    assert (decoded.purpose, decoded.sequence) == (purpose, sequence)
    assert decoded.fragment == signal.int & ((1 << 80) - 1)
    assert decoded.fragment == signal_fragment(signal)


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_format(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """HAT-SCH-053: `^HT[A-Za-z0-9]{18}$`, the 16-character signal part decodes to its fragment."""
    tag = encode_tag(signal, purpose, sequence)
    assert re.fullmatch(r"HT[A-Za-z0-9]{18}", tag)
    assert decode_tag(tag).fragment == signal_fragment(signal)


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_encode_decode_roundtrip(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """HAT-DEC-I10: 20 alphanumeric characters starting HT, never the terminal's ST prefix, and
    in the alphabet and length the broker accepts."""
    tag = encode_tag(signal, purpose, sequence)
    assert len(tag) == 20
    assert tag.isalnum()
    assert tag.startswith("HT")
    assert not tag.startswith("ST")
    assert FYERS_ALLOWED.fullmatch(tag)
    assert FYERS_ORDER_TAG_PATTERN.fullmatch(tag)  # the pattern the Fyers client itself enforces
    assert len(tag) <= FYERS_CAPABILITIES.tag_max_len
    assert decode_tag(tag).purpose == purpose


@given(st.text(max_size=30))
def test_decode_refuses_what_is_not_a_hat_tag(text: str) -> None:
    """A string that is not exactly the tag shape is never decoded (nor read as HAT's)."""
    assert is_hat_tag(text) == bool(TAG_PATTERN.fullmatch(text))
    if not is_hat_tag(text):
        with pytest.raises(Exception, match="not a HAT order tag"):
            decode_tag(text)


@given(
    st.integers(min_value=0, max_value=(1 << 48) - 1),
    st.integers(min_value=2, max_value=150),
)
def test_tags_are_unique_within_one_millisecond(clock_ms: int, count: int) -> None:
    """HAT-BRK-100: distinct signals made in one millisecond give distinct tags for every
    (signal, purpose, sequence), and one signal's tags never repeat across purpose or sequence."""
    generator = Uuid7Generator(clock_ms=lambda: clock_ms)
    signals = [generator.next() for _ in range(count)]
    assert len(set(signals)) == count
    tags = {}
    for signal in signals:
        for purpose in PURPOSE_LETTERS:
            for sequence in (0, 1, MAX_SEQUENCE):
                tag = encode_tag(signal, purpose, sequence)
                assert tag not in tags, (tags[tag], (signal, purpose, sequence))
                tags[tag] = (signal, purpose, sequence)


@given(SIGNALS, PURPOSES, st.integers(min_value=MAX_SEQUENCE + 1, max_value=1000))
def test_a_37th_order_is_refused(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    with pytest.raises(TagExhaustedError):
        encode_tag(signal, purpose, sequence)


# --- sequence from the order table ----------------------------------------------------------


@given(
    st.lists(PURPOSES, min_size=1, max_size=40),
)
@settings(max_examples=60, suppress_health_check=[HealthCheck.too_slow])
def test_sequence_counts_earlier_orders(purposes: list[str]) -> None:
    """HAT-BRK-100: a new order's sequence is the number of earlier orders of the same signal and
    purpose (so a retry of one order, which reuses its tag, never moves it), and past 36 orders
    the order is refused and no row is written."""
    env = exec_env()
    signal = uuid7()
    seen: dict[str, int] = {}
    tags = set()
    for purpose in purposes:
        count = seen.get(purpose, 0)
        if count > MAX_SEQUENCE:
            request = env.request(signal, purpose)
            with pytest.raises(TagExhaustedError):
                reserve_order(env.session, request)
            continue
        tag = reserve_order(env.session, env.request(signal, purpose))
        assert decode_tag(tag).sequence == count
        assert tag not in tags
        tags.add(tag)
        seen[purpose] = count + 1


def test_a_signal_with_36_orders_of_one_purpose_is_refused_the_37th() -> None:
    env = exec_env()
    signal = uuid7()
    for _ in range(36):
        reserve_order(env.session, env.request(signal, "exit_partial"))
    request = env.request(signal, "exit_partial")
    with pytest.raises(TagExhaustedError):
        reserve_order(env.session, request)
    assert len({o.tag for o in env.session.query(type(_row(env))).all()}) == 36


def _row(env):  # noqa: ANN001, ANN202
    from app.hat.models import HatOrder

    return env.session.query(HatOrder).first()


# --- the Fyers payload ----------------------------------------------------------------------

prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("99999.95"), places=2)
quantities = st.integers(min_value=1, max_value=100_000)
db_symbols = st.sampled_from(sorted(SYMBOLS))
order_types = st.sampled_from(list(OrderType))
tags_ = st.builds(encode_tag, SIGNALS, PURPOSES, SEQUENCES)


@st.composite
def valid_intents(draw: st.DrawFn) -> OrderIntent:
    order_type = draw(order_types)
    needs_limit = order_type in {OrderType.LIMIT, OrderType.SL}
    needs_trigger = order_type in {OrderType.SL, OrderType.SL_M}
    return OrderIntent(
        symbol=draw(db_symbols),
        side=draw(st.sampled_from(list(Side))),
        qty=draw(quantities),
        order_type=order_type,
        tag=draw(tags_),
        price=draw(prices) if needs_limit else None,
        trigger_price=draw(prices) if needs_trigger else None,
    )


FYERS_TYPE = {"MARKET": 2, "LIMIT": 1, "SL-M": 3, "SL": 4}


@given(valid_intents())
def test_fyers_payload_mapping(intent: OrderIntent) -> None:
    """HAT-BRK-090: the SDK receives exactly the documented keys with the documented values."""
    sdk = RecordingSdk()
    result = adapter_over(sdk).place_order(intent)
    assert result.status == "placed"
    assert result.broker_order_id == "SDK-1"
    assert len(sdk.calls) == 1
    assert sdk.calls[0][0] == "place_order"
    assert sdk.calls[0][1] == {
        "symbol": SYMBOLS[intent.symbol],
        "qty": intent.qty,
        "type": FYERS_TYPE[intent.order_type.value],
        "side": 1 if intent.side is Side.BUY else -1,
        "productType": "CNC",
        "limitPrice": float(intent.price) if intent.price is not None else 0,
        "stopPrice": float(intent.trigger_price) if intent.trigger_price is not None else 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "orderTag": intent.tag,
    }


@given(valid_intents(), st.sampled_from(["EQ", "BE"]), st.booleans())
def test_order_goes_out_under_the_stored_broker_symbol(
    intent: OrderIntent, series: str, readable: bool
) -> None:
    """HAT-DATA-024, HAT-BRK-090: an order is sent under the broker symbol stored for its
    instrument (`-BE` for a trade-for-trade stock), and under the converted `-EQ` symbol when the
    stored symbols cannot be read."""
    sdk = RecordingSdk()
    stored = f"NSE:{intent.symbol}-{series}"

    def source() -> list[tuple[str, str]]:
        if not readable:
            raise RuntimeError("database down")
        return [(intent.symbol, stored)]

    result = FyersHatBroker(broker_over(sdk), instruments=source).place_order(intent)
    assert result.status == "placed"
    assert sdk.calls[0][1]["symbol"] == (stored if readable else SYMBOLS[intent.symbol])


bad_values = st.sampled_from([0, -1, None, True, 1.5, "10", Decimal("NaN"), Decimal("Infinity")])


@given(valid_intents(), st.data())
def test_invalid_intents_raise_before_any_sdk_call(
    intent: OrderIntent, data: st.DataObject
) -> None:
    """HAT-BRK-090: an invalid intent raises InvalidIntentError and the SDK is never called."""
    field = data.draw(
        st.sampled_from(["qty", "product", "tag", "symbol", "price", "trigger_price", "side"])
    )
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    if field == "qty":
        bad = OrderIntent(**{**intent.__dict__, "qty": data.draw(bad_values)})
    elif field == "product":
        bad = OrderIntent(
            **{
                **intent.__dict__,
                "product": data.draw(st.sampled_from(["MIS", "INTRADAY", "MTF", ""])),
            }
        )
    elif field == "tag":
        bad = OrderIntent(
            **{
                **intent.__dict__,
                "tag": data.draw(st.sampled_from(["", "ST" + "A" * 18, "HT", "x" * 21])),
            }
        )
    elif field == "symbol":
        bad = OrderIntent(
            **{**intent.__dict__, "symbol": data.draw(st.sampled_from(["", "bad symbol!", "NSE:"]))}
        )
    elif field == "side":
        bad = OrderIntent(**{**intent.__dict__, "side": "short"})
    else:  # a price the type does not take, or a missing or non-positive one it needs
        needs = (
            intent.order_type in {OrderType.LIMIT, OrderType.SL}
            if field == "price"
            else intent.order_type in {OrderType.SL, OrderType.SL_M}
        )
        wrong = (
            data.draw(st.sampled_from([None, Decimal(0), Decimal(-5), Decimal("NaN")]))
            if needs
            else data.draw(prices)
        )
        bad = OrderIntent(**{**intent.__dict__, field: wrong})
    with pytest.raises(InvalidIntentError):
        broker.place_order(bad)
    assert sdk.calls == []


@given(valid_intents())
def test_intent_product_cnc(intent: OrderIntent) -> None:
    """HAT-EXEC-013 / HAT-DEC-B24: every payload the adapter sends is CNC, and any other product
    is refused before the broker."""
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    broker.place_order(intent)
    assert sdk.calls[0][1]["productType"] == "CNC"
    other = OrderIntent(**{**intent.__dict__, "product": "INTRADAY"})
    with pytest.raises(InvalidIntentError):
        broker.place_order(other)
    assert len(sdk.calls) == 1


@given(
    db_symbols, quantities, prices, prices, st.text(min_size=1, max_size=8, alphabet="0123456789")
)
def test_gtt_payloads(symbol: str, qty: int, trigger: Decimal, limit: Decimal, gtt_id: str) -> None:
    """HAT-BRK-090: the GTT place, modify and cancel payloads are exact, CNC, single-leg."""
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    if limit >= trigger:
        request = GttRequest(symbol, qty, trigger, limit)
        with pytest.raises(InvalidIntentError):
            broker.place_gtt_stop(request)
        assert sdk.calls == []
        return
    assert broker.place_gtt_stop(GttRequest(symbol, qty, trigger, limit)).status == "placed"
    assert broker.modify_gtt(GttModify(gtt_id, qty, trigger, limit)).ok
    assert broker.cancel_gtt(gtt_id).ok
    leg = {"price": float(limit), "triggerPrice": float(trigger), "qty": qty}
    assert sdk.calls == [
        (
            "place_gtt_order",
            {
                "side": -1,
                "symbol": SYMBOLS[symbol],
                "productType": "CNC",
                "orderInfo": {"leg1": leg},
            },
        ),
        ("modify_gtt_order", {"id": gtt_id, "orderInfo": {"leg1": leg}}),
        ("cancel_gtt_order", {"id": gtt_id}),
    ]


def test_gtt_refuses_a_non_cnc_or_non_sell_request() -> None:
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    for request in (
        GttRequest("SBIN", 1, Decimal(100), Decimal(99), product="MTF"),
        GttRequest("SBIN", 1, Decimal(100), Decimal(99), side=Side.BUY),
        GttRequest("SBIN", 0, Decimal(100), Decimal(99)),
    ):
        with pytest.raises(InvalidIntentError):
            broker.place_gtt_stop(request)
    assert sdk.calls == []


# --- fail closed ----------------------------------------------------------------------------

SECRET = "SECRET-acct-9981-token"
READS = ("orders", "trades", "positions", "holdings", "funds")
FAILURES = st.sampled_from(["circuit", "raises", "error_payload", "timeout", "garbage"])


def _failing_fake(kind: str) -> FakeBroker:
    fake = FakeBroker()
    if kind == "circuit":
        fake.circuit_open = True
    elif kind == "raises":
        error = RuntimeError(SECRET)
        fake.orders = fake.tradebook = fake.positions = fake.holdings = error
        fake.available = error
    elif kind == "garbage":
        row = {"id": SECRET}  # a row missing every other field
        fake.orders, fake.tradebook, fake.holdings = [row], [row], [row]
        fake.positions = [{"symbol": SECRET}]
        fake.available = {"available": SECRET, "used": 0, "total": 0}
    return fake


@given(FAILURES, st.sampled_from(READS))
def test_broker_errors_fail_closed(kind: str, read: str) -> None:
    """HAT-BRK-081: for every injected failure, each read raises BrokerUnavailableError with a
    fixed code and no raw text, never returning an empty list."""
    if kind in {"error_payload", "timeout"}:
        sdk = RecordingSdk(
            reply={"s": "error", "message": SECRET}
            if kind == "error_payload"
            else TimeoutError(SECRET)
        )
        broker = adapter_over(sdk)
    else:
        broker = adapter_over_fake(_failing_fake(kind))
    method = getattr(broker, read)
    with pytest.raises(BrokerUnavailableError) as raised:
        method()
    error = raised.value
    assert error.code in {"broker_unavailable", "broker_response_invalid"}
    assert SECRET not in f"{error!s}{error!r}{error.args}"


def adapter_over_fake(fake: FakeBroker):  # noqa: ANN201
    from app.hat.execution.fyers_adapter import FyersHatBroker

    return FyersHatBroker(fake, instruments=lambda: list(SYMBOLS.items()))  # type: ignore[arg-type]


@given(st.sampled_from(["circuit", "reject", "unconfirmed", "raises"]), valid_intents())
def test_write_results_carry_no_broker_text(kind: str, intent: OrderIntent) -> None:
    """HAT-BRK-081: a rejected, lost or raising write has a fixed code, never the broker's words."""
    fake = FakeBroker()
    if kind == "circuit":
        fake.circuit_open = True
    elif kind == "reject":
        fake.place_reply = {
            "order_id": "",
            "status": "REJECTED",
            "message": SECRET,
            "details": SECRET,
        }
    elif kind == "unconfirmed":
        fake.place_reply = unconfirmed()
    else:
        fake.place_reply = RuntimeError(SECRET)
    result = adapter_over_fake(fake).place_order(intent)
    assert result.status in {"rejected", "unconfirmed"}
    assert result.broker_order_id is None
    assert SECRET not in repr(result)
    assert result.code in {"broker_rejected", "broker_unavailable", "submit_unconfirmed"}


# --- the status mapping is total ------------------------------------------------------------

odd_values = st.one_of(
    st.integers(min_value=-(10**12), max_value=10**12),
    st.none(),
    st.text(max_size=5),
    st.floats(allow_nan=True, allow_infinity=True),
    st.booleans(),
    st.lists(st.integers(), max_size=2),
)


@given(odd_values, odd_values, odd_values)
def test_broker_status_mapping_is_total(code: object, filled: object, qty: object) -> None:
    """Every broker status maps to exactly one of our states, and a status the mapping does not
    know is `unconfirmed` (it blocks a retry until looked up), never filled or cancelled."""
    status = map_fyers_status(code, filled, qty)
    assert status in ORDER_STATUSES
    known = {1, 2, 4, 5, 6, 7}
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        assert status == "unconfirmed"
    elif isinstance(code, float) and not code.is_integer():  # fractions, NaN, infinities
        assert status == "unconfirmed"
    elif int(code) not in known:
        assert status == "unconfirmed"


@given(st.integers(min_value=1, max_value=10_000), st.integers(min_value=0, max_value=10_000))
def test_working_orders_report_fills(qty: int, filled: int) -> None:
    assert map_fyers_status(2, filled, qty) == "filled"
    assert map_fyers_status(1, filled, qty) == "cancelled"
    assert map_fyers_status(5, filled, qty) == "rejected"
    assert map_fyers_status(7, filled, qty) == "expired"
    working = map_fyers_status(6, filled, qty)
    assert working == ("filled" if filled >= qty else "partially_filled" if filled else "placed")


# --- reconciliation scope -------------------------------------------------------------------


def _order(tag: str | None, symbol: str, qty: int = 5) -> BrokerOrder:
    return BrokerOrder(
        order_id=f"O-{tag}-{symbol}",
        tag=tag,
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        filled_qty=0,
        status="placed",
        product="CNC",
    )


foreign_tags = st.one_of(
    st.none(),
    st.just(""),
    st.builds(lambda s: "ST" + s, st.text("0123456789ABCDEF", min_size=18, max_size=18)),
    st.text("abcdef", min_size=1, max_size=12),
)


@given(
    st.lists(st.tuples(tags_, st.sampled_from(sorted(SYMBOLS))), max_size=6),
    st.lists(st.tuples(foreign_tags, st.sampled_from(sorted(SYMBOLS))), max_size=6),
    st.sets(tags_, max_size=4),
    st.dictionaries(st.sampled_from(sorted(SYMBOLS)), st.integers(min_value=0, max_value=20)),
)
def test_reconcile_scope(ht, foreign, known, quantities_) -> None:  # noqa: ANN001
    """HAT-BRK-112: for every order book mixing HT, ST and untagged orders, the result depends
    only on the HT-tagged subset."""
    ht_orders = [_order(t, s) for t, s in ht]
    foreign_orders = [_order(t, s) for t, s in foreign]

    def run(orders: list[BrokerOrder]) -> list[Mismatch]:
        return find_mismatches(
            broker_orders=orders,
            broker_qty=quantities_,
            hat_tags=known,
            hat_positions=[],
            hat_symbols=set(),
        )

    assert run(ht_orders + foreign_orders) == run(ht_orders)
    assert run(foreign_orders + ht_orders) == run(ht_orders)
    assert run(foreign_orders) == []


# --- persist before call, idempotent on tag, unconfirmed resolved by tag --------------------


class SimulatedCrash(BaseException):  # noqa: N818 - models a power cut, not an error
    """Raised between the broker call and the recording of its answer."""


class CrashingFake(FakeBroker):
    """A fake whose place lands the order and then 'crashes' the process when told to."""

    crash_next = False

    def place_order(self, order):  # noqa: ANN001, ANN201
        reply = super().place_order(order)
        if self.crash_next:
            self.crash_next = False
            if not any(r["orderTag"] == order["order_tag"] for r in self.book):
                self.book.append(self._book_row(order, {"order_id": "CRASH"}))
            raise SimulatedCrash
        return reply


class OrderSubmission(RuleBasedStateMachine):
    """Random sends of one reserved tag against a flaky broker: accepted, rejected, answer lost
    (order landed or not), a crash after the call, a failing order book."""

    def __init__(self) -> None:
        super().__init__()
        self.env = exec_env(fake=CrashingFake())
        live_session(self.env)
        self.signal = uuid7()
        self.tag = reserve_order(self.env.session, self.env.request(self.signal))
        self.fake: CrashingFake = self.env.fake  # type: ignore[assignment]

    def _send(self) -> None:
        try:
            send_order(
                self.env.session,
                self.env.broker,
                self.env.guard,
                self.tag,
                symbol="SBIN",
                bot_id=self.env.bot_id,
            )
        except (BrokerUnavailableError, SimulatedCrash):
            self.env.session.rollback()

    @rule()
    def send_accepted(self) -> None:
        self._send()

    @rule()
    def send_rejected(self) -> None:
        self.fake.script(
            "place_order",
            {"order_id": "", "status": "REJECTED", "message": SECRET, "details": None},
        )
        self._send()

    @rule()
    def send_answer_lost_order_landed(self) -> None:
        self.fake.script("place_order", unconfirmed(landed=True))
        self._send()

    @rule()
    def send_answer_lost_order_not_landed(self) -> None:
        self.fake.script("place_order", unconfirmed())
        self._send()

    @rule()
    def send_then_crash(self) -> None:
        self.fake.crash_next = True
        self._send()

    @rule()
    def send_while_the_circuit_is_open(self) -> None:
        self.fake.script("place_order", rejected(BROKER_CIRCUIT_OPEN))
        self._send()

    @rule()
    def send_with_book_down(self) -> None:
        self.fake.script("fetch_orderbook", BrokerUnavailableError("down"))
        self._send()

    @invariant()
    def at_most_one_broker_order_per_tag(self) -> None:
        landed = [row for row in self.fake.book if row["orderTag"] == self.tag]
        assert len(landed) <= 1, landed

    @invariant()
    def one_row_per_tag(self) -> None:
        from app.hat.models import HatOrder

        rows = self.env.session.query(HatOrder).filter(HatOrder.tag == self.tag).all()
        assert len(rows) == 1
        if rows[0].status == "placed":
            assert rows[0].broker_order_id

    @invariant()
    def every_place_call_follows_a_committed_pending_row(self) -> None:
        # The row exists from reserve_order on, before the first broker call (fresh session read).
        from app.hat.models import HatOrder

        # Roll back first: only a committed row survives, so a flushed-only row fails here.
        self.env.session.rollback()
        rows = self.env.session.query(HatOrder).filter(HatOrder.tag == self.tag).count()
        assert rows == 1 or not self.fake.calls_named("place_order")


def test_idempotent_order_submission() -> None:
    """HAT-BRK-101/-102: whatever sequence of sends, lost answers and crashes, one tag yields at
    most one broker order and one row, and a lost answer is resolved by looking the tag up."""
    run_state_machine_as_test(
        OrderSubmission,
        settings=settings(
            max_examples=120, stateful_step_count=14, deadline=None, derandomize=True
        ),
    )


@given(st.lists(st.sampled_from(["lost", "landed", "ok"]), min_size=1, max_size=6))
def test_a_timeout_is_looked_up_before_any_retry(outcomes: list[str]) -> None:
    """HAT-BRK-102: after a lost answer, a second place call with the tag happens only after an
    order-book lookup that found nothing; a found order is adopted, not placed again."""
    env = exec_env()
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7()))
    replies = {
        "lost": unconfirmed(),
        "landed": unconfirmed(landed=True),
        "ok": {"status": "SUBMITTED", "order_id": "B-ok"},
    }
    for outcome in outcomes:
        env.fake.script("place_order", replies[outcome])
        send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    names = [name for name, _ in env.fake.calls]
    placed_at = [i for i, name in enumerate(names) if name == "place_order"]
    for earlier, later in zip(placed_at, placed_at[1:], strict=False):
        assert "fetch_orderbook" in names[earlier:later], names
    assert len([r for r in env.fake.book if r["orderTag"] == tag]) <= 1


# An ok reply that does not carry the order book list where Fyers documents it.
KEYLESS_BOOKS = st.sampled_from(
    [
        {"s": "ok", "code": 200},
        {"s": "ok", "code": 200, "orderBook": None},
        {"s": "ok", "code": 200, "orderBook": {}},
        {"s": "ok", "code": 200, "data": {"orderBook": []}},
    ]
)


class LostThenKeyless(RecordingSdk):
    """A place whose answer is lost (the call raises mid-request), then an order book reply
    without its list."""

    def __init__(self, book: dict) -> None:
        super().__init__()
        self.book = book

    def place_order(self, data: dict) -> object:
        self.calls.append(("place_order", data))
        raise TimeoutError("read timed out")

    def orderbook(self) -> object:
        self.calls.append(("orderbook", None))
        return self.book


@given(book=KEYLESS_BOOKS, sends=st.integers(min_value=2, max_value=4))
@settings(deadline=None)
def test_a_keyless_book_never_reads_as_absent(book: dict, sends: int) -> None:
    """HAT-BRK-081, HAT-BRK-102: after a lost answer, an order book that answered ok without its
    list is unreadable, not empty: every later send raises `BrokerUnavailableError` and places
    nothing more under the tag. Harm: the lookup reads "not found" and the same order goes out
    twice (a doubled entry)."""
    env = exec_env()
    sdk = LostThenKeyless(book)
    env.broker = adapter_over(sdk)
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7()))
    first = send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert first.status == "unconfirmed"
    for _ in range(sends - 1):
        with pytest.raises(BrokerUnavailableError):
            send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert [name for name, _ in sdk.calls].count("place_order") == 1


refusal_cases = st.fixed_dictionaries(
    {
        "account_active": st.booleans(),
        "reconciled": st.booleans(),
        "blocked": st.booleans(),
        "session": st.sampled_from(list(SessionStatus)),
        "purpose": st.sampled_from(["entry", "stop_day", "exit_partial", "exit_full", "flatten"]),
    }
)


@given(refusal_cases)
def test_refusals_happen_before_the_broker(case: dict) -> None:
    """HAT-BRK-092/-110/-111, HAT-DEC-T14: an order is placed only for an active account, after a
    reconciliation this session, with no mismatch against the bot (entries), and a live session
    (entries); every refusal leaves the broker uncalled and the row pending."""
    env = exec_env(
        account_status="active" if case["account_active"] else "pending",
        reconciled=case["reconciled"],
    )
    live_session(env)
    status = case["session"]
    env.broker.session_status = lambda: status  # type: ignore[method-assign]
    if case["blocked"]:
        env.guard.blocked_bots.add(env.bot_id)
    signal = uuid7()
    tag = reserve_order(env.session, env.request(signal, case["purpose"]))
    is_entry = case["purpose"] == "entry"
    allowed = (
        case["account_active"]
        and case["reconciled"]
        and (not is_entry or (not case["blocked"] and status is SessionStatus.LIVE))
    )
    if allowed:
        out = send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
        assert out.status == "placed"
        assert env.fake.count("place_order") == 1
    else:
        with pytest.raises(OrderRefusedError):
            send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
        assert env.fake.count("place_order") == 0
        from app.hat.models import HatOrder

        assert env.session.query(HatOrder).one().status == "pending"


def test_an_unreachable_broker_is_not_a_rejection() -> None:
    """A place that was never sent (circuit open) raises and leaves the order pending, so it is
    neither marked rejected nor counted against the one retry; a later send places it once."""
    from app.hat.models import HatOrder

    env = exec_env()
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7()))
    env.fake.script("place_order", rejected(BROKER_CIRCUIT_OPEN))
    with pytest.raises(BrokerUnavailableError):
        send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert env.session.query(HatOrder).one().status == "pending"
    out = send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert out.status == "placed"
    assert len([r for r in env.fake.book if r["orderTag"] == tag]) == 1


def test_a_virtual_order_never_reaches_the_broker() -> None:
    """HAT-EXEC-010 (the adapter's half): a virtual row is refused before any broker call."""
    from app.hat.models import HatOrder

    env = exec_env()
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7()))
    env.session.query(HatOrder).update({"is_virtual": True})
    env.session.commit()
    with pytest.raises(OrderRefusedError):
        send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert env.fake.calls == []


def test_a_pending_gtt_is_not_retried_after_a_lost_answer() -> None:
    """07 §10: a GTT has no tag at Fyers, so an unconfirmed one cannot be looked up and is not
    placed again."""
    from app.hat.execution.submit import UnresolvedOrderError

    env = exec_env()
    live_session(env)
    request = env.request(uuid7(), "stop_gtt", trigger_price=Decimal("100"))
    tag = reserve_order(env.session, request)
    env.fake.script("place_gtt", unconfirmed())
    out = send_order(
        env.session,
        env.broker,
        env.guard,
        tag,
        symbol="SBIN",
        bot_id=env.bot_id,
        tick_size=Decimal("0.05"),
    )
    assert out.status == "unconfirmed"
    tick = Decimal("0.05")
    with pytest.raises(UnresolvedOrderError):
        send_order(
            env.session,
            env.broker,
            env.guard,
            tag,
            symbol="SBIN",
            bot_id=env.bot_id,
            tick_size=tick,
        )
    assert env.fake.count("place_gtt") == 1
    assert env.fake.calls_named("place_gtt")[0]["price"] == 99.0


def _send_gtt(env, tag: str, tick_size: Decimal | None = None):  # noqa: ANN001, ANN202
    return send_order(
        env.session,
        env.broker,
        env.guard,
        tag,
        symbol="SBIN",
        bot_id=env.bot_id,
        tick_size=tick_size,
    )


def test_a_gtt_refused_before_the_call_can_be_sent_later() -> None:
    """A GTT that was never sent (no tick, an unavailable broker) leaves no attempt marker, so
    the next call places it instead of treating it as unresolved."""
    from app.hat.models import HatOrder

    env = exec_env()
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7(), "stop_gtt", trigger_price=Decimal("100")))
    with pytest.raises(OrderRefusedError):
        _send_gtt(env, tag)  # no tick size
    env.fake.script("place_gtt", rejected(BROKER_CIRCUIT_OPEN))
    tick = Decimal("0.05")
    with pytest.raises(BrokerUnavailableError):
        _send_gtt(env, tag, tick_size=tick)
    assert env.session.query(HatOrder).one().placed_at is None
    out = _send_gtt(env, tag, tick_size=tick)
    assert out.status == "placed"
    assert env.fake.count("place_gtt") == 2


@given(st.floats(allow_nan=False, allow_infinity=False).filter(lambda x: not x.is_integer()))
def test_a_fractional_status_code_is_unconfirmed(code: float) -> None:
    """A malformed code such as 2.7 is never truncated to a real status."""
    assert map_fyers_status(code, 0, 0) == "unconfirmed"


@given(st.dictionaries(st.sampled_from(["s", "message", "code"]), st.text(max_size=5), max_size=3))
def test_a_gtt_reply_that_is_not_an_explicit_answer_is_unconfirmed(reply: dict) -> None:
    """Only `ok` with an id is placed and only an explicit `error` is rejected; any other
    dictionary may have landed."""
    result = adapter_over(RecordingSdk(reply=reply)).place_gtt_stop(
        GttRequest("SBIN", 1, Decimal("100"), Decimal("99"))
    )
    expected = "rejected" if reply.get("s") == "error" else "unconfirmed"
    assert result.status == expected


def test_several_bots_on_one_symbol_reconcile_against_their_sum() -> None:
    import uuid as _uuid

    from app.hat.execution.reconcile import HatPositionView

    bot_a, bot_b = _uuid.uuid4(), _uuid.uuid4()
    views = [HatPositionView(bot_a, "SBIN", 5), HatPositionView(bot_b, "SBIN", 5)]

    def run(quantity: int) -> list[Mismatch]:
        return find_mismatches(
            broker_orders=[],
            broker_qty={"SBIN": quantity},
            hat_tags=set(),
            hat_positions=views,
            hat_symbols=set(),
        )

    assert run(10) == []
    assert [(m.kind, m.bot_id) for m in run(12)] == [("quantity_difference", None)]
    assert {m.bot_id for m in run(0)} == {bot_a, bot_b}


def test_the_purposes_match_the_database_domain() -> None:
    assert set(PURPOSE_LETTERS) == set(ORDER_PURPOSES)
    assert datetime.now(UTC)  # keep the import honest for type checkers


# ---------------------------------------------------------------------------------------------
# HAT-EXEC-065: two senders of one reserved tag; the GTT book read
# ---------------------------------------------------------------------------------------------


@given(answer=st.sampled_from(["accepted", "rejected", "unconfirmed"]), retry=st.booleans())
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_a_reserved_tag_is_sent_by_one_sender(answer: str, retry: bool) -> None:
    """HAT-BRK-102, HAT-EXEC-065: when two senders read the same order before either stamped
    a new attempt (a fill sync and a stop job protecting one position), whether it was never
    sent or its earlier answer was lost and the lookup found nothing (`retry`), only the one
    that stamps first calls the broker, with a new attempt time from which a lost answer's
    settle wait runs; the other sends nothing and reports what the first left. Harm: two sell
    stops rest for the same shares, an entry is bought twice, or a resend still in flight is
    settled as absent."""
    env = exec_env()
    live_session(env)
    tag = reserve_order(env.session, env.request(uuid7()))
    earlier = None
    if retry:
        env.fake.script("place_order", unconfirmed())
        send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
        earlier = env.session.scalar(select(HatOrder.placed_at).where(HatOrder.tag == tag))
    other = Session(env.session.get_bind(), expire_on_commit=False)
    reply = {"accepted": accepted("B-1"), "rejected": rejected("RMS"), "unconfirmed": unconfirmed()}
    env.fake.script("place_order", reply[answer])
    build = submit._build_request
    first: list = []

    def racing(*args: object, **kwargs: object) -> object:
        if not first:  # the other sender runs between this one's read and its stamp
            first.append(None)
            first[0] = send_order(
                other, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id
            )
        return build(*args, **kwargs)

    with patch.object(submit, "_build_request", racing):
        late = send_order(env.session, env.broker, env.guard, tag, symbol="SBIN", bot_id=env.bot_id)
    assert first[0].broker_calls == 1
    assert late.broker_calls == 0
    assert late.status == first[0].status
    assert env.fake.count("place_order") == (2 if retry else 1)
    stamped = env.session.scalar(select(HatOrder.placed_at).where(HatOrder.tag == tag))
    assert earlier is None or stamped > earlier


GTT_SHAPES = st.sampled_from(["flat", "order_info", "leg1"])


@st.composite
def gtt_rows(draw: st.DrawFn) -> tuple[dict, BrokerGtt | None]:
    """A GTT book row and the resting GTT it stands for (None: not one of HAT's resting stops)."""
    gtt_id = f"G{draw(st.integers(min_value=1, max_value=999))}"
    qty = draw(st.integers(min_value=1, max_value=500))
    trigger = Decimal(draw(st.integers(min_value=1, max_value=99999))) / 20
    status = draw(st.sampled_from([None, 1, 2, 4, 5, 6, 7]))
    oco = draw(st.booleans())
    product = draw(st.sampled_from(["CNC", "CNC", "INTRADAY"]))
    shape = draw(GTT_SHAPES)
    leg = {"price": float(trigger) - 1, "triggerPrice": float(trigger), "qty": qty}
    row: dict = {"id": gtt_id, "symbol": SYMBOLS["SBIN"], "side": -1, "productType": product}
    if status is not None:
        row["ord_status"] = status
    if shape == "flat":
        row.update(price_limit=leg["price"], price_trigger=leg["triggerPrice"], qty=qty)
        row["gtt_oco_ind"] = 2 if oco else 1
    elif shape == "order_info":
        row["orderInfo"] = {"leg1": leg, **({"leg2": dict(leg)} if oco else {})}
    else:
        row.update(leg1=leg, **({"leg2": dict(leg)} if oco else {}))
    resting = status in (None, 2, 4, 6) and not oco and product == "CNC"
    expected = BrokerGtt(gtt_id, "SBIN", Side.SELL, qty, trigger) if resting else None
    return row, expected


class GttBookSdk(RecordingSdk):
    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self.rows = rows

    def gtt_orderbook(self) -> object:
        self.calls.append(("gtt_orderbook", None))
        return {"s": "ok", "code": 200, "orderBook": self.rows}


@given(rows=st.lists(gtt_rows(), max_size=6), odd=st.sampled_from([None, 3, 9]))
def test_the_gtt_book_reads_only_resting_stops(
    rows: list[tuple[dict, BrokerGtt | None]], odd: int | None
) -> None:
    """HAT-EXEC-065, HAT-BRK-081: `gtts()` lists exactly the single-leg CNC GTTs that rest or
    have triggered (the flat book fields, `orderInfo.leg1` or `leg1`); a cancelled, rejected or
    expired GTT, an OCO or another product is left out, and a single-leg CNC row with a status
    code it does not know makes the book unreadable. Harm: a lost GTT is matched to one that
    never sold and no longer rests, leaving the position without a stop; or a triggered or
    unknown one is read as gone and a second GTT is placed under shares already sold."""
    book = [row for row, _ in rows]
    single = {"id": "G0", "symbol": SYMBOLS["SBIN"], "side": -1, "productType": "CNC"}
    single.update(price_trigger=100.0, qty=1)
    if odd is not None:
        # Someone else's GTT (an OCO, or intraday) with any status is not HAT's to read.
        foreign = [{**single, "gtt_oco_ind": 2, "ord_status": odd}]
        foreign.append({**single, "productType": "INTRADAY", "ord_status": odd})
        expected = [g for _, g in rows if g is not None]
        assert adapter_over(GttBookSdk([*book, *foreign])).gtts() == expected
        with pytest.raises(BrokerUnavailableError):
            adapter_over(GttBookSdk([*book, {**single, "ord_status": odd}])).gtts()
        return
    assert adapter_over(GttBookSdk(book)).gtts() == [g for _, g in rows if g is not None]
