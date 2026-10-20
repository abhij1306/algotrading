"""Regression tests for live-trading defects: quote parsing in risk checks,
stop-loss order mapping, position sync during a broker outage, the margin
check, paper fills, live modify and exit-position."""

import time
from collections.abc import Callable, Generator
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.brokers.plugins.fyers import FyersBroker
from app.models.live_order import LiveOrder
from app.models.live_position import LivePosition
from app.services import order_execution_service as oes_module
from app.services import risk_manager as risk_module
from app.services.live_market_service import live_market
from app.services.order_execution_service import OrderExecutionService
from app.services.position_sync_service import PositionSyncService
from app.services.risk_manager import RiskConfig, RiskManager, RiskStatus

# A quote as FyersBroker.get_quote() returns it: one entry of the Fyers "d" list.
SBIN_QUOTE = {"n": "NSE:SBIN-EQ", "s": "ok", "v": {"lp": 812.5, "bid": 812.4, "ask": 812.6}}


class FakeFyersClient:
    """Stands in for the fyers-apiv3 client and records every call."""

    def __init__(
        self,
        positions_response: dict[str, Any] | None = None,
        quotes_response: dict[str, Any] | None = None,
        funds_response: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.positions_response = positions_response or {"s": "ok", "netPositions": []}
        self.quotes_response = quotes_response or {"s": "ok", "d": [SBIN_QUOTE]}
        self.funds_response = funds_response or {"s": "ok", "fund_limit": []}

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("place_order", data))
        return {"s": "ok", "id": "FY-1", "message": "Order placed"}

    def positions(self) -> dict[str, Any]:
        self.calls.append(("positions", None))
        return self.positions_response

    def quotes(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("quotes", data))
        return self.quotes_response

    def funds(self) -> dict[str, Any]:
        self.calls.append(("funds", None))
        return self.funds_response

    def modify_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("modify_order", data))
        return {"s": "ok", "id": data["id"], "message": "Order modified"}


class FakeClientBroker(FyersBroker):
    """FyersBroker with a fixed client instead of the live Fyers login."""

    client: Any = None  # shadows FyersBroker.client so tests can assign it

    def __init__(self, client: FakeFyersClient | None) -> None:
        self.client = client


@pytest.fixture(autouse=True)
def reset_circuit_breaker() -> Generator[None, None, None]:
    for broker_cls in (FyersBroker, FakeClientBroker):
        broker_cls._record_success()
    yield
    for broker_cls in (FyersBroker, FakeClientBroker):
        broker_cls._record_success()


def make_broker(client: FakeFyersClient | None) -> FyersBroker:
    return FakeClientBroker(client)


# --- 1. Risk price estimate reads the real Fyers quote shape ---------------


def test_risk_price_estimate_reads_fyers_quote_shape() -> None:
    client = FakeFyersClient()
    manager = RiskManager()
    manager._broker = make_broker(client)

    assert manager._get_estimated_price("NSE:SBIN-EQ") == 812.5
    assert client.calls == [("quotes", {"symbols": "NSE:SBIN-EQ"})]


def test_risk_exposure_check_prices_market_order_from_quote(db_session: Session) -> None:
    client = FakeFyersClient()
    manager = RiskManager(RiskConfig(max_total_exposure=8000))
    manager._broker = make_broker(client)
    db_session.query(LivePosition).delete()
    db_session.commit()

    result = manager._check_total_exposure(
        {"symbol": "SBIN", "fyers_symbol": "NSE:SBIN-EQ", "quantity": 10, "price": 0},
        db_session,
    )

    assert result.code == "EXPOSURE_LIMIT"
    assert result.details == {
        "current_exposure": 0,
        "new_exposure": 8125.0,
        "total": 8125.0,
        "limit": 8000,
    }
    assert client.calls == [("quotes", {"symbols": "NSE:SBIN-EQ"})]


def test_risk_price_estimate_fails_when_quote_has_no_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RISK_DEFAULT_PRICE", raising=False)
    manager = RiskManager()
    manager._broker = make_broker(FakeFyersClient(quotes_response={"s": "error", "d": []}))

    result = manager.check_large_order({"symbol": "NSE:SBIN-EQ", "quantity": 10, "price": 0})

    assert result.code == "PRICE_ESTIMATE_FAILED"
    assert result.status.value == "WARNING"


