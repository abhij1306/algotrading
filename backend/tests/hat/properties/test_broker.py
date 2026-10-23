"""PBT for broker accounts, the session, the capability gate and the adapter's reads."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hat.fakes.broker import FakeBroker
from hat.fakes.fyers_sdk import RecordingSdk, broker_over
from hat_exec_support import SYMBOLS, adapter_over
from hat_fyers_support import LIVE_REPLIES
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.execution.accounts import onboarding_status
from app.hat.execution.broker import (
    CODE_RATE_LIMITED,
    CODE_SESSION_EXPIRED,
    CODE_UNAVAILABLE,
    BrokerUnavailableError,
    GttModify,
    GttRequest,
    OrderIntent,
    OrderType,
    SessionStatus,
    Side,
)
from app.hat.execution.fyers_adapter import (
    CODE_INVALID_RESPONSE,
    TOKEN_CHECK_SECONDS,
    FyersHatBroker,
    map_session,
)
from app.hat.execution.manage import (
    NEVER_SOLD,
    SETTLED,
    SOLD,
    UNKNOWN,
    UNREADABLE,
    WAIT,
    SaleGate,
    SaleStep,
    earlier_sale_verdict,
    sale_gate,
)
from app.hat.execution.modes import (
    Gates,
    ModeRefusedError,
    UnknownModeError,
    capability_gated,
    request_mode,
)
from app.hat.execution.position_desk import LOST_ANSWER_SETTLE, Verdict, settle_verdict
from app.hat.execution.session import (
    SessionOwnedError,
    SessionRegistry,
)
from app.hat.execution.stop_plans import stop_confirmed
from app.hat.execution.tags import encode_tag
from app.hat.models import HatOrder
from app.hat.risk.modes import MODE_VALUES, Mode


@given(
    st.one_of(st.sampled_from(tuple(Mode)), st.sampled_from(["live", "", "AUTO", "Shadow"])),
    st.sampled_from(["active", "pending", "failed", "", "ACTIVE"]),
    st.fixed_dictionaries({"gtt_delivery": st.sampled_from([True, False, None, "true", 1])}),
    st.booleans(),
    st.booleans(),
)
@example(
    mode="confirm", status="active", capabilities={"gtt_delivery": True}, ddpi=False, weights=True
)
@example(
    mode="auto", status="active", capabilities={"gtt_delivery": True}, ddpi=True, weights=False
)
def test_confirm_and_auto_need_gtt(
    mode: str, status: str, capabilities: dict, ddpi: bool, weights: bool
) -> None:
    """A bot never runs confirm or auto unless its
    account is active and reports `gtt_delivery = true`, the owner has confirmed DDPI on it and
    the policy's base weights, and such a request is refused with the first gate that fails; a
    mode outside the three runs as shadow and is refused as unknown, whatever the account."""
    gates = Gates(status, capabilities, ddpi_confirmed=ddpi, weights_confirmed=weights)
    effective = capability_gated(mode, gates)
    gtt = status == "active" and capabilities["gtt_delivery"] is True
    known = mode in MODE_VALUES
    ready = gtt and ddpi and weights
    assert effective == (mode if known and (mode == "shadow" or ready) else "shadow")
    if not known:
        with pytest.raises(UnknownModeError):
            request_mode(mode, gates)
    elif mode != "shadow" and not ready:
        with pytest.raises(ModeRefusedError) as refused:
            request_mode(mode, gates)
        first = (
            "no_gtt_delivery"
            if not gtt
            else "ddpi_not_confirmed"
            if not ddpi
            else "weights_not_confirmed"
        )
        assert refused.value.code == first
    else:
        assert request_mode(mode, gates) == mode


@given(status=st.sampled_from(["active", "pending", "failed", "suspended"]))
def test_the_fyers_adapter_unlocks_modes_on_an_active_account(status: str) -> None:
    """With the adapter reports GTT for delivery (verified live), so
    with its capabilities and the owner's two confirmations every mode runs as asked on an
    active account, and only shadow on any other; without GTT for delivery only shadow runs
    anywhere."""
    capabilities = FyersHatBroker(FakeBroker()).capabilities().to_json()
    assert capabilities["gtt_delivery"] is True
    for mode in Mode:
        expected = mode if status == "active" else "shadow"
        gates = Gates(status, capabilities, ddpi_confirmed=True, weights_confirmed=True)
        assert capability_gated(mode, gates) == expected
        off = Gates(
            status,
            {**capabilities, "gtt_delivery": False},
            ddpi_confirmed=True,
            weights_confirmed=True,
        )
        assert capability_gated(mode, off) == "shadow"


reads = st.sampled_from(["funds", "positions", "holdings"])


@given(st.booleans(), st.booleans(), st.booleans())
def test_session_status_mapping(connected: bool, has_client: bool, store_error: bool) -> None:
    """The mapping is total and only a readable store, a saved token and a usable
    client give LIVE."""
    status = map_session(connected=connected, has_client=has_client, store_error=store_error)
    assert status in set(SessionStatus)
    assert (status is SessionStatus.LIVE) == (connected and has_client and not store_error)
    if store_error:
        assert status is SessionStatus.UNKNOWN


class _Auth:
    def __init__(self, connected: bool | BaseException) -> None:
        self.connected = connected

    def status(self) -> object:
        if isinstance(self.connected, BaseException):
            raise self.connected
        return type("S", (), {"connected": self.connected})()


@given(st.one_of(st.booleans(), st.just(RuntimeError("store unreadable"))), st.booleans())
def test_adapter_session_status_never_raises(
    connected: bool | BaseException, has_model: bool
) -> None:
    broker = FyersHatBroker(
        FakeBroker(),  # type: ignore[arg-type]
        client=lambda: type("C", (), {"fyers": object() if has_model else None})(),  # type: ignore[arg-type,return-value]
        auth=lambda: _Auth(connected),  # type: ignore[arg-type,return-value]
    )
    status = broker.session_status()
    assert (status is SessionStatus.LIVE) == (connected is True and has_model)


REFUSED = [  # the -16 replies Fyers sent for a bad token, recorded 2026-10-22
    LIVE_REPLIES["funds_invalid_token"],
    LIVE_REPLIES["profile_invalid_token"],
    LIVE_REPLIES["orderbook_invalid_token"],
]
OUTAGES = [
    {"s": "error", "code": -1, "message": "Something went wrong"},
    {"s": "error", "code": 500, "message": "Internal server error"},
    RuntimeError("connection reset"),
]


@given(
    reply=st.sampled_from([*REFUSED, *OUTAGES]),
    read=st.sampled_from(["funds", "positions", "holdings", "orders", "trades", "gtts"]),
    wait=st.sampled_from([0.0, 29.9, 30.0, 300.0]),
)
def test_token_refused_is_session_expired(reply: object, read: str, wait: float) -> None:
    """Whenever Fyers answers code -16 (a bad, expired or revoked
    token), the session reads EXPIRED though the token file is still in date, and a read fails
    with `broker_session_expired`, so the outage watch alerts and asks for a login; any other
    failure is an outage: the read fails with `broker_unavailable` and the session stays LIVE.
    The profile is read at most once per token per 30 s window."""
    sdk, now, model = RecordingSdk(reply), [0.0], [object()]  # a new token builds a new model
    adapter = FyersHatBroker(
        broker_over(sdk),
        client=lambda: type("C", (), {"fyers": model[0]})(),  # type: ignore[arg-type,return-value]
        auth=lambda: _Auth(True),  # type: ignore[arg-type,return-value]
        instruments=lambda: [],
        clock=lambda: now[0],
    )
    refused = reply in REFUSED
    expected = SessionStatus.EXPIRED if refused else SessionStatus.LIVE
    profiles = lambda: sum(name == "get_profile" for name, _ in sdk.calls)  # noqa: E731
    assert adapter.session_status() is expected
    now[0] += wait
    assert adapter.session_status() is expected
    # one profile read per token per check window: none more within it, one more after it or
    # for a new token
    assert profiles() == (2 if wait >= TOKEN_CHECK_SECONDS else 1)
    model[0] = object()
    adapter.session_status()
    assert profiles() == (3 if wait >= TOKEN_CHECK_SECONDS else 2)
    fetch = getattr(adapter, read)
    with pytest.raises(BrokerUnavailableError) as caught:
        fetch()
    assert caught.value.code == (CODE_SESSION_EXPIRED if refused else CODE_UNAVAILABLE)


# Fyers' reply once the plan's daily requests are spent: the code and message the owner saw on
# 2026-10-22 (the history endpoint), in Fyers' error shape.
RATE_LIMITED = {"s": "error", "code": -360, "message": "Standard plan limit of 5,000 requests/day"}
WRITES = {
    "place_order": lambda b: b.place_order(
        OrderIntent(
            "SBIN",
            Side.BUY,
            3,
            OrderType.LIMIT,
            encode_tag(uuid.UUID(int=7), "entry", 0),
            price=Decimal("100"),
        )
    ),
    "modify_order": lambda b: b.modify_order(
        "26102200316678", order_type=OrderType.LIMIT, qty=3, price=Decimal("101")
    ),
    "cancel_order": lambda b: b.cancel_order("26102200316678"),
    "place_gtt_stop": lambda b: b.place_gtt_stop(
        GttRequest(
            "SBIN",
            3,
            Decimal("95"),
            Decimal("94.5"),
            tag=encode_tag(uuid.UUID(int=7), "stop_gtt", 0),
        )
    ),
    "modify_gtt": lambda b: b.modify_gtt(
        GttModify("26102200000001", 3, Decimal("96"), Decimal("95.5"))
    ),
    "cancel_gtt": lambda b: b.cancel_gtt("26102200000001"),
}


@given(
    read=st.sampled_from(["funds", "positions", "holdings", "orders", "trades", "gtts"]),
    write=st.sampled_from(sorted(WRITES)),
)
def test_rate_limit_is_its_own_code(read: str, write: str) -> None:
    """Fyers' -360 (the plan's daily requests spent) is neither an outage nor a lost
    session: a read fails closed with `broker_rate_limited`, a write is a definite refusal with
    `broker_rate_limited` (nothing was placed), the session stays LIVE, and no broker text is
    carried."""
    sdk = RecordingSdk(RATE_LIMITED)
    adapter = FyersHatBroker(
        broker_over(sdk),
        client=lambda: type("C", (), {"fyers": object()})(),  # type: ignore[arg-type,return-value]
        auth=lambda: _Auth(True),  # type: ignore[arg-type,return-value]
        instruments=lambda: list(SYMBOLS.items()),
    )
    assert adapter.session_status() is SessionStatus.LIVE
    read_book = getattr(adapter, read)
    with pytest.raises(BrokerUnavailableError) as caught:
        read_book()
    assert caught.value.code == CODE_RATE_LIMITED
    result = WRITES[write](adapter)
    assert getattr(result, "status", "rejected") == "rejected"
    assert getattr(result, "ok", False) is False
    assert result.code == CODE_RATE_LIMITED
    assert "5,000" not in repr(result) + repr(caught.value)
    assert adapter.session_status() is SessionStatus.LIVE


@given(read=st.sampled_from(["funds", "positions", "holdings", "orders", "trades", "gtts"]))
def test_token_refused_on_a_read_ends_the_check(read: str) -> None:
    """A -16 on a read inside the 30 s window of a passed profile check is seen by the next
    `session_status`: EXPIRED at once, not when the window ends."""
    sdk = RecordingSdk(LIVE_REPLIES["profile_ok"])
    adapter = FyersHatBroker(
        broker_over(sdk),
        client=lambda: type("C", (), {"fyers": model})(),  # type: ignore[arg-type,return-value]
        auth=lambda: _Auth(True),  # type: ignore[arg-type,return-value]
        instruments=lambda: [],
        clock=lambda: 0.0,
    )
    model = object()
    assert adapter.session_status() is SessionStatus.LIVE
    sdk.reply = LIVE_REPLIES["funds_invalid_token"]  # revoked at Fyers inside the window
    fetch = getattr(adapter, read)
    with pytest.raises(BrokerUnavailableError):
        fetch()
    assert adapter.session_status() is SessionStatus.EXPIRED


@given(st.lists(st.tuples(st.integers(0, 2), st.sampled_from(["acquire", "release"])), max_size=30))
def test_one_session_owner_per_account(steps: list[tuple[int, str]]) -> None:
    """At most one owner holds a given account's session at any time."""
    accounts = [uuid.uuid4() for _ in range(3)]
    registry = SessionRegistry()
    held: dict[int, object] = {}
    for index, action in steps:
        if action == "acquire":
            if index in held:
                with pytest.raises(SessionOwnedError):
                    registry.acquire(accounts[index])
            else:
                held[index] = registry.acquire(accounts[index])
        elif index in held:
            held.pop(index).release()  # type: ignore[attr-defined]
    for index in range(3):  # a released account can be acquired again, exactly once
        if index not in held:
            registry.acquire(accounts[index])
            with pytest.raises(SessionOwnedError):
                registry.acquire(accounts[index])


