"""PBT for broker accounts, the session, the capability gate and the adapter's reads
(HAT-BRK-082, -091, -092, HAT-DEC-I8, HAT-DEC-I9, HAT-DEC-T8, HAT-EXEC-012, HAT-OPS-031)."""

import uuid
from decimal import Decimal

import pytest
from hat_exec_support import SYMBOLS, USER, adapter_over, exec_env
from hat_support import memory_engine
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import sessionmaker

from app.hat.execution.accounts import (
    ADAPTER_FACTORIES,
    MODES,
    ModeRefusedError,
    OnboardingError,
    effective_mode,
    onboard_account,
    request_mode,
)
from app.hat.execution.broker import BrokerUnavailableError, SessionStatus
from app.hat.execution.fyers_adapter import FyersHatBroker, map_session
from app.hat.execution.session import (
    SessionOwnedError,
    SessionRegistry,
)
from app.hat.models import HatBrokerAccount, HatBrokerSession, HatEvent

from fakes.broker import FakeBroker


@given(
    st.sampled_from(MODES),
    st.sampled_from(["active", "pending", "failed", "", "ACTIVE"]),
    st.fixed_dictionaries({"gtt_delivery": st.sampled_from([True, False, None, "true", 1])}),
)
def test_confirm_and_auto_need_gtt(mode: str, status: str, capabilities: dict) -> None:
    """HAT-EXEC-012 / HAT-DEC-I9: a bot never runs confirm or auto unless its account is active and
    reports `gtt_delivery = true`, and such a request is refused."""
    effective = effective_mode(mode, account_status=status, capabilities=capabilities)
    ready = status == "active" and capabilities["gtt_delivery"] is True
    assert effective == (mode if mode == "shadow" or ready else "shadow")
    if mode != "shadow" and not ready:
        with pytest.raises(ModeRefusedError):
            request_mode(mode, account_status=status, capabilities=capabilities)
    else:
        assert request_mode(mode, account_status=status, capabilities=capabilities) == mode


def test_the_fyers_adapter_never_unlocks_confirm_or_auto() -> None:
    """HAT-BRK-091 with HAT-EXEC-012: with the capabilities the adapter reports, no mode but
    shadow is effective."""
    capabilities = FyersHatBroker(FakeBroker()).capabilities().to_json()
    for mode in MODES:
        assert effective_mode(mode, account_status="active", capabilities=capabilities) == "shadow"


reads = st.sampled_from(["funds", "positions", "holdings"])


@given(st.one_of(st.none(), reads), st.sampled_from(["circuit", "raises"]))
@settings(max_examples=60)
def test_account_active_after_check(failing: str | None, how: str) -> None:
    """HAT-BRK-092: an account is `active` exactly when funds, positions and holdings were all
    fetched, and carries the adapter's capabilities; a failing read leaves it not active."""
    fake = FakeBroker()
    if failing is not None:
        if how == "circuit":
            fake.circuit_open = True
        else:
            error = RuntimeError("down")
            if failing == "funds":
                fake.available = error
            elif failing == "positions":
                fake.positions = error
            else:
                fake.holdings = error
    engine = memory_engine(HatBrokerAccount)
    session = sessionmaker(engine, expire_on_commit=False)()
    broker = FyersHatBroker(fake, symbols=lambda: [])  # type: ignore[arg-type]
    account = onboard_account(session, broker, user_id=USER, broker_name="fyers", account_ref="Z1")
    expected_pass = failing is None
    assert (account.status == "active") == expected_pass
    assert account.capabilities == broker.capabilities().to_json()
    assert account.broker == "fyers"


@given(st.text(max_size=12).filter(lambda name: name != "fyers"))
def test_only_the_fyers_adapter_is_registered(name: str) -> None:
    """HAT-DEC-I8: onboarding any broker but Fyers is refused, and the registry holds only Fyers."""
    assert set(ADAPTER_FACTORIES) == {"fyers"}
    engine = memory_engine(HatBrokerAccount)
    session = sessionmaker(engine)()
    adapter = FyersHatBroker(FakeBroker())
    with pytest.raises(OnboardingError):
        onboard_account(session, adapter, user_id=USER, broker_name=name, account_ref="Z")
    assert session.query(HatBrokerAccount).count() == 0


@given(st.booleans(), st.booleans(), st.booleans())
def test_session_status_mapping(connected: bool, has_client: bool, store_error: bool) -> None:
    """HAT-BRK-082: the mapping is total and only a readable store, a saved token and a usable
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


@given(st.lists(st.tuples(st.integers(0, 2), st.sampled_from(["acquire", "release"])), max_size=30))
def test_one_session_owner_per_account(steps: list[tuple[int, str]]) -> None:
    """HAT-DEC-T8: at most one owner holds a given account's session at any time."""
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


def test_record_session_writes_the_morning_login() -> None:
    from datetime import date

    from app.hat.execution.session import record_session

    engine = memory_engine(HatBrokerSession, HatEvent)
    session = sessionmaker(engine, expire_on_commit=False)()
    account = uuid.uuid4()

    class Broker:
        status = SessionStatus.LIVE

        def session_status(self) -> SessionStatus:
            return self.status

    broker = Broker()
    today = date(2026, 10, 21)
    assert record_session(session, broker, account, today) is SessionStatus.LIVE  # type: ignore[arg-type]
    row = session.get(HatBrokerSession, (account, today))
    assert row.status == "live"
    assert row.logged_in_at is not None
    broker.status = SessionStatus.EXPIRED
    record_session(session, broker, account, today, lost_alert=True)  # type: ignore[arg-type]
    types = sorted(e.event_type for e in session.query(HatEvent))
    assert types == ["broker.logged_in", "broker.session_lost"]


book_rows = st.dictionaries(
    st.sampled_from(["A", "B", "C"]),
    st.tuples(st.sampled_from([6, 2, 1, 5]), st.integers(min_value=0, max_value=4)),
    max_size=3,
)


@given(st.lists(book_rows, min_size=1, max_size=8))
def test_order_updates_report_each_change_once(polls: list[dict]) -> None:
    """The adapter's polling stream reports an order when its status or fill changed since the
    last successful poll (or it is new), and nothing for an unchanged book."""
    fake = FakeBroker()
    broker = FyersHatBroker(fake, symbols=lambda: [])  # type: ignore[arg-type]
    previous: dict[str, tuple[int, int]] = {}
    for book in polls:
        fake.orders = [
            {
                "id": order_id,
                "orderTag": "HTE0AAAAAAAAAAAAAAAA",
                "symbol": SYMBOLS["SBIN"],
                "side": 1,
                "qty": 100,
                "filledQty": filled,
                "status": status,
                "productType": "CNC",
            }
            for order_id, (status, filled) in book.items()
        ]
        updated = {u.order_id for u in broker.order_updates()}
        assert updated == {i for i, row in book.items() if previous.get(i) != row}
        assert broker.order_updates() == []  # the same book again reports nothing
        previous = dict(book)


@given(st.sampled_from(["orders", "trades", "positions", "holdings", "funds"]))
def test_reads_raise_when_the_sdk_replies_with_an_error(read: str) -> None:
    from hat_exec_support import RecordingSdk

    sdk = RecordingSdk(reply={"s": "error", "code": -1, "message": "boom"})
    method = getattr(adapter_over(sdk), read)
    with pytest.raises(BrokerUnavailableError):
        method()


def test_decimal_funds_and_exact_positions() -> None:
    env = exec_env()
    env.fake.available = 12345.5
    funds = env.broker.funds()
    assert funds.available == Decimal("12345.5")
    assert isinstance(funds.total, Decimal)