@pytest.mark.parametrize(
    ("symbol", "fyers_symbol", "quoted_symbol"),
    [
        ("SBIN", None, "NSE:SBIN-EQ"),
        ("NSE:SBIN-EQ", None, "NSE:SBIN-EQ"),
        ("NSE:NIFTY2520622500CE", None, "NSE:NIFTY2520622500CE"),
        ("NIFTY2520622500CE", "NSE:NIFTY2520622500CE", "NSE:NIFTY2520622500CE"),
    ],
)
def test_risk_price_estimate_quotes_fyers_symbol(
    symbol: str, fyers_symbol: str | None, quoted_symbol: str
) -> None:
    client = FakeFyersClient()
    manager = RiskManager()
    manager._broker = make_broker(client)

    assert manager._get_estimated_price(symbol, fyers_symbol) == 812.5
    assert client.calls == [("quotes", {"symbols": quoted_symbol})]


def test_large_order_check_from_bare_symbol_quotes_fyers_symbol() -> None:
    client = FakeFyersClient()
    manager = RiskManager(RiskConfig(large_order_notional=10000))
    manager._broker = make_broker(client)

    result = manager.check_large_order({"symbol": "SBIN", "quantity": 20, "price": 0})

    assert result.code == "LARGE_ORDER_CONFIRMATION"
    assert result.details == {"notional": 16250.0, "threshold": 10000}
    assert client.calls == [("quotes", {"symbols": "NSE:SBIN-EQ"})]


# --- 2. Live stop-loss orders reach Fyers with the trigger price -----------


@pytest.mark.parametrize(
    ("order_type", "price", "trigger_price", "fyers_type", "limit_price", "stop_price"),
    [
        ("MARKET", 0.0, 0.0, 2, 0, 0),
        ("LIMIT", 810.0, 0.0, 1, 810.0, 0),
        ("SL", 805.0, 806.0, 4, 805.0, 806.0),
        ("SL-M", 0.0, 806.0, 3, 0, 806.0),
    ],
)
def test_live_order_payload_per_order_type(
    order_type: str,
    price: float,
    trigger_price: float,
    fyers_type: int,
    limit_price: float,
    stop_price: float,
) -> None:
    client = FakeFyersClient()
    service = OrderExecutionService()
    service._broker = make_broker(client)
    order = LiveOrder(status="PENDING")

    response = service._submit_live_order(
        {
            "symbol": "SBIN",
            "side": "SELL",
            "quantity": 5,
            "type": order_type,
            "product": "INTRADAY",
            "price": price,
            "trigger_price": trigger_price,
        },
        "NSE:SBIN-EQ",
        order,
    )

    assert response["status"] == "SUBMITTED"
    assert order.status == "SUBMITTED"
    assert client.calls == [
        (
            "place_order",
            {
                "symbol": "NSE:SBIN-EQ",
                "qty": 5,
                "type": fyers_type,
                "side": -1,
                "productType": "INTRADAY",
                "limitPrice": limit_price,
                "stopPrice": stop_price,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            },
        )
    ]


@pytest.mark.parametrize("order_type", ["SL", "SL-M"])
@pytest.mark.parametrize("trigger_price", [None, 0, -1])
def test_stop_order_without_trigger_is_rejected_before_broker(
    order_type: str, trigger_price: float | None
) -> None:
    client = FakeFyersClient()
    broker = make_broker(client)

    response = broker.place_order(
        {
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "type": order_type,
            "price": 805.0,
            "trigger_price": trigger_price,
        }
    )

    assert response["status"] == "REJECTED"
    assert response["message"] == f"{order_type} order requires a positive trigger price."
    assert client.calls == []


@pytest.mark.parametrize("order_type", ["LIMIT", "SL"])
@pytest.mark.parametrize("price", [None, 0, -1])
def test_limit_priced_order_without_price_is_rejected_before_broker(
    order_type: str, price: float | None
) -> None:
    client = FakeFyersClient()

    response = make_broker(client).place_order(
        {
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "type": order_type,
            "price": price,
            "trigger_price": 806.0,
        }
    )

    assert response["status"] == "REJECTED"
    assert response["message"] == f"{order_type} order requires a positive limit price."
    assert client.calls == []


def test_unknown_order_type_is_rejected_before_broker() -> None:
    client = FakeFyersClient()

    response = make_broker(client).place_order(
        {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1, "type": "STOP", "price": 1}
    )

    assert response["status"] == "REJECTED"
    assert client.calls == []