def test_an_owner_serialises_its_broker_calls() -> None:
    import threading

    registry = SessionRegistry()
    owner = registry.acquire(uuid.uuid4())
    inside = 0
    overlap = []

    def work() -> int:
        nonlocal inside
        inside += 1
        overlap.append(inside)
        threading.Event().wait(0.002)
        inside -= 1
        return 1

    threads = [threading.Thread(target=lambda: owner.call(work)) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(overlap) == 1


@given(st.sampled_from(["orders", "trades", "positions", "holdings", "funds"]))
def test_reads_raise_when_the_sdk_replies_with_an_error(read: str) -> None:
    from hat.fakes.fyers_sdk import RecordingSdk

    sdk = RecordingSdk(reply={"s": "error", "code": -1, "message": "boom"})
    method = getattr(adapter_over(sdk), read)
    with pytest.raises(BrokerUnavailableError):
        method()


@pytest.mark.parametrize(
    ("previous", "error", "status"),
    [
        ("pending", None, "active"),
        ("failed", None, "active"),
        ("pending", BrokerUnavailableError(), "failed"),  # a first check that fails
        ("active", BrokerUnavailableError(), "active"),  # an outage keeps it
        ("active", BrokerUnavailableError(CODE_RATE_LIMITED), "active"),
        ("active", BrokerUnavailableError(CODE_SESSION_EXPIRED), "active"),
        ("failed", BrokerUnavailableError(CODE_UNAVAILABLE), "failed"),
        ("active", BrokerUnavailableError(CODE_INVALID_RESPONSE), "failed"),  # definite
    ],
)
def test_onboarding_status_keeps_the_status_through_an_outage(
    previous: str, error: BrokerUnavailableError | None, status: str
) -> None:
    assert onboarding_status(previous, error) == status


@pytest.mark.parametrize(
    ("qty_open", "held", "row_qty", "verdict"),
    [
        (100, 100, 100, NEVER_SOLD),
        (100, 120, 40, NEVER_SOLD),
        (100, 0, 100, SOLD),
        (100, 60, 40, SOLD),
        (100, 0, 40, SOLD),  # more gone than the sales: sold, capped at what is held
        (100, 70, 40, UNKNOWN),  # part of the sale gone
    ],
)
def test_earlier_sale_verdict(qty_open: int, held: int, row_qty: int, verdict: str) -> None:
    assert earlier_sale_verdict(qty_open, held, row_qty) == verdict


FLOOR = Decimal("240.15")
SEND, HOLD = SaleStep.SEND, SaleGate(SaleStep.HOLD)


@pytest.mark.parametrize(
    ("settled", "qty", "busy", "open_now", "rejected", "floor", "gate"),
    [
        (SETTLED, 10, False, True, 0, None, SaleGate(SEND, attempts=2)),
        (SETTLED, 10, False, True, 0, FLOOR, SaleGate(SEND, attempts=2, floor=FLOOR)),
        (SETTLED, 10, False, True, 1, None, SaleGate(SEND, attempts=1)),
        (SETTLED, 10, False, True, 2, FLOOR, SaleGate(SEND, attempts=0, floor=FLOOR)),
        (SETTLED, 10, False, True, 2, None, HOLD),  # refused twice, told then, no band
        (SETTLED, 10, False, True, 3, FLOOR, HOLD),  # the band limit was refused too
        (UNREADABLE, 10, False, True, 0, None, SaleGate(SaleStep.REFUSE, "sale_unresolved")),
        (UNREADABLE, 0, True, False, 3, None, SaleGate(SaleStep.REFUSE, "sale_unresolved")),
        (WAIT, 10, False, True, 0, None, HOLD),  # a lost sale may still land
        (SETTLED, 0, False, True, 0, None, HOLD),  # a settled sale took the rest
        (SETTLED, 10, True, True, 0, None, HOLD),  # one sale at a time
        (SETTLED, 10, False, False, 0, None, SaleGate(SaleStep.REFUSE, "market_closed")),
        (SETTLED, 10, False, False, 3, None, SaleGate(SaleStep.REFUSE, "market_closed")),
    ],
)
def test_sale_gate(
    settled: str,
    qty: int,
    busy: bool,
    open_now: bool,
    rejected: int,
    floor: Decimal | None,
    gate: SaleGate,
) -> None:
    got = sale_gate(settled, qty, busy=busy, open_now=open_now, rejected=rejected, floor=floor)
    assert got == gate


READ_AT = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "gtt_id", "placed_ago", "book", "confirmed"),
    [
        ("placed", "g1", timedelta(hours=1), {"g1", "g2"}, True),  # present: rests
        ("partially_filled", "g1", timedelta(days=3), {"g1"}, True),
        ("placed", "g1", timedelta(hours=1), {"g2"}, False),  # absent after a good read: gone
        ("placed", "g1", timedelta(hours=1), set(), False),
        ("placed", "g1", timedelta(hours=1), None, None),  # unreadable book: nothing known
        ("placed", "g1", timedelta(seconds=30), set(), None),  # too young for the book
        ("placed", "g1", -timedelta(seconds=5), set(), None),  # placed after the read
        ("placed", "g1", None, set(), None),  # never stamped
        ("unconfirmed", None, timedelta(hours=1), set(), None),  # lost answer: settle_lost's
        ("cancelled", "g1", timedelta(hours=1), set(), None),  # already ended
    ],
)
def test_stop_confirmed(
    status: str,
    gtt_id: str | None,
    placed_ago: timedelta | None,
    book: set[str] | None,
    confirmed: bool | None,
) -> None:
    row = HatOrder(
        status=status,
        broker_order_id=gtt_id,
        placed_at=None if placed_ago is None else READ_AT - placed_ago,
    )
    assert stop_confirmed(row, book, READ_AT) is confirmed


