"""OrderIntent: every order request is built and validated in one place, with enums.

Construction is table-driven: every valid combination of the choice fields builds the
expected intent, and every invalid value fails with the message the API returned before
the fields were enums. The API tests pin the status codes and bodies.
"""

import uuid
from collections.abc import Callable, Generator
from decimal import Decimal
from itertools import product
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.brokers.plugins.fyers import FyersBroker
from app.hat.execution.fyers_broker import PROCESS_BREAKER
from app.models.live_order import LiveOrder
from app.services import order_execution_service as oes
from app.services.execution.intent import (
    InvalidQuantityError,
    Mode,
    OrderIntent,
    OrderType,
    Product,
    Side,
)
from app.services.risk_manager import RiskManager, RiskOrder
from app.utils.clock import FixedClock
from app.utils.market_hours import MarketHoursPolicy

from fakes.broker import FakeBroker
from fakes.market import MARKET_OPEN_INSTANT

CLIENT_ORDER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
QUANTITY_MESSAGE = "Quantity must be a positive integer"
MODE_MESSAGE = "Invalid mode. Use PAPER or LIVE"
SIDE_MESSAGE = "Invalid order side. Expected BUY or SELL."
TYPE_MESSAGE = "Invalid order type. Expected MARKET, LIMIT, SL or SL-M."
PRODUCT_MESSAGE = "Invalid product. Expected INTRADAY, CNC, MARGIN, MIS, CO, BO or MTF."
INSTRUMENT_MESSAGE = "Invalid instrument type. Expected EQ, FUT, CE or PE."
CLIENT_ORDER_ID_MESSAGE = "client_order_id must be a UUID v4"

MODES = ["PAPER", "LIVE"]
SIDES = ["BUY", "SELL"]
ORDER_TYPES = ["MARKET", "LIMIT", "SL", "SL-M"]
PRODUCTS = ["INTRADAY", "CNC", "MARGIN", "MIS", "CO", "BO", "MTF"]
INSTRUMENT_TYPES = ["EQ", "FUT", "CE", "PE", None]
# The prices each type keeps from a request that sent price 810.5 and trigger 806.
KEPT_PRICES = {
    "MARKET": (None, None),
    "LIMIT": (Decimal("810.5"), None),
    "SL": (Decimal("810.5"), Decimal("806")),
    "SL-M": (None, Decimal("806")),
}


def params(**overrides: object) -> dict[str, Any]:
    return {
        "mode": "LIVE",
        "client_order_id": CLIENT_ORDER_ID,
        "symbol": "SBIN",
        "side": "BUY",
        "quantity": 2,
        "type": "LIMIT",
        "product": "INTRADAY",
        "price": 810.5,
        "trigger_price": 806.0,
        **overrides,
    }


def fields(intent: OrderIntent) -> tuple[object, ...]:
    return (
        intent.mode,
        intent.side,
        intent.order_type,
        intent.product,
        intent.instrument_type,
        intent.quantity,
        intent.price,
        intent.trigger_price,
    )


# --- Construction -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "side", "order_type", "product_type"),
    list(product(MODES, SIDES, ORDER_TYPES, PRODUCTS)),
)
def test_every_valid_combination_builds_the_intent(
    mode: str, side: str, order_type: str, product_type: str
) -> None:
    intent = OrderIntent.from_params(
        params(mode=mode, side=side, type=order_type, product=product_type)
    )

    price, trigger = KEPT_PRICES[order_type]
    expected = (
        mode,
        side,
        order_type,
        product_type,
        "EQ",
        2,
        price,
        trigger,
    )
    assert fields(intent) == expected


@pytest.mark.parametrize("instrument_type", INSTRUMENT_TYPES)
def test_every_instrument_type_builds_the_intent(instrument_type: str | None) -> None:
    intent = OrderIntent.from_params(params(instrument_type=instrument_type))

    assert intent.instrument_type == instrument_type


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # Any letter case, padded or not, as the API accepted before.
        (
            {"mode": "live", "side": " sell ", "type": "sl-m", "product": "cnc"},
            (Mode.LIVE, Side.SELL, OrderType.SL_M, Product.CNC),
        ),
        (
            {"mode": "Paper", "side": "Buy", "type": "Limit", "product": "Intraday"},
            (Mode.PAPER, Side.BUY, OrderType.LIMIT, Product.INTRADAY),
        ),
        # Missing mode, type and product take PAPER, MARKET and INTRADAY.
        (
            {"mode": None, "type": None, "product": None},
            (Mode.PAPER, Side.BUY, OrderType.MARKET, Product.INTRADAY),
        ),
    ],
)
def test_choices_are_case_insensitive_with_defaults(
    overrides: dict[str, object], expected: tuple[object, ...]
) -> None:
    intent = OrderIntent.from_params(params(**overrides))

    choices = (intent.mode, intent.side, intent.order_type, intent.product)
    assert choices == expected


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [(2, 2), (5.0, 5), ("7", 7), (Decimal("3"), 3)],
)
def test_quantity_is_a_positive_int(quantity: object, expected: int) -> None:
    assert OrderIntent.from_params(params(quantity=quantity)).quantity == expected