def test_live_order_sends_the_client_order_id_as_fyers_order_tag() -> None:
    client = FakeFyersClient()
    service = OrderExecutionService()
    service._broker = make_broker(client)

    service._submit_live_order(
        {"symbol": "SBIN", "side": "BUY", "quantity": 1, "type": "MARKET", "product": "CNC"},
        "NSE:SBIN-EQ",
        LiveOrder(status="PENDING_SUBMIT"),
        "3f2b8c1e-9d4a-4b6e-8f10-2a7c5e9d1b34",
    )

    assert client.calls == [
        (
            "place_order",
            {
                "symbol": "NSE:SBIN-EQ",
                "qty": 1,
                "type": 2,
                "side": 1,
                "productType": "CNC",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "orderTag": "ST3F2B8C1E9D4A4B6E8F",
            },
        )
    ]


@pytest.mark.parametrize("order_tag", ["", "terminal-options", "A" * 21])
def test_order_tag_fyers_cannot_accept_is_rejected_before_broker(order_tag: str) -> None:
    client = FakeFyersClient()

    response = make_broker(client).place_order(
        {
            "symbol": "NSE:SBIN-EQ",
            "side": "BUY",
            "quantity": 1,
            "type": "MARKET",
            "order_tag": order_tag,
        }
    )

    assert response["status"] == "REJECTED"
    assert response["message"] == "Invalid order tag. Expected 1-20 letters or digits."
    assert client.calls == []


# --- 3. Position sync never wipes stored positions on a broker outage -----


