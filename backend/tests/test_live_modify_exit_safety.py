"""LIVE modify, cancel and exit safety: risk checks before every broker call that changes
exposure, exits that can only reduce a position, exit idempotency, and no broker or
exception text in client responses (trading rules 3, 5, 7 and 10)."""

from collections.abc import Generator
from datetime import datetime
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.brokers.base import BrokerUnavailableError
from app.brokers.plugins.fyers import FyersBroker
from app.models.live_order import LiveOrder
from app.models.live_position import LivePosition
from app.routers import trading
from app.services import live_exit_service as les
from app.services import order_execution_service as oes
from app.services.risk_manager import RiskManager, RiskStatus

from fakes.broker import FakeBroker
from fakes.broker import broker_position as position
from fakes.market import FreezeMarket

SECRET = "token=abc123 internal broker detail"
CLIENT_ORDER_ID = "5b0e4c2a-7d1f-4e3b-9a6c-2f8d1e0b7c45"
OTHER_CLIENT_ORDER_ID = "8c3d1f6e-2a4b-4c7d-9e0f-1a2b3c4d5e6f"


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()


@pytest.fixture
def market_open(freeze_market: FreezeMarket) -> None:
    freeze_market(True)


@pytest.fixture
def broker(fake_broker: FakeBroker) -> FakeBroker:
    """One long SBIN INTRADAY position of 10; everything else is the FakeBroker default."""
    fake_broker.positions = [position("NSE:SBIN-EQ", 10)]
    return fake_broker


@pytest.fixture
def service(broker: FakeBroker) -> oes.OrderExecutionService:
    svc = oes.OrderExecutionService()
    svc._broker = broker
    return svc


@pytest.fixture
def exits(service: oes.OrderExecutionService) -> les.LiveExitService:
    return les.LiveExitService(service)


def add_live_order(
    db: Session, *, status: str = "SUBMITTED", quantity: int = 5, order_type: str = "LIMIT"
) -> LiveOrder:
    order = LiveOrder(
        id="FY-1",
        internal_id="ORD-FY1",
        user_id="u1",
        symbol="SBIN",
        fyers_symbol="NSE:SBIN-EQ",
        side="BUY",
        quantity=quantity,
        order_type=order_type,
        product_type="INTRADAY",
        price=800.0,
        trigger_price=0.0,
        status=status,
        instrument_type="EQ",
        is_paper=0,
    )
    db.add(order)
    db.commit()
    return order


def add_realized_loss(db: Session, realized: float) -> None:
    db.add(
        LivePosition(
            id="default_user-NSE:INFY-EQ-INTRADAY",
            symbol="INFY",
            fyers_symbol="NSE:INFY-EQ",
            product_type="INTRADAY",
            side="LONG",
            net_qty=0,
            realized_pl=realized,
            last_synced_at=datetime.now(),
        )
    )
    db.commit()


# --- LIVE modify: every exposure-changing modify passes the risk checks ---------


@pytest.mark.parametrize(
    ("setup", "params", "code"),
    [
        ("closed", {"price": 801.0}, "MARKET_CLOSED"),
        ("none", {"quantity": 1001}, "POSITION_SIZE_LIMIT"),
        ("none", {"price": 1000.0}, "PRICE_OUT_OF_BAND"),
        ("none", {"order_type": "SL", "trigger_price": 500.0}, "PRICE_OUT_OF_BAND"),
        ("no_quote", {"price": 801.0}, "PRICE_UNAVAILABLE"),
        ("quote_raises", {"price": 801.0}, "PRICE_UNAVAILABLE"),
        ("loss_limit", {"quantity": 6}, "DAILY_LOSS_LIMIT"),
        ("none", {"quantity": 700}, "EXPOSURE_LIMIT"),
        ("no_margin", {"quantity": 6}, "INSUFFICIENT_MARGIN"),
    ],
)
def test_live_modify_blocked_by_each_risk_check_never_reaches_broker(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    params: dict[str, Any],
    code: str,
    freeze_market: FreezeMarket,
) -> None:
    freeze_market(setup != "closed")
    if setup == "no_quote":
        broker.ltp = 0.0
    if setup == "quote_raises":
        broker.ltp = RuntimeError(SECRET)
    if setup == "loss_limit":
        add_realized_loss(orders, -6000.0)
    if setup == "no_margin":
        broker.available = 100.0
    order = add_live_order(orders)

    result = service.modify_order("FY-1", params, orders)

    assert (result["status"], result["code"]) == ("ERROR", code)
    assert result["message"].startswith("Risk Check Failed: ")
    assert SECRET not in repr(result)
    assert broker.calls_named("modify_order") == []
    orders.refresh(order)
    assert (order.quantity, order.price, order.order_type) == (5, 800.0, "LIMIT")