def test_mode_argument_overrides_the_mapping() -> None:
    intent = OrderIntent.from_params(params(mode="LIVE"), mode="paper")

    assert intent.mode is Mode.PAPER


def test_client_order_id_is_normalized_to_lowercase() -> None:
    intent = OrderIntent.from_params(params(client_order_id=CLIENT_ORDER_ID.upper()))

    assert intent.client_order_id == CLIENT_ORDER_ID


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"mode": "DEMO"}, ValueError, MODE_MESSAGE),
        ({"side": "HOLD"}, ValueError, SIDE_MESSAGE),
        ({"side": None}, ValueError, SIDE_MESSAGE),
        ({"type": "ICEBERG"}, ValueError, TYPE_MESSAGE),
        ({"type": "STOP"}, ValueError, TYPE_MESSAGE),
        ({"product": "XYZ"}, ValueError, PRODUCT_MESSAGE),
        ({"instrument_type": "OPT"}, ValueError, INSTRUMENT_MESSAGE),
        ({"quantity": 0}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": -3}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": 2.5}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": "2.5"}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": "two"}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": True}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"quantity": None}, InvalidQuantityError, QUANTITY_MESSAGE),
        ({"client_order_id": "abc"}, ValueError, CLIENT_ORDER_ID_MESSAGE),
        ({"client_order_id": str(uuid.uuid1())}, ValueError, CLIENT_ORDER_ID_MESSAGE),
        ({"symbol": None}, ValueError, "Invalid symbol"),
        # The first invalid field is reported: mode before quantity.
        ({"mode": "DEMO", "quantity": 0}, ValueError, MODE_MESSAGE),
    ],
)
def test_invalid_values_fail_with_the_user_facing_message(
    overrides: dict[str, object], error: type[Exception], message: str
) -> None:
    order = params(**overrides)

    with pytest.raises(error) as raised:
        OrderIntent.from_params(order)

    assert str(raised.value) == message


# --- Cross-field price rules -------------------------------------------------------


@pytest.mark.parametrize(
    ("order_type", "price", "trigger", "message"),
    [
        ("MARKET", None, None, None),
        ("MARKET", 0, 0, None),
        ("LIMIT", 810.0, None, None),
        ("LIMIT", None, 806.0, "LIMIT order requires a positive limit price."),
        ("LIMIT", 0, None, "LIMIT order requires a positive limit price."),
        ("LIMIT", -1, None, "LIMIT order requires a positive limit price."),
        ("SL", 805.0, 806.0, None),
        ("SL", 805.0, None, "SL order requires a positive trigger price."),
        ("SL", None, 806.0, "SL order requires a positive limit price."),
        ("SL", None, None, "SL order requires a positive trigger price."),
        ("SL-M", None, 806.0, None),
        ("SL-M", 805.0, 0, "SL-M order requires a positive trigger price."),
    ],
)
def test_price_rules_per_order_type(
    order_type: str, price: float | None, trigger: float | None, message: str | None
) -> None:
    intent = OrderIntent.from_params(params(type=order_type, price=price, trigger_price=trigger))

    assert intent.price_error == message


# --- Through the service --------------------------------------------------------------


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    db_session.query(LiveOrder).delete()
    db_session.commit()