@pytest.fixture
def sync_session_factory(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[sessionmaker, None, None]:
    factory = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    monkeypatch.setattr("app.services.position_sync_service.SessionLocal", factory)
    with factory() as db:
        db.query(LivePosition).delete()
        db.add(
            LivePosition(
                id="default_user-NSE:SBIN-EQ-INTRADAY",
                user_id="default_user",
                symbol="SBIN",
                fyers_symbol="NSE:SBIN-EQ",
                product_type="INTRADAY",
                side="LONG",
                net_qty=10,
                net_avg=800.0,
                ltp=812.5,
                unrealized_pl=125.0,
            )
        )
        db.commit()
    yield factory
    with factory() as db:
        db.query(LivePosition).delete()
        db.commit()


def stored_positions(factory: sessionmaker) -> list[tuple[str, int, float]]:
    with factory() as db:
        return [
            (p.id, p.net_qty, p.unrealized_pl)
            for p in db.query(LivePosition).order_by(LivePosition.id).all()
        ]


@pytest.mark.parametrize(
    "client",
    [
        None,  # client never initialised (not logged in)
        FakeFyersClient(positions_response={"s": "error", "message": "token expired"}),
    ],
)
def test_position_sync_aborts_without_touching_positions_when_broker_unavailable(
    sync_session_factory: sessionmaker, client: FakeFyersClient | None
) -> None:
    service = PositionSyncService()
    service._broker = make_broker(client)

    result = service.sync_positions()

    assert result == {
        "status": "error",
        "code": "BROKER_UNAVAILABLE",
        "message": "Broker unavailable; stored positions were not changed",
    }
    assert stored_positions(sync_session_factory) == [
        ("default_user-NSE:SBIN-EQ-INTRADAY", 10, 125.0)
    ]


def test_position_sync_updates_and_closes_by_symbol_and_product(
    sync_session_factory: sessionmaker,
) -> None:
    # SBIN is now held as CNC only, so the INTRADAY row must close.
    client = FakeFyersClient(
        positions_response={
            "s": "ok",
            "netPositions": [
                {
                    "symbol": "NSE:SBIN-EQ",
                    "netQty": 4,
                    "buyAvg": 805.0,
                    "sellAvg": 0,
                    "ltp": 812.5,
                    "pl": 30.0,
                    "productType": "CNC",
                }
            ],
        }
    )
    service = PositionSyncService()
    service._broker = make_broker(client)

    result = service.sync_positions()

    assert {k: result[k] for k in ("status", "synced", "updated", "closed")} == {
        "status": "success",
        "synced": 1,
        "updated": 0,
        "closed": 1,
    }
    assert stored_positions(sync_session_factory) == [
        ("default_user-NSE:SBIN-EQ-CNC", 4, 30.0),
        ("default_user-NSE:SBIN-EQ-INTRADAY", 0, 0.0),
    ]


# --- 4. Margin check reads BrokerFunds by key ------------------------------


def funds_response(available: float) -> dict[str, Any]:
    return {
        "s": "ok",
        "fund_limit": [
            {"title": "Total Balance", "equityAmount": 50000.0},
            {"title": "Available Balance", "equityAmount": available},
            {"title": "Utilized Amount", "equityAmount": 50000.0 - available},
        ],
    }


def test_margin_check_passes_with_sufficient_funds() -> None:
    client = FakeFyersClient(funds_response=funds_response(20000.0))
    manager = RiskManager()
    manager._broker = make_broker(client)

    result = manager._check_available_margin(
        {"symbol": "SBIN", "quantity": 10, "price": 800.0, "product": "CNC"}
    )

    assert result.status == RiskStatus.PASS
    assert result.code == "MARGIN_OK"
    assert result.details == {"available": 20000.0, "required": 8000.0}
    assert client.calls == [("funds", None)]


def test_margin_check_fails_with_insufficient_funds() -> None:
    manager = RiskManager()
    manager._broker = make_broker(FakeFyersClient(funds_response=funds_response(5000.0)))

    result = manager._check_available_margin(
        {"symbol": "SBIN", "quantity": 10, "price": 800.0, "product": "CNC"}
    )

    assert result.status == RiskStatus.FAIL
    assert result.code == "INSUFFICIENT_MARGIN"
    assert result.details == {"available": 5000.0, "required": 8000.0, "shortfall": 3000.0}


# --- 5. PAPER orders fill end-to-end and never touch the broker ------------


@pytest.fixture
def order_db(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.query(LiveOrder).delete()
    db_session.commit()


@pytest.fixture
def market_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_market, "is_market_open", lambda: True)


@pytest.fixture
def sbin_tick(monkeypatch: pytest.MonkeyPatch, market_open: None) -> None:
    tick = {"symbol": "SBIN", "ltp": 812.5, "exch_feed_time": int(time.time())}
    monkeypatch.setitem(live_market.latest_values, "SBIN", tick)


def place_paper_order(
    db: Session, client: FakeFyersClient, **overrides: object
) -> tuple[dict[str, Any], LiveOrder]:
    service = OrderExecutionService()
    service._broker = make_broker(client)
    params: dict[str, Any] = {
        "symbol": "SBIN",
        "fyers_symbol": "NSE:SBIN-EQ",
        "side": "BUY",
        "quantity": 10,
        "type": "MARKET",
        "product": "INTRADAY",
        "price": 0.0,
        "trigger_price": None,
        "user_id": "paper_user",
        **overrides,
    }
    response = service.place_order(params, db, mode="PAPER")
    stored = db.query(LiveOrder).filter(LiveOrder.internal_id == response["order_id"]).one()
    return response, stored


@pytest.mark.usefixtures("sbin_tick")
def test_paper_market_order_fills_from_market_data_tick(order_db: Session) -> None:
    client = FakeFyersClient()

    response, stored = place_paper_order(order_db, client)

    assert {k: response[k] for k in ("status", "average_price", "filled_qty")} == {
        "status": "FILLED",
        "average_price": 812.91,
        "filled_qty": 10,
    }
    assert response["estimated_charges"] == 2.85
    assert (
        stored.status,
        stored.is_paper,
        stored.filled_qty,
        stored.average_price,
        stored.trigger_price,
    ) == ("FILLED", 1, 10, 812.91, 0.0)
    assert client.calls == []


@pytest.mark.usefixtures("sbin_tick")
@pytest.mark.parametrize(
    ("side", "limit_price", "status", "average_price", "filled_qty"),
    [
        ("BUY", 815.0, "FILLED", 812.91, 10),  # marketable: tick + slippage
        ("BUY", 812.6, "FILLED", 812.6, 10),  # slippage capped at the limit
        ("BUY", 810.0, "SUBMITTED", 0.0, 0),  # below market: rests
        ("SELL", 810.0, "FILLED", 812.09, 10),
    ],
)
def test_paper_limit_order_against_market_data_tick(
    order_db: Session,
    side: str,
    limit_price: float,
    status: str,
    average_price: float,
    filled_qty: int,
) -> None:
    client = FakeFyersClient()

    response, stored = place_paper_order(
        order_db, client, side=side, type="LIMIT", price=limit_price
    )

    assert (response["status"], response["average_price"], response["filled_qty"]) == (
        status,
        average_price,
        filled_qty,
    )
    assert (stored.status, stored.price, stored.average_price) == (
        status,
        limit_price,
        average_price,
    )
    assert client.calls == []


def test_paper_limit_order_without_tick_uses_its_limit_price(order_db: Session) -> None:
    client = FakeFyersClient()

    response, stored = place_paper_order(order_db, client, type="LIMIT", price=810.0)

    assert (response["status"], response["average_price"]) == ("FILLED", 810.0)
    assert stored.status == "FILLED"
    assert client.calls == []


@pytest.mark.parametrize(
    ("order_type", "price", "trigger_price"),
    [("MARKET", 0.0, None), ("SL-M", 0.0, 806.0)],
)
def test_paper_order_without_any_price_is_rejected_without_broker(
    order_db: Session, order_type: str, price: float, trigger_price: float | None
) -> None:
    client = FakeFyersClient()

    response, stored = place_paper_order(
        order_db, client, type=order_type, price=price, trigger_price=trigger_price
    )

    message = (
        f"No market price available for SBIN; paper {order_type} order rejected. "
        "Subscribe the symbol to live data or use a LIMIT order."
    )
    assert (response["status"], response["message"]) == ("REJECTED", message)
    assert (stored.status, stored.reject_reason, stored.filled_qty) == ("REJECTED", message, 0)
    assert client.calls == []


NO_SBIN_MARKET_PRICE = (
    "No market price available for SBIN; paper MARKET order rejected. "
    "Subscribe the symbol to live data or use a LIMIT order."
)


@pytest.mark.parametrize(
    ("tick_age_sec", "is_open", "has_time"),
    [
        (120, True, True),  # older than the 60s default
        (0, False, True),  # fresh, but the market is closed
        (0, True, False),  # no exchange time on the tick
    ],
)
def test_paper_order_ignores_stale_or_closed_market_tick(
    order_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tick_age_sec: int,
    is_open: bool,
    has_time: bool,
) -> None:
    monkeypatch.delenv("PAPER_TICK_MAX_AGE_SEC", raising=False)
    monkeypatch.setattr(live_market, "is_market_open", lambda: is_open)
    tick: dict[str, Any] = {"symbol": "SBIN", "ltp": 812.5}
    if has_time:
        tick["exch_feed_time"] = int(time.time()) - tick_age_sec
    monkeypatch.setitem(live_market.latest_values, "SBIN", tick)
    client = FakeFyersClient()

    market_response, _ = place_paper_order(order_db, client)
    limit_response, _ = place_paper_order(order_db, client, type="LIMIT", price=800.0)

    assert (market_response["status"], market_response["message"]) == (
        "REJECTED",
        NO_SBIN_MARKET_PRICE,
    )
    # A LIMIT falls back to its own price instead of the stale 812.5 tick.
    assert (limit_response["status"], limit_response["average_price"]) == ("FILLED", 800.0)
    assert client.calls == []


@pytest.mark.usefixtures("market_open")
def test_paper_tick_max_age_is_configurable(
    order_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_TICK_MAX_AGE_SEC", "300")
    tick = {"symbol": "SBIN", "ltp": 812.5, "last_traded_time": int(time.time()) - 120}
    monkeypatch.setitem(live_market.latest_values, "SBIN", tick)

    response, _ = place_paper_order(order_db, FakeFyersClient())

    assert (response["status"], response["average_price"]) == ("FILLED", 812.91)


@pytest.mark.usefixtures("sbin_tick")
@pytest.mark.parametrize("order_type", ["SL", "SL-M"])
@pytest.mark.parametrize("trigger_price", [None, 0, -1])
def test_paper_stop_order_without_trigger_is_rejected(
    order_db: Session, order_type: str, trigger_price: float | None
) -> None:
    client = FakeFyersClient()

    response, stored = place_paper_order(
        order_db, client, type=order_type, price=815.0, trigger_price=trigger_price
    )

    message = f"{order_type} order requires a positive trigger price."
    assert (response["status"], response["message"], response["filled_qty"]) == (
        "REJECTED",
        message,
        0,
    )
    assert (stored.status, stored.reject_reason) == ("REJECTED", message)
    assert client.calls == []


# --- 6a. Paper modify applies or rejects every change ----------------------


def add_paper_order(
    db: Session, order_type: str, price: float, trigger_price: float, status: str = "SUBMITTED"
) -> LiveOrder:
    order = LiveOrder(
        id="ORD-PAPER1",
        internal_id="ORD-PAPER1",
        user_id="paper_user",
        symbol="SBIN",
        fyers_symbol="NSE:SBIN-EQ",
        side="SELL",
        quantity=5,
        order_type=order_type,
        product_type="INTRADAY",
        price=price,
        trigger_price=trigger_price,
        status=status,
        is_paper=1,
    )
    db.add(order)
    db.commit()
    return order


@pytest.mark.parametrize(
    ("params", "stored"),
    [
        # Market at 812: the SELL stops stay untriggered and rest.
        ({"trigger_price": 807.0}, ("SL", 5, 805.0, 807.0, "SUBMITTED", 0, 0.0)),
        ({"price": 804.0, "quantity": 8}, ("SL", 8, 804.0, 806.0, "SUBMITTED", 0, 0.0)),
        ({"order_type": "SL-M"}, ("SL-M", 5, 805.0, 806.0, "SUBMITTED", 0, 0.0)),
        # A SELL limit at 803 is marketable: 812 less 5 bps = 811.594 -> 811.59.
        (
            {"order_type": "LIMIT", "price": 803.0},
            ("LIMIT", 5, 803.0, 806.0, "FILLED", 5, 811.59),
        ),
    ],
)
def test_paper_modify_applies_type_price_and_trigger(
    order_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    stored: tuple[str, int, float, float, str, int, float],
) -> None:
    monkeypatch.setattr(oes_module, "latest_tick_price", lambda _symbol: 812.0)
    order = add_paper_order(order_db, "SL", 805.0, 806.0)
    client = FakeFyersClient()
    service = OrderExecutionService()
    service._broker = make_broker(client)

    result = service.modify_order("ORD-PAPER1", params, order_db)

    assert (result["status"], result["order_status"]) == ("SUCCESS", stored[4])
    order_db.refresh(order)
    modified = (
        order.order_type,
        order.quantity,
        order.price,
        order.trigger_price,
        order.status,
        order.filled_qty,
        order.average_price,
    )
    assert modified == stored
    assert client.calls == []


@pytest.mark.parametrize(
    ("order_type", "price", "trigger_price", "status", "params", "message"),
    [
        (
            "LIMIT",
            805.0,
            0.0,
            "SUBMITTED",
            {"order_type": "SL-M"},
            "SL-M order requires a positive trigger price.",
        ),
        (
            "SL",
            805.0,
            806.0,
            "SUBMITTED",
            {"trigger_price": 0},
            "Trigger price must be a positive number",
        ),
        (
            "SL",
            805.0,
            806.0,
            "FILLED",
            {"trigger_price": 807.0},
            "Cannot modify order in FILLED state",
        ),
    ],
)
def test_paper_modify_rejects_invalid_change_without_saving(
    order_db: Session,
    order_type: str,
    price: float,
    trigger_price: float,
    status: str,
    params: dict[str, Any],
    message: str,
) -> None:
    order = add_paper_order(order_db, order_type, price, trigger_price, status)

    result = OrderExecutionService().modify_order("ORD-PAPER1", params, order_db)

    assert result == {"status": "ERROR", "message": message}
    order_db.refresh(order)
    assert (order.order_type, order.price, order.trigger_price) == (
        order_type,
        price,
        trigger_price,
    )


# --- 6. Live modify keeps the order type and trigger -----------------------


def add_live_order(db: Session, order_id: str, order_type: str, trigger_price: float) -> LiveOrder:
    order = LiveOrder(
        id=order_id,
        internal_id=f"ORD-{order_id}",
        user_id="u1",
        symbol="SBIN",
        fyers_symbol="NSE:SBIN-EQ",
        side="SELL",
        quantity=5,
        order_type=order_type,
        product_type="INTRADAY",
        price=805.0,
        trigger_price=trigger_price,
        status="SUBMITTED",
        is_paper=0,
    )
    db.add(order)
    db.commit()
    return order


@pytest.mark.parametrize(
    ("params", "payload", "stored"),
    [
        (
            {"price": 804.0},
            {"id": "FY-1", "type": 4, "limitPrice": 804.0, "stopPrice": 806.0, "qty": 5},
            ("SL", 5, 804.0, 806.0),
        ),
        (
            {"quantity": 8},
            {"id": "FY-1", "type": 4, "limitPrice": 805.0, "stopPrice": 806.0, "qty": 8},
            ("SL", 8, 805.0, 806.0),
        ),
        (
            {"trigger_price": 807.0, "price": 806.0},
            {"id": "FY-1", "type": 4, "limitPrice": 806.0, "stopPrice": 807.0, "qty": 5},
            ("SL", 5, 806.0, 807.0),
        ),
        (
            {"order_type": "SL-M"},
            {"id": "FY-1", "type": 3, "limitPrice": 0, "stopPrice": 806.0, "qty": 5},
            ("SL-M", 5, 805.0, 806.0),
        ),
        (
            {"order_type": "MARKET"},
            {"id": "FY-1", "type": 2, "limitPrice": 0, "stopPrice": 0, "qty": 5},
            ("MARKET", 5, 805.0, 806.0),
        ),
    ],
)
def test_live_modify_preserves_order_type_and_trigger(
    order_db: Session,
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    payload: dict[str, Any],
    stored: tuple[str, int, float, float],
    freeze_market: Callable[[bool], None],
) -> None:
    # Market open and enough margin, so the modify risk checks pass.
    freeze_market(True)
    risk_client = FakeFyersClient(funds_response=funds_response(40000.0))
    monkeypatch.setattr(risk_module.risk_manager, "_broker", make_broker(risk_client))
    order = add_live_order(order_db, "FY-1", "SL", 806.0)
    client = FakeFyersClient()
    service = OrderExecutionService()
    service._broker = make_broker(client)

    result = service.modify_order("FY-1", params, order_db)

    assert result == {"status": "SUCCESS", "message": "Order Modified"}
    assert client.calls == [("quotes", {"symbols": "NSE:SBIN-EQ"}), ("modify_order", payload)]
    order_db.refresh(order)
    modified = (order.order_type, order.quantity, order.price, order.trigger_price)
    assert modified == stored


def test_live_modify_to_stop_without_trigger_is_rejected_before_broker(
    order_db: Session,
) -> None:
    add_live_order(order_db, "FY-2", "LIMIT", 0.0)
    client = FakeFyersClient()
    service = OrderExecutionService()
    service._broker = make_broker(client)

    result = service.modify_order("FY-2", {"order_type": "SL-M"}, order_db)

    assert result == {"status": "ERROR", "message": "SL-M order requires a positive trigger price."}
    assert client.calls == []


# --- 7. Exit position uses dict keys and fails closed ----------------------


def test_exit_position_places_opposite_market_order_for_matching_symbol() -> None:
    client = FakeFyersClient(
        positions_response={
            "s": "ok",
            "netPositions": [
                {
                    "symbol": "NSE:SBIN-EQ",
                    "netQty": 4,
                    "buyAvg": 805.0,
                    "sellAvg": 0,
                    "ltp": 812.5,
                    "pl": 30.0,
                    "productType": "CNC",
                },
                {
                    "symbol": "NSE:INFY-EQ",
                    "netQty": -2,
                    "buyAvg": 0,
                    "sellAvg": 1500.0,
                    "ltp": 1490.0,
                    "pl": 20.0,
                    "productType": "INTRADAY",
                },
            ],
        }
    )

    result = make_broker(client).exit_position("NSE:SBIN-EQ")

    assert (result["status"], result["message"]) == ("OK", "Exit orders placed")
    assert [r["status"] for r in result["results"]] == ["SUBMITTED"]
    assert client.calls == [
        ("positions", None),
        (
            "place_order",
            {
                "symbol": "NSE:SBIN-EQ",
                "qty": 4,
                "type": 2,
                "side": -1,
                "productType": "CNC",
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            },
        ),
    ]


def test_exit_position_rejects_when_positions_unavailable() -> None:
    client = FakeFyersClient(positions_response={"s": "error", "message": "token expired"})

    result = make_broker(client).exit_position("NSE:SBIN-EQ")

    assert result == {
        "status": "REJECTED",
        "code": "BROKER_UNAVAILABLE",
        "message": "Broker positions unavailable; no exit order was placed",
    }
    assert client.calls == [("positions", None)]