@pytest.mark.parametrize(
    "status", ["PENDING_SUBMIT", "SUBMIT_UNCONFIRMED", "REJECTED", "ERROR", "CANCELLED"]
)
def test_live_modify_of_an_order_with_no_working_broker_order_is_rejected(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker, status: str
) -> None:
    add_live_order(orders, status=status)

    result = service.modify_order("FY-1", {"price": 801.0}, orders)

    assert result == {"status": "ERROR", "message": f"Cannot modify order in {status} state"}
    assert broker.calls == []


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"quantity": 0}, "Quantity must be a positive integer"),
        ({"price": -1}, "Price must be a positive number"),
        ({"order_type": "STOP"}, "Invalid order type. Expected MARKET, LIMIT, SL or SL-M."),
    ],
)
def test_live_modify_with_invalid_values_never_reaches_broker(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    params: dict[str, Any],
    message: str,
) -> None:
    add_live_order(orders)

    result = service.modify_order("FY-1", params, orders)

    assert result == {"status": "ERROR", "message": message}
    assert broker.calls == []


def add_open_exposure(db: Session, value: float) -> None:
    """An open position worth `value` at its LTP of 100."""
    db.add(
        LivePosition(
            id="default_user-NSE:ITC-EQ-INTRADAY",
            symbol="ITC",
            fyers_symbol="NSE:ITC-EQ",
            product_type="INTRADAY",
            side="LONG",
            net_qty=int(value / 100),
            ltp=100.0,
            last_synced_at=datetime.now(),
        )
    )
    db.commit()


@pytest.mark.usefixtures("market_open")
@pytest.mark.parametrize(
    ("current_type", "params", "setup", "code"),
    [
        # Same quantity, limit 800 -> 900: notional +500 takes 499,900 past 500,000.
        ("LIMIT", {"price": 900.0}, "near_exposure_limit", "EXPOSURE_LIMIT"),
        # Same quantity, notional +50 needs 10 of intraday margin; 5 is available.
        ("LIMIT", {"price": 810.0}, "low_margin", "INSUFFICIENT_MARGIN"),
        # LIMIT 800 -> MARKET at LTP 820: notional +100.
        ("LIMIT", {"order_type": "MARKET"}, "low_margin", "INSUFFICIENT_MARGIN"),
        # MARKET at LTP 820 -> LIMIT 830: notional +50.
        ("MARKET", {"order_type": "LIMIT", "price": 830.0}, "low_margin", "INSUFFICIENT_MARGIN"),
        # A MARKET order cannot be valued without a live price.
        ("MARKET", {"order_type": "LIMIT", "price": 801.0}, "no_quote", "PRICE_ESTIMATE_FAILED"),
    ],
)
def test_live_modify_that_raises_notional_at_same_quantity_is_risk_checked(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    current_type: str,
    params: dict[str, Any],
    setup: str,
    code: str,
) -> None:
    broker.ltp = 820.0
    if setup == "near_exposure_limit":
        add_open_exposure(orders, 499_980.0)
    if setup == "low_margin":
        broker.available = 5.0
    if setup == "no_quote":
        broker.ltp = 0.0
    order = add_live_order(orders, order_type=current_type)

    result = service.modify_order("FY-1", params, orders)

    assert (result["status"], result["code"]) == ("ERROR", code)
    assert broker.calls_named("modify_order") == []
    orders.refresh(order)
    assert (order.quantity, order.price, order.order_type) == (5, 800.0, current_type)