@pytest.fixture
def tick(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    def set_tick(price: float) -> None:
        monkeypatch.setattr(oes, "latest_tick_price", lambda _symbol: price)

    return set_tick


def test_paper_market_order_ignores_a_stray_price(
    orders: Session, fake_broker: FakeBroker, tick: Callable[[float], None]
) -> None:
    tick(0.0)
    service = oes.OrderExecutionService()

    result = service.place_order(params(type="MARKET", price=810.0), orders, mode="PAPER")

    message = (
        "No market price available for SBIN; paper MARKET order rejected. "
        "Subscribe the symbol to live data or use a LIMIT order."
    )
    assert (result["status"], result["message"]) == ("REJECTED", message)
    stored = orders.query(LiveOrder).one()
    assert (stored.order_type, stored.price, stored.trigger_price) == ("MARKET", 0.0, 0.0)
    assert fake_broker.calls == []


def test_live_market_order_is_priced_from_the_quote_not_a_stray_price(
    orders: Session, fake_broker: FakeBroker
) -> None:
    fake_broker.ltp = None
    manager = RiskManager(market_hours=MarketHoursPolicy(FixedClock(MARKET_OPEN_INSTANT)))
    manager._broker = fake_broker  # type: ignore[assignment]

    result = manager.pre_trade_check(
        params(type="MARKET", price=1.0), orders, user_id="default_user"
    )

    assert (result.code, result.message) == (
        "PRICE_ESTIMATE_FAILED",
        "Cannot estimate price for SBIN. Please provide limit price.",
    )


class RecordingFyersClient:
    """The fyers-apiv3 client's place_order, recording each payload."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self.placed.append(data)
        return {"s": "ok", "id": "FY-1", "message": "Order placed"}


class RecordingBroker(FyersBroker):
    def __init__(self, client: RecordingFyersClient) -> None:
        super().__init__()
        self._client_source = lambda: client


@pytest.mark.parametrize(
    ("product_type", "sent"),
    [
        ("INTRADAY", "INTRADAY"),
        ("MIS", "INTRADAY"),
        ("CNC", "CNC"),
        ("MARGIN", "MARGIN"),
        ("MTF", "MTF"),
        ("CO", "CO"),
        ("BO", "BO"),
    ],
)
def test_every_product_reaches_fyers_as_itself(product_type: str, sent: str) -> None:
    PROCESS_BREAKER.record_success()
    client = RecordingFyersClient()
    service = oes.OrderExecutionService()
    service._broker = RecordingBroker(client)

    service._submit_live_order(
        params(type="MARKET", product=product_type),
        "NSE:SBIN-EQ",
        LiveOrder(status="PENDING_SUBMIT"),
    )

    assert [data["productType"] for data in client.placed] == [sent]


@pytest.mark.parametrize(
    ("product_type", "required"),
    [("INTRADAY", 320.0), ("MIS", 320.0), ("CNC", 1600.0), ("MTF", 1600.0)],
)
def test_mis_is_margined_as_intraday(
    fake_broker: FakeBroker, product_type: str, required: float
) -> None:
    manager = RiskManager()
    manager._broker = fake_broker  # type: ignore[assignment]
    order = RiskOrder.of(OrderIntent.from_params(params(product=product_type, price=800)))

    result = manager._check_available_margin(order)

    assert (result.code, result.details) == (
        "MARGIN_OK",
        {"available": 1_000_000.0, "required": required},
    )


# --- API status codes -----------------------------------------------------------------


@pytest.fixture
def api(client: TestClient, orders: Session) -> TestClient:
    return client


PAPER_BODY = {
    "symbol": "SBIN",
    "side": "BUY",
    "quantity": 2,
    "type": "LIMIT",
    "product": "INTRADAY",
    "price": 810.0,
    "mode": "PAPER",
}


@pytest.mark.parametrize(
    ("path", "method", "body", "detail"),
    [
        ("/api/trading/order", "post", {**PAPER_BODY, "mode": "DEMO"}, MODE_MESSAGE),
        ("/api/trading/order", "post", {**PAPER_BODY, "side": "HOLD"}, SIDE_MESSAGE),
        ("/api/trading/order", "post", {**PAPER_BODY, "type": "ICEBERG"}, TYPE_MESSAGE),
        ("/api/trading/order", "post", {**PAPER_BODY, "product": "XYZ"}, PRODUCT_MESSAGE),
        (
            "/api/trading/order",
            "post",
            {**PAPER_BODY, "instrument_type": "OPT"},
            INSTRUMENT_MESSAGE,
        ),
        (
            "/api/trading/order",
            "put",
            {"order_id": "ORD-X", "new_type": "STOP"},
            TYPE_MESSAGE,
        ),
        (
            "/api/trading/positions/square",
            "post",
            {"mode": "DEMO"},
            "mode must be PAPER or LIVE",
        ),
        (
            "/api/trading/risk-check",
            "post",
            {"symbol": "SBIN", "side": "HOLD", "quantity": 1},
            SIDE_MESSAGE,
        ),
    ],
)
def test_invalid_choice_answers_400_with_its_message(
    api: TestClient,
    fake_broker: FakeBroker,
    path: str,
    method: str,
    body: dict[str, Any],
    detail: str,
) -> None:
    response = api.request(method.upper(), path, headers={"x-user-id": "u1"}, json=body)

    assert (response.status_code, response.json()) == (400, {"detail": detail})
    assert fake_broker.calls == []


def test_lowercase_choices_are_accepted_and_stored_uppercase(
    api: TestClient,
    orders: Session,
    fake_broker: FakeBroker,
    tick: Callable[[float], None],
) -> None:
    tick(805.0)
    body = {**PAPER_BODY, "mode": "paper", "side": "buy", "type": "limit", "product": "cnc"}

    response = api.post("/api/trading/order", headers={"x-user-id": "u1"}, json=body)

    assert (response.status_code, response.json()["status"]) == (200, "FILLED")
    stored = orders.query(LiveOrder).one()
    assert (stored.side, stored.order_type, stored.product_type, stored.is_paper) == (
        "BUY",
        "LIMIT",
        "CNC",
        1,
    )
    assert fake_broker.calls == []


def test_openapi_lists_the_enum_values(api: TestClient) -> None:
    schemas = api.get("/openapi.json").json()["components"]["schemas"]

    assert {name: schemas[name]["enum"] for name in ("Mode", "Side", "OrderType")} == {
        "Mode": ["PAPER", "LIVE"],
        "Side": ["BUY", "SELL"],
        "OrderType": ["MARKET", "LIMIT", "SL", "SL-M"],
    }
    assert schemas["Product"]["enum"] == PRODUCTS
    assert schemas["InstrumentType"]["enum"] == ["EQ", "FUT", "CE", "PE"]