@pytest.mark.parametrize(
    ("placed_ago", "found", "sale", "verdict"),
    [
        (timedelta(minutes=5), True, False, Verdict.ADOPT),  # held: adopted
        (timedelta(seconds=10), True, True, Verdict.ADOPT),  # held, even when young
        (timedelta(days=1), True, True, Verdict.ADOPT),  # held, of an earlier session
        (timedelta(seconds=10), False, False, Verdict.WAIT),  # young: may still land
        (timedelta(seconds=59), False, True, Verdict.WAIT),
        (LOST_ANSWER_SETTLE, False, False, Verdict.EXPIRE),  # absent, old enough: never landed
        (timedelta(minutes=5), False, True, Verdict.EXPIRE),
        (timedelta(days=1), False, True, Verdict.STALE),  # a sale of an earlier session
        (timedelta(days=1), False, False, Verdict.EXPIRE),  # a stop or entry of one
        (None, False, True, Verdict.EXPIRE),  # never sent
    ],
)
def test_settle_verdict(
    placed_ago: timedelta | None, found: bool, sale: bool, verdict: Verdict
) -> None:
    placed_at = None if placed_ago is None else READ_AT - placed_ago
    assert settle_verdict(placed_at, found, READ_AT, LOST_ANSWER_SETTLE, sale=sale) is verdict


@pytest.mark.parametrize("sale", [False, True])
def test_settle_verdict_waits_on_shares_sold(sale: bool) -> None:
    """A stop or sale found with shares sold waits for the fill sync to adopt it with its fills,
    however old it is."""
    placed_at = READ_AT - timedelta(minutes=5)
    found = settle_verdict(placed_at, True, READ_AT, LOST_ANSWER_SETTLE, filled_qty=40, sale=sale)
    assert found is Verdict.WAIT