@pytest.mark.usefixtures("market_open")
def test_live_modify_that_lowers_the_price_skips_margin_and_exposure(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    broker.available = 0.0  # a margin check would fail
    add_open_exposure(orders, 499_980.0)  # so would an exposure check on any increase
    order = add_live_order(orders)

    result = service.modify_order("FY-1", {"price": 795.0}, orders)

    assert result == {"status": "SUCCESS", "message": "Order Modified"}
    assert broker.calls_named("get_funds") == []
    orders.refresh(order)
    assert order.price == 795.0


@pytest.mark.usefixtures("market_open")
def test_live_modify_that_reduces_quantity_skips_margin_and_exposure(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    broker.available = 0.0  # a margin check would fail
    order = add_live_order(orders)

    result = service.modify_order("FY-1", {"quantity": 3}, orders)

    assert result == {"status": "SUCCESS", "message": "Order Modified"}
    assert broker.calls == [
        ("get_quote", "NSE:SBIN-EQ"),
        (
            "modify_order",
            ("FY-1", {"order_type": "LIMIT", "quantity": 3, "price": 800.0, "trigger_price": 0.0}),
        ),
    ]
    orders.refresh(order)
    assert (order.quantity, order.broker_message) == (3, "Modified: Modified")


@pytest.mark.usefixtures("market_open")
def test_live_modify_returns_risk_warnings_with_success(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    add_realized_loss(orders, -4500.0)  # past 80% of the 5000 daily loss limit
    add_live_order(orders)

    result = service.modify_order("FY-1", {"quantity": 6}, orders)

    assert result == {
        "status": "SUCCESS",
        "message": "Order Modified",
        "warnings": ["Approaching daily loss limit. Current P&L: ₹-4500.00"],
    }
    assert broker.count("modify_order") == 1


# --- LIVE modify and cancel never return broker or exception text ---------------


@pytest.mark.usefixtures("market_open")
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (RuntimeError(SECRET), "Broker order modify failed"),
        (
            {"s": "error", "message": "Order already\r\nexecuted\x00"},
            "Broker rejected the modify: Order already executed",
        ),
        ("not a dict", "Broker rejected the modify"),
    ],
)
def test_live_modify_failure_returns_fixed_message(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    caplog: pytest.LogCaptureFixture,
    reply: object,
    message: str,
) -> None:
    broker.modify_reply = reply  # type: ignore[assignment]
    add_live_order(orders)

    result = service.modify_order("FY-1", {"price": 801.0}, orders)

    assert result == {"status": "ERROR", "message": message}
    assert SECRET not in repr(result)
    if isinstance(reply, Exception):
        # The real error stays in the server log.
        assert any(r.exc_info and SECRET in str(r.exc_info[1]) for r in caplog.records)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (RuntimeError(SECRET), "Broker order cancel failed"),
        ("not a dict", "Broker order cancel failed"),
        ({"s": "error", "message": "x" * 300}, "Broker rejected the cancel: " + "x" * 120),
    ],
)
def test_live_cancel_failure_returns_fixed_message(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    reply: object,
    message: str,
) -> None:
    broker.cancel_reply = reply  # type: ignore[assignment]
    order = add_live_order(orders)

    result = service.cancel_order("FY-1", orders)

    assert result == {"status": "ERROR", "message": message}
    assert SECRET not in repr(result)
    orders.refresh(order)
    assert order.status == "SUBMITTED"


UNCONFIRMED_MESSAGE = (
    "Order sent but not confirmed by the broker. Check the broker orderbook before "
    "placing it again."
)
ORDER_TAG = oes.fyers_order_tag(CLIENT_ORDER_ID)


def live_market_params() -> dict[str, Any]:
    return {
        "symbol": "SBIN",
        "side": "BUY",
        "quantity": 1,
        "type": "MARKET",
        "product": "INTRADAY",
        "client_order_id": CLIENT_ORDER_ID,
    }


