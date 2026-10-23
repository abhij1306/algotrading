"""PBT for order tags, the Fyers payload mapping, fail-closed broker reads and idempotent
submission.

Every property runs the real adapter, tag encoder and order path on `FakeBroker` or a recording
stand-in for the Fyers SDK model. Nothing reaches a real broker.
"""

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hat.fakes.broker import FakeBroker, funds_reply, unconfirmed
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from hat_exec_support import SYMBOLS, adapter_over
from hypothesis import assume, example, given
from hypothesis import strategies as st

from app.hat.execution.broker import (
    BrokerGtt,
    BrokerOrder,
    BrokerUnavailableError,
    GttModify,
    GttRequest,
    InvalidIntentError,
    OrderIntent,
    OrderType,
    Side,
)
from app.hat.execution.fyers_adapter import FYERS_CAPABILITIES, FyersHatBroker
from app.hat.execution.fyers_payloads import map_fyers_status
from app.hat.execution.order_book import (
    TagBook,
    find_gtt,
)
from app.hat.execution.reconcile import (
    DUPLICATE_HT_ORDER,
    Mismatch,
    find_mismatches,
)
from app.hat.execution.tags import (
    BASE32_ALPHABET,
    MAX_SEQUENCE,
    PURPOSE_LETTERS,
    SEQUENCE_ALPHABET,
    TAG_LENGTH,
    TAG_PATTERN,
    TagExhaustedError,
    encode_tag,
    is_hat_tag,
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
LETTER_PURPOSES = {letter: purpose for purpose, letter in PURPOSE_LETTERS.items()}


def decode(tag: str) -> tuple[str, int, int]:
    """(purpose, sequence, 80-bit signal fragment) of a HAT tag, read back from its characters."""
    fragment = 0
    for char in tag[4:]:
        fragment = (fragment << 5) | BASE32_ALPHABET.index(char)
    return LETTER_PURPOSES[tag[2]], SEQUENCE_ALPHABET.index(tag[3]), fragment


def fragment_of(signal: uuid.UUID) -> int:
    return signal.int & ((1 << 80) - 1)


# --- tag shape ------------------------------------------------------------------------------


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_shape_and_roundtrip(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """The shape regex, length 20, and the decode of what was encoded."""
    tag = encode_tag(signal, purpose, sequence)
    assert re.fullmatch(r"HT[EDGPFL][0-9A-Z][0-9A-HJKMNP-TV-Z]{16}", tag)
    assert len(tag) == TAG_LENGTH == 20
    assert decode(tag) == (purpose, sequence, fragment_of(signal))


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_format(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """`^HT[A-Za-z0-9]{18}$`, the 16-character signal part decodes to its fragment."""
    tag = encode_tag(signal, purpose, sequence)
    assert re.fullmatch(r"HT[A-Za-z0-9]{18}", tag)
    assert decode(tag)[2] == fragment_of(signal)


@given(SIGNALS, PURPOSES, SEQUENCES)
def test_encode_decode_roundtrip(signal: uuid.UUID, purpose: str, sequence: int) -> None:
    """20 alphanumeric characters starting HT, never the terminal's ST prefix, and
    in the alphabet and length the broker accepts."""
    tag = encode_tag(signal, purpose, sequence)
    assert len(tag) == 20
    assert tag.isalnum()
    assert tag.startswith("HT")
    assert not tag.startswith("ST")
    assert FYERS_ALLOWED.fullmatch(tag)
    assert len(tag) <= FYERS_CAPABILITIES.tag_max_len
    assert decode(tag)[0] == purpose


@given(st.text(max_size=30))
def test_only_the_tag_shape_is_a_hat_tag(text: str) -> None:
    """A string that is not exactly the tag shape is never read as HAT's."""
    assert is_hat_tag(text) == bool(TAG_PATTERN.fullmatch(text))


@given(
    st.integers(min_value=0, max_value=(1 << 48) - 1),
    st.integers(min_value=2, max_value=150),
)
def test_tags_are_unique_within_one_millisecond(clock_ms: int, count: int) -> None:
    """Distinct signals made in one millisecond give distinct tags for every
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


# --- the Fyers payload ----------------------------------------------------------------------

prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("99999.95"), places=2)
quantities = st.integers(min_value=1, max_value=100_000)
db_symbols = st.sampled_from(sorted(SYMBOLS))
order_types = st.sampled_from(list(OrderType))
tags_ = st.builds(encode_tag, SIGNALS, PURPOSES, SEQUENCES)
gtt_tags = st.builds(encode_tag, SIGNALS, st.just("stop_gtt"), SEQUENCES)
# Tags a GTT is refused with: none, another purpose's HAT tag, a foreign one
not_gtt_tags = st.one_of(
    st.none(), tags_.filter(lambda t: decode(t)[0] != "stop_gtt"), st.just("MYSTOP1")
)


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
    """The SDK receives exactly the documented keys with the documented values."""
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
    """An order is sent under the broker symbol stored for its
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


@given(valid_intents(), st.sampled_from(["price", "trigger_price"]), st.sampled_from([float, str]))
def test_float_intent_price_refused(intent: OrderIntent, field: str, form: type) -> None:
    """PBT tags.float_intent_price_refused (trading-safety rule 8): an intent whose price
    or trigger is a float or text, even one equal to a valid price, raises InvalidIntentError
    before the SDK is called; the same intent with the Decimal is placed. Harm: a float's binary
    rounding, or text such as "1_5", reaches an order price unseen."""
    price = getattr(intent, field)
    assume(price is not None)
    sdk = RecordingSdk()
    adapter = adapter_over(sdk)
    bad = OrderIntent(**{**intent.__dict__, field: form(price)})
    with pytest.raises(InvalidIntentError):
        adapter.place_order(bad)
    assert sdk.calls == []
    assert adapter_over(sdk).place_order(intent).status == "placed"


@given(valid_intents())
def test_intent_product_cnc(intent: OrderIntent) -> None:
    """Every payload the adapter sends is CNC, and any other product
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
    db_symbols,
    quantities,
    prices,
    prices,
    st.text(min_size=1, max_size=8, alphabet="0123456789"),
    gtt_tags,
    not_gtt_tags,
)
def test_gtt_payloads(
    symbol: str,
    qty: int,
    trigger: Decimal,
    limit: Decimal,
    gtt_id: str,
    tag: str,
    other: str | None,
) -> None:
    """The GTT place, modify and cancel payloads are exact, CNC, single-leg, and the
    place carries the stop's HT tag as `orderTag` (Fyers takes a tag on a GTT, recorded
    2026-10-22); a GTT without a stop_gtt HT tag is refused before any SDK call."""
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    untagged = GttRequest(symbol, qty, trigger, min(limit, trigger), tag=other)
    with pytest.raises(InvalidIntentError):
        broker.place_gtt_stop(untagged)
    if limit >= trigger:
        request = GttRequest(symbol, qty, trigger, limit, tag=tag)
        with pytest.raises(InvalidIntentError):
            broker.place_gtt_stop(request)
        assert sdk.calls == []
        return
    assert sdk.calls == []
    placed = broker.place_gtt_stop(GttRequest(symbol, qty, trigger, limit, tag=tag))
    assert placed.status == "placed"
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
                "orderTag": tag,
            },
        ),
        ("modify_gtt_order", {"id": gtt_id, "orderInfo": {"leg1": leg}}),
        ("cancel_gtt_order", {"id": gtt_id}),
    ]


def test_gtt_refuses_a_non_cnc_or_non_sell_request() -> None:
    sdk = RecordingSdk()
    broker = adapter_over(sdk)
    tag = encode_tag(uuid.UUID(int=1), "stop_gtt", 0)
    for request in (
        GttRequest("SBIN", 1, Decimal(100), Decimal(99), product="MTF", tag=tag),
        GttRequest("SBIN", 1, Decimal(100), Decimal(99), side=Side.BUY, tag=tag),
        GttRequest("SBIN", 0, Decimal(100), Decimal(99), tag=tag),
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
        fake.tradebook, fake.holdings = [row], [row]
        # an untagged order row that cannot be read is skipped (D118); an HT-tagged one fails
        fake.orders = [{**row, "orderTag": encode_tag(uuid.UUID(int=7), "entry", 0)}]
        fake.positions = [{"symbol": SECRET}]
        fake.available = funds_reply(SECRET)
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
    """A rejected, lost or raising write has a fixed code, never the broker's words."""
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
@example("¹", None, None)  # a Unicode digit `int` refuses (found by Hypothesis)
@example("--1", None, None)
@example("1", None, None)
def test_broker_status_mapping_is_total(code: object, filled: object, qty: object) -> None:
    """Every broker status maps to exactly one of our states (it never raises), and a status the
    mapping does not know is `unconfirmed` (it blocks a retry until looked up), never filled or
    cancelled. A status sent as an ASCII integer string reads as that integer."""
    status = map_fyers_status(code, filled, qty)
    assert status in ORDER_STATUSES
    known = {1, 2, 4, 5, 6, 7}
    if isinstance(code, str) and re.fullmatch(r"-?[0-9]+", code.strip()):
        code = int(code)
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
    """For every order book mixing HT, ST and untagged orders, the result depends
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


@given(
    st.lists(st.tuples(tags_, st.sampled_from(sorted(SYMBOLS))), max_size=8),
    st.sets(tags_, max_size=4),
)
def test_a_tag_twice_at_the_broker_blocks_the_account(ht, known) -> None:  # noqa: ANN001
    """For every order book, each HT tag the broker holds more than one
    order for is reported once as `duplicate_ht_order` for the whole account (the bot cannot be
    told), known to HAT or not; a tag held once is never reported so. Harm: one logical order
    reaches the broker twice and the account keeps opening positions as if nothing happened."""
    mismatches = find_mismatches(
        broker_orders=[_order(t, s) for t, s in ht],
        broker_qty={},
        hat_tags=known | {t for t, _ in ht},
        hat_positions=[],
        hat_symbols=set(),
    )
    tags = [t for t, _ in ht]
    twice = {t for t in tags if tags.count(t) > 1}
    found = [m for m in mismatches if m.kind == DUPLICATE_HT_ORDER]
    assert sorted(m.key for m in found) == sorted(twice)
    assert all(m.bot_id is None for m in found)


DUPLICATES = st.lists(
    st.tuples(
        st.sampled_from(["placed", "filled", "cancelled", "rejected", "expired"]),
        st.integers(min_value=0, max_value=2),
    ),
    min_size=1,
    max_size=4,
)


@given(tags_, DUPLICATES, st.one_of(st.none(), st.integers(min_value=0, max_value=4)))
def test_a_duplicated_tag_is_read_one_way(tag: str, orders: list, owned: int | None) -> None:
    """Of the broker orders under one HT tag, a row that holds a broker order id
    reads that order only (nothing when the book does not hold it), and a row without one reads
    the first order the broker has not ended unfilled, or the first when every one is. Harm:
    a dead duplicate's status ends a live entry's signal, or a live duplicate's fills are
    refused."""
    book = [
        replace(_order(tag, "SBIN"), order_id=f"D-{n}", status=status, filled_qty=filled)
        for n, (status, filled) in enumerate(orders)
    ]
    found = TagBook.of(book).for_row(
        HatOrder(tag=tag, broker_order_id=None if owned is None else f"D-{owned}")
    )
    if owned is not None:
        assert found is (book[owned] if owned < len(book) else None)
        return
    alive = [
        o for o in book if o.filled_qty or o.status not in ("cancelled", "rejected", "expired")
    ]
    assert found is (alive or book)[0]


@st.composite
def gtt_books(draw: st.DrawFn) -> tuple[SimpleNamespace, list[BrokerGtt], set[str]]:
    """A lost stop_gtt row, a GTT book around it (GTTs under its tag, another HAT GTT tag or
    none, on its symbol or another, of its shape or not) and the GTT ids other rows hold."""
    tag = draw(gtt_tags)
    other = draw(gtt_tags.filter(lambda t: t != tag))
    row = SimpleNamespace(tag=tag, qty=draw(st.integers(1, 3)), trigger_price=Decimal("100"))
    book = [
        BrokerGtt(
            f"G{i}",
            draw(st.sampled_from(["SBIN", "SBIN", "SBIN", "RELIANCE"])),
            draw(st.sampled_from([Side.SELL, Side.SELL, Side.BUY])),
            draw(st.sampled_from([row.qty, row.qty, 1, 2, 3])),
            draw(st.sampled_from([Decimal("100"), Decimal("100"), Decimal("99.95")])),
            draw(st.sampled_from([tag, other, None, None])),
            draw(st.booleans()),  # foreign: shown under a tag that is not a HAT GTT's
        )
        for i in range(draw(st.integers(0, 6)))
    ]
    book = [replace(g, foreign=g.foreign and g.tag is None) for g in book]
    claimed = {gtt.gtt_id for gtt in book if draw(st.sampled_from([False, False, True]))}
    return row, book, claimed


_LOST = SimpleNamespace(tag=encode_tag(uuid.UUID(int=1), "stop_gtt", 0), qty=1)
_LOST.trigger_price = Decimal("100")


@given(gtt_books())
@example(  # a foreign GTT of exactly the row's shape, alone on the symbol: never adopted
    (_LOST, [BrokerGtt("G0", "SBIN", Side.SELL, 1, Decimal("100"), None, True)], set())
)
def test_a_lost_gtt_is_found_by_its_tag(
    book: tuple[SimpleNamespace, list[BrokerGtt], set[str]],
) -> None:
    """The GTT a lost stop_gtt answer settles to is one no other row
    holds on the row's symbol: the one under the row's own tag when the book shows one (at any
    quantity or trigger), else one shown untagged of exactly the row's side, quantity and
    trigger; never one under another HAT GTT's tag or a foreign one. Harm: another position's
    stop, or a GTT HAT did not place, is adopted (and later cancelled or moved), leaving that
    position unprotected."""
    row, gtts, claimed = book
    found = find_gtt(gtts, row, "SBIN", claimed)  # type: ignore[arg-type]
    free = [g for g in gtts if g.gtt_id not in claimed and g.symbol == "SBIN"]
    own = [g for g in free if g.tag == row.tag]
    shaped = [
        g
        for g in free
        if g.tag is None
        and not g.foreign
        and g.side is Side.SELL
        and g.qty == row.qty
        and g.trigger_price == row.trigger_price
    ]
    assert found == (own[0] if own else shaped[0] if shaped else None)


@given(st.floats(allow_nan=False, allow_infinity=False).filter(lambda x: not x.is_integer()))
def test_a_fractional_status_code_is_unconfirmed(code: float) -> None:
    """A malformed code such as 2.7 is never truncated to a real status."""
    assert map_fyers_status(code, 0, 0) == "unconfirmed"


GTT_TAG = encode_tag(uuid.UUID(int=1), "stop_gtt", 0)


@given(st.dictionaries(st.sampled_from(["s", "message", "code"]), st.text(max_size=5), max_size=3))
def test_a_gtt_reply_that_is_not_an_explicit_answer_is_unconfirmed(reply: dict) -> None:
    """Only `ok` with an id is placed and only an explicit `error` is rejected; any other
    dictionary may have landed."""
    result = adapter_over(RecordingSdk(reply=reply)).place_gtt_stop(
        GttRequest("SBIN", 1, Decimal("100"), Decimal("99"), tag=GTT_TAG)
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
# two senders of one reserved tag; the GTT book read
# ---------------------------------------------------------------------------------------------


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
    hat_tag = draw(gtt_tags)
    # how Fyers shows the tag (recorded 2026-10-22): "<n>:GTT<tag>", "<n>:GTTUntagged", absent;
    # a foreign tag or another purpose's HAT tag is not one of HAT's GTTs
    shown, tag = draw(
        st.sampled_from(
            [
                (f"1:GTT{hat_tag}", hat_tag),
                (f"GTT{hat_tag}", hat_tag),
                (hat_tag, hat_tag),
                ("2:GTTUntagged", None),
                (None, None),
                ("1:GTTMYSTOP", "foreign"),
                (f"1:GTT{encode_tag(uuid.UUID(int=3), 'entry', 0)}", "foreign"),
            ]
        )
    )
    leg = {"price": float(trigger) - 1, "triggerPrice": float(trigger), "qty": qty}
    # the book's keys as recorded live, or the ones a GTT is placed with
    side_key, product_key = draw(
        st.sampled_from([("tran_side", "product_type"), ("side", "productType")])
    )
    row: dict = {"id": gtt_id, "symbol": SYMBOLS["SBIN"], side_key: -1, product_key: product}
    if shown is not None:
        row["ordertag"] = shown
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
    foreign = tag == "foreign"
    shown_tag = None if foreign else tag
    expected = (
        BrokerGtt(gtt_id, "SBIN", Side.SELL, qty, trigger, shown_tag, foreign) if resting else None
    )
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
    """`gtts()` lists exactly the single-leg CNC GTTs that rest or
    have triggered (the flat book fields, `orderInfo.leg1` or `leg1`; the live keys `tran_side`,
    `product_type` or the placement keys `side`, `productType`), each with the stop_gtt HT tag
    Fyers shows on it ("<n>:GTT<tag>") or None (untagged, a foreign tag, another purpose's HAT
    tag); a cancelled, rejected or expired GTT, an OCO or another product is left out, and a
    single-leg CNC row with a status code it does not know makes the book unreadable. Harm: a
    lost GTT is matched to one that never sold and no longer rests, leaving the position without
    a stop; or a triggered or unknown one is read as gone and a second GTT is placed under
    shares already sold."""
    book = [row for row, _ in rows]
    single = {"id": "G0", "symbol": SYMBOLS["SBIN"], "tran_side": -1, "product_type": "CNC"}
    single.update(price_trigger=100.0, qty=1)
    if odd is not None:
        # Someone else's GTT (an OCO, or intraday) with any status is not HAT's to read.
        foreign = [{**single, "gtt_oco_ind": 2, "ord_status": odd}]
        foreign.append({**single, "product_type": "INTRADAY", "ord_status": odd})
        expected = [g for _, g in rows if g is not None]
        assert adapter_over(GttBookSdk([*book, *foreign])).gtts() == expected
        unknown = adapter_over(GttBookSdk([*book, {**single, "ord_status": odd}]))
        with pytest.raises(BrokerUnavailableError):
            unknown.gtts()
        return
    assert adapter_over(GttBookSdk(book)).gtts() == [g for _, g in rows if g is not None]