@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError(SECRET),
        {"order_id": "", "status": "ERROR", "message": SECRET, "details": None},
        {"order_id": "", "status": "SUBMIT_UNCONFIRMED", "message": SECRET, "details": None},
        {"order_id": "", "status": "SUBMITTED", "message": SECRET, "details": None},
        "not a dict",
    ],
)
@pytest.mark.parametrize("lookup", [None, BrokerUnavailableError(SECRET)])
def test_ambiguous_live_submit_is_unconfirmed_never_rejected(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    reply: object,
    lookup: object,
) -> None:
    broker.place_reply = reply  # type: ignore[assignment]
    broker.tagged_order = lookup  # type: ignore[assignment]
    monkeypatch.setattr(service, "_run_live_risk_checks", lambda params, db: (None, []))

    result = service.place_order(live_market_params(), orders, mode="LIVE")

    row = orders.query(LiveOrder).one()
    assert result == {
        "status": "SUBMIT_UNCONFIRMED",
        "order_id": row.internal_id,
        "message": UNCONFIRMED_MESSAGE,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }
    assert (row.id, row.status, row.reject_reason) == (
        row.internal_id,
        "SUBMIT_UNCONFIRMED",
        None,
    )
    # One orderbook lookup by the order's tag before giving up.
    assert broker.calls_named("find_order_by_tag") == [ORDER_TAG]

    retry = service.place_order(live_market_params(), orders, mode="LIVE")

    assert (retry["status"], retry["duplicate"]) == ("SUBMIT_UNCONFIRMED", True)
    assert broker.count("place_order") == 1


@pytest.mark.parametrize("found_status", ["SUBMITTED", "FILLED", "REJECTED"])
def test_ambiguous_live_submit_found_in_the_orderbook_records_broker_id_and_status(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    found_status: str,
) -> None:
    broker.place_reply = RuntimeError(SECRET)
    broker.tagged_order = {"order_id": "B-77", "status": found_status}
    monkeypatch.setattr(service, "_run_live_risk_checks", lambda params, db: (None, []))

    result = service.place_order(live_market_params(), orders, mode="LIVE")

    assert result == {
        "status": found_status,
        "order_id": "B-77",
        "message": "Confirmed from the broker orderbook",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }
    row = orders.query(LiveOrder).one()
    assert (row.id, row.status) == ("B-77", found_status)


def test_live_place_rejection_drops_the_raw_broker_reply(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.place_reply = {
        "order_id": "",
        "status": "REJECTED",
        "message": "RMS: margin shortfall",
        "details": {"s": "error", "raw": SECRET},
    }
    monkeypatch.setattr(service, "_run_live_risk_checks", lambda params, db: (None, []))
    params = {
        "symbol": "SBIN",
        "side": "BUY",
        "quantity": 1,
        "type": "MARKET",
        "product": "INTRADAY",
        "client_order_id": CLIENT_ORDER_ID,
    }

    result = service.place_order(params, orders, mode="LIVE")

    assert result == {
        "status": "REJECTED",
        "message": "Broker rejected the order: RMS: margin shortfall",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
    }


# --- Exit checks: an exit only ever reduces a position --------------------------


@pytest.mark.parametrize(
    ("net_qty", "side", "quantity", "status", "code"),
    [
        (10, "SELL", 10, RiskStatus.PASS, "EXIT_OK"),
        (10, "SELL", 4, RiskStatus.PASS, "EXIT_OK"),
        (-10, "BUY", 10, RiskStatus.PASS, "EXIT_OK"),
        (10, "BUY", 10, RiskStatus.FAIL, "EXIT_WRONG_SIDE"),
        (-10, "SELL", 5, RiskStatus.FAIL, "EXIT_WRONG_SIDE"),
        (10, "SELL", 11, RiskStatus.FAIL, "EXIT_EXCEEDS_POSITION"),
        (10, "SELL", 0, RiskStatus.FAIL, "EXIT_EXCEEDS_POSITION"),
        (0, "SELL", 1, RiskStatus.FAIL, "NO_OPEN_POSITION"),
    ],
)
def test_exit_order_check_never_flips_or_adds(
    net_qty: int, side: str, quantity: int, status: RiskStatus, code: str
) -> None:
    result = RiskManager.exit_order_check(net_qty, side, quantity)

    assert (result.status, result.code) == (status, code)


def exit_payload(side: str, quantity: int, symbol: str = "NSE:SBIN-EQ") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "quantity": quantity,
        "side": side,
        "type": "MARKET",
        "product": "INTRADAY",
        "price": 0.0,
        "trigger_price": 0.0,
        "order_tag": oes.fyers_order_tag(
            les.exit_order_client_id(CLIENT_ORDER_ID, symbol, "INTRADAY")
        ),
    }


def run_exit(
    exits: les.LiveExitService,
    db: Session,
    symbol: str | None = "SBIN",
    client_order_id: object = CLIENT_ORDER_ID,
    product_type: str | None = None,
) -> dict[str, Any]:
    return exits.exit_positions(
        symbol=symbol,
        product_type=product_type,
        client_order_id=client_order_id,
        user_id="default_user",
        db=db,
    )


@pytest.mark.usefixtures("market_open")
@pytest.mark.parametrize(("net_qty", "side"), [(10, "SELL"), (-7, "BUY")])
def test_exit_places_an_opposite_market_order_for_the_open_quantity(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker, net_qty: int, side: str
) -> None:
    broker.positions = [position("NSE:SBIN-EQ", net_qty), position("NSE:INFY-EQ", 3)]

    result = run_exit(exits, orders)

    assert result == {
        "status": "SUCCESS",
        "mode": "LIVE",
        "symbol": "SBIN",
        "message": "Exit orders placed",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": False,
        "orders": [
            {
                "symbol": "NSE:SBIN-EQ",
                "product_type": "INTRADAY",
                "order_id": "B-1",
                "side": side,
                "quantity": abs(net_qty),
                "status": "SUBMITTED",
                "message": "Submitted",
            }
        ],
    }
    assert broker.calls_named("place_order") == [exit_payload(side, abs(net_qty))]
    row = orders.query(LiveOrder).one()
    assert (row.id, row.status, row.side, row.quantity, row.is_paper, row.source) == (
        "B-1",
        "SUBMITTED",
        side,
        abs(net_qty),
        0,
        "SQUARE_OFF",
    )
    assert row.client_order_id == les.exit_order_client_id(
        CLIENT_ORDER_ID, "NSE:SBIN-EQ", "INTRADAY"
    )


@pytest.mark.usefixtures("market_open")
def test_exit_subtracts_exit_quantity_already_working_at_the_broker(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    broker.working = [
        {"symbol": "NSE:SBIN-EQ", "side": "SELL", "quantity": 4, "product_type": "INTRADAY"},
        # A working BUY adds to the long; it does not count as closing it.
        {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 2, "product_type": "INTRADAY"},
        {"symbol": "NSE:SBIN-EQ", "side": "SELL", "quantity": 5, "product_type": "CNC"},
    ]

    result = run_exit(exits, orders)

    assert result["status"] == "SUCCESS"
    assert broker.calls_named("place_order") == [exit_payload("SELL", 6)]


@pytest.mark.usefixtures("market_open")
def test_exit_with_the_whole_position_already_working_places_nothing(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    broker.working = [
        {"symbol": "NSE:SBIN-EQ", "side": "SELL", "quantity": 10, "product_type": "INTRADAY"}
    ]

    result = run_exit(exits, orders)

    assert (result["status"], result["orders"]) == (
        "SUCCESS",
        [
            {
                "symbol": "NSE:SBIN-EQ",
                "product_type": "INTRADAY",
                "status": "SKIPPED",
                "message": "An exit order for this position is already working",
            }
        ],
    )
    assert broker.count("place_order") == 0
    assert orders.query(LiveOrder).count() == 0


@pytest.mark.parametrize("failing", ["positions", "working"])
@pytest.mark.usefixtures("market_open")
def test_exit_fails_closed_when_positions_or_working_orders_are_unknown(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker, failing: str
) -> None:
    setattr(broker, failing, BrokerUnavailableError(SECRET))

    result = run_exit(exits, orders)

    assert result == {
        "status": "ERROR",
        "mode": "LIVE",
        "symbol": "SBIN",
        "message": "Broker positions unavailable; no exit order was placed",
        "code": "BROKER_UNAVAILABLE",
    }
    assert broker.count("place_order") == 0
    assert orders.query(LiveOrder).count() == 0


def test_exit_with_market_closed_never_reaches_broker(
    orders: Session,
    exits: les.LiveExitService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
) -> None:
    freeze_market(False)

    result = run_exit(exits, orders)

    assert (result["status"], result["code"]) == ("ERROR", "MARKET_CLOSED")
    assert broker.calls == []


@pytest.mark.usefixtures("market_open")
@pytest.mark.parametrize(
    ("client_order_id", "code"),
    [(None, "CLIENT_ORDER_ID_REQUIRED"), ("not-a-uuid", "INVALID_CLIENT_ORDER_ID")],
)
def test_exit_without_a_valid_client_order_id_never_reaches_broker(
    orders: Session,
    exits: les.LiveExitService,
    broker: FakeBroker,
    client_order_id: str | None,
    code: str,
) -> None:
    result = run_exit(exits, orders, client_order_id=client_order_id)

    assert (result["status"], result["code"]) == ("ERROR", code)
    assert broker.calls == []


@pytest.mark.usefixtures("market_open")
def test_exit_with_an_invalid_symbol_never_reaches_broker(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    result = run_exit(exits, orders, symbol="bad sym!")

    assert (result["status"], result["code"], result["message"]) == (
        "ERROR",
        "INVALID_SYMBOL",
        "Invalid symbol",
    )
    assert broker.calls == []


@pytest.mark.usefixtures("market_open")
def test_exit_matches_an_option_position_by_its_db_symbol(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    broker.positions = [position("NSE:NIFTY26OCT25000CE", -75)]

    result = run_exit(exits, orders, symbol="NIFTY26OCT25000CE")

    assert result["status"] == "SUCCESS"
    assert broker.calls_named("place_order") == [
        exit_payload("BUY", 75, symbol="NSE:NIFTY26OCT25000CE")
    ]


@pytest.mark.usefixtures("market_open")
def test_exit_with_no_matching_position_places_nothing(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    result = run_exit(exits, orders, symbol="TCS")

    assert (result["status"], result["message"], result["orders"]) == (
        "SUCCESS",
        "No open position to exit",
        [],
    )
    assert broker.count("place_order") == 0


@pytest.mark.usefixtures("market_open")
def test_exit_all_exits_every_position_once_and_filters_by_product(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    broker.positions = [
        position("NSE:SBIN-EQ", 10),
        position("NSE:INFY-EQ", -3),
        position("NSE:TCS-EQ", 2, product="CNC"),
    ]

    result = run_exit(exits, orders, symbol=None, product_type="intraday")

    assert [(o["symbol"], o["side"], o["quantity"]) for o in result["orders"]] == [
        ("NSE:SBIN-EQ", "SELL", 10),
        ("NSE:INFY-EQ", "BUY", 3),
    ]
    rows = orders.query(LiveOrder).order_by(LiveOrder.id).all()
    assert len({row.client_order_id for row in rows}) == 2


# --- Exit idempotency ------------------------------------------------------------


@pytest.mark.usefixtures("market_open")
def test_exit_retry_with_the_same_id_returns_existing_orders_without_broker_calls(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    run_exit(exits, orders)
    calls_after_first = len(broker.calls)

    retry = run_exit(exits, orders)

    assert retry == {
        "status": "SUCCESS",
        "mode": "LIVE",
        "symbol": "SBIN",
        "message": "Duplicate client_order_id: returning the existing exit orders",
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
        "orders": [
            {
                "order_id": "B-1",
                "symbol": "NSE:SBIN-EQ",
                "product_type": "INTRADAY",
                "side": "SELL",
                "quantity": 10,
                "status": "SUBMITTED",
            }
        ],
    }
    assert len(broker.calls) == calls_after_first
    assert broker.count("place_order") == 1


@pytest.mark.usefixtures("market_open")
def test_exit_retry_is_deduplicated_even_after_the_market_closes(
    orders: Session,
    exits: les.LiveExitService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    freeze_market: FreezeMarket,
) -> None:
    run_exit(exits, orders)
    freeze_market(False)

    retry = run_exit(exits, orders)

    assert (retry["status"], retry["duplicate"]) == ("SUCCESS", True)
    assert broker.count("place_order") == 1


@pytest.mark.usefixtures("market_open")
def test_second_click_while_the_first_exit_works_does_not_flip_the_position(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    run_exit(exits, orders)

    second = run_exit(exits, orders, client_order_id=OTHER_CLIENT_ORDER_ID)

    assert [o["status"] for o in second["orders"]] == ["SKIPPED"]
    assert broker.count("place_order") == 1


@pytest.mark.usefixtures("market_open")
def test_exit_with_db_down_raises_and_never_calls_the_broker(
    orders: Session,
    exits: les.LiveExitService,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lost_commit() -> NoReturn:
        raise OperationalError("COMMIT", {}, Exception("connection refused"))

    monkeypatch.setattr(orders, "commit", lost_commit)

    with pytest.raises(OperationalError):
        run_exit(exits, orders)
    monkeypatch.undo()

    assert broker.count("place_order") == 0


@pytest.mark.usefixtures("market_open")
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (
            {"order_id": "", "status": "REJECTED", "message": "RMS: blocked", "details": SECRET},
            "Broker rejected the order: RMS: blocked",
        ),
    ],
)
def test_failed_exit_reports_error_without_broker_text(
    orders: Session,
    exits: les.LiveExitService,
    broker: FakeBroker,
    reply: object,
    message: str,
) -> None:
    broker.place_reply = reply  # type: ignore[assignment]

    result = run_exit(exits, orders)

    assert (result["status"], result["message"]) == ("ERROR", "One or more exit orders failed")
    assert result["orders"][0]["message"] == message
    assert SECRET not in repr(result)


@pytest.mark.usefixtures("market_open")
def test_exit_with_an_ambiguous_broker_reply_is_unconfirmed_not_failed(
    orders: Session, exits: les.LiveExitService, broker: FakeBroker
) -> None:
    broker.place_reply = RuntimeError(SECRET)

    result = run_exit(exits, orders)

    assert (result["status"], result["message"]) == (
        "SUBMIT_UNCONFIRMED",
        "Exit sent but not confirmed by the broker. Check the broker orderbook before "
        "squaring again.",
    )
    assert [o["status"] for o in result["orders"]] == ["SUBMIT_UNCONFIRMED"]
    assert SECRET not in repr(result)
    assert orders.query(LiveOrder).one().status == "SUBMIT_UNCONFIRMED"


# --- Routes ------------------------------------------------------------------------


def test_live_square_without_client_order_id_is_rejected_before_any_broker_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(**_kwargs: object) -> NoReturn:
        raise AssertionError("exit service reached")

    monkeypatch.setattr(trading.live_exit_service, "exit_positions", must_not_run)

    square = client.post("/api/trading/positions/square", json={"symbol": "SBIN", "mode": "LIVE"})
    exit_ = client.post("/api/trading/positions/exit", json={"symbol": "SBIN"})

    assert (square.status_code, exit_.status_code) == (422, 422)


def test_paper_square_never_reaches_the_live_exit_path(
    client: TestClient, orders: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(**_kwargs: object) -> NoReturn:
        raise AssertionError("exit service reached")

    monkeypatch.setattr(trading.live_exit_service, "exit_positions", must_not_run)

    response = client.post(
        "/api/trading/positions/square",
        json={"symbol": "SBIN", "mode": "PAPER", "client_order_id": CLIENT_ORDER_ID},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "SUCCESS",
        "mode": "PAPER",
        "message": "No paper position to square",
    }


def test_live_square_route_passes_the_request_to_the_exit_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def record(**kwargs: object) -> dict[str, Any]:
        seen.append({k: v for k, v in kwargs.items() if k != "db"})
        return {"status": "SUCCESS", "mode": "LIVE", "message": "Exit orders placed"}

    monkeypatch.setattr(trading.live_exit_service, "exit_positions", record)

    response = client.post(
        "/api/trading/positions/square",
        json={
            "symbol": "SBIN",
            "mode": "LIVE",
            "product_type": "INTRADAY",
            "client_order_id": CLIENT_ORDER_ID,
        },
    )

    assert response.json() == {"status": "SUCCESS", "mode": "LIVE", "message": "Exit orders placed"}
    assert [{**call, "client_order_id": str(call["client_order_id"])} for call in seen] == [
        {
            "symbol": "SBIN",
            "product_type": "INTRADAY",
            "client_order_id": CLIENT_ORDER_ID,
            "user_id": "default_user",
        }
    ]


# --- FyersBroker.get_working_orders -------------------------------------------------


class OrderbookClient:
    def __init__(self, response: object) -> None:
        self.response = response

    def orderbook(self) -> object:
        return self.response


class OrderbookBroker(FyersBroker):
    client: Any = None  # shadows FyersBroker.client so the test can assign it

    def __init__(self, client: OrderbookClient | None) -> None:
        self.client = client


@pytest.fixture
def reset_circuit() -> Generator[None, None, None]:
    # The circuit state is per class: the subclass keeps its own once it records a call.
    for broker_cls in (FyersBroker, OrderbookBroker):
        broker_cls._record_success()
    yield
    for broker_cls in (FyersBroker, OrderbookBroker):
        broker_cls._record_success()


def fyers_order(symbol: str, status: int, side: int, qty: int, filled: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": status,
        "side": side,
        "qty": qty,
        "filledQty": filled,
        "productType": "INTRADAY",
    }


@pytest.mark.usefixtures("reset_circuit")
def test_working_orders_keep_pending_and_transit_with_unfilled_quantity() -> None:
    orderbook = [
        fyers_order("NSE:SBIN-EQ", 6, -1, 10, 3),  # pending, partly filled
        fyers_order("NSE:INFY-EQ", 4, 1, 2, 0),  # in transit
        fyers_order("NSE:TCS-EQ", 2, 1, 5, 5),  # traded
        fyers_order("NSE:ITC-EQ", 1, 1, 5, 0),  # cancelled
    ]
    broker = OrderbookBroker(OrderbookClient({"s": "ok", "orderBook": orderbook}))

    assert broker.get_working_orders() == [
        {"symbol": "NSE:SBIN-EQ", "side": "SELL", "quantity": 7, "product_type": "INTRADAY"},
        {"symbol": "NSE:INFY-EQ", "side": "BUY", "quantity": 2, "product_type": "INTRADAY"},
    ]


@pytest.mark.usefixtures("reset_circuit")
@pytest.mark.parametrize(
    "client",
    [
        None,
        OrderbookClient({"s": "error", "message": SECRET}),
        OrderbookClient({"s": "ok", "orderBook": [fyers_order("NSE:SBIN-EQ", 6, 0, 1, 0)]}),
    ],
)
def test_working_orders_fail_closed_when_the_orderbook_is_unreadable(
    client: OrderbookClient | None,
) -> None:
    with pytest.raises(BrokerUnavailableError):
        OrderbookBroker(client).get_working_orders()


# --- FyersBroker.place_order outcome and find_order_by_tag ---------------------------


class PlaceClient:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.sent: list[dict[str, Any]] = []

    def place_order(self, data: dict[str, Any]) -> object:
        self.sent.append(data)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


MARKET_BUY = {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1, "type": "MARKET"}


@pytest.mark.usefixtures("reset_circuit")
@pytest.mark.parametrize(
    "reply",
    [
        TimeoutError(SECRET),  # the SDK call raised mid-request
        {"s": "ok", "message": "placed"},  # accepted but no id
    ],
)
def test_fyers_place_without_a_clear_reply_is_unconfirmed(reply: object) -> None:
    client = PlaceClient(reply)

    result = OrderbookBroker(client).place_order(MARKET_BUY)  # type: ignore[arg-type]

    assert result == {
        "order_id": "",
        "status": "SUBMIT_UNCONFIRMED",
        "message": "Order sent but not confirmed by the broker",
        "details": None,
    }
    assert len(client.sent) == 1


def test_fyers_place_with_the_circuit_open_is_rejected_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OrderbookBroker, "_circuit_open_until", float("inf"))
    client = PlaceClient({"s": "ok", "id": "B-1"})

    result = OrderbookBroker(client).place_order(MARKET_BUY)  # type: ignore[arg-type]

    assert (result["status"], result["message"]) == (
        "REJECTED",
        "Broker unavailable; order not sent",
    )
    assert client.sent == []


@pytest.mark.usefixtures("reset_circuit")
def test_find_order_by_tag_returns_the_tagged_order_and_its_status() -> None:
    tagged = {**fyers_order("NSE:SBIN-EQ", 2, 1, 1, 1), "id": "B-9", "orderTag": "2:" + ORDER_TAG}
    other = {**fyers_order("NSE:SBIN-EQ", 6, 1, 1, 0), "id": "B-8", "orderTag": "ST0"}
    broker = OrderbookBroker(OrderbookClient({"s": "ok", "orderBook": [other, tagged]}))

    assert broker.find_order_by_tag(ORDER_TAG) == {"order_id": "B-9", "status": "FILLED"}
    assert broker.find_order_by_tag("STNOTTHERE") is None


@pytest.mark.usefixtures("reset_circuit")
@pytest.mark.parametrize("client", [None, OrderbookClient({"s": "error"})])
def test_find_order_by_tag_fails_closed_when_the_orderbook_is_unreadable(
    client: OrderbookClient | None,
) -> None:
    with pytest.raises(BrokerUnavailableError):
        OrderbookBroker(client).find_order_by_tag(ORDER_TAG)
