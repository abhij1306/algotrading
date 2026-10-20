"""Regression tests for live-trading defects: quote parsing in risk checks,
stop-loss order mapping, and position sync during a broker outage."""

from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.brokers.plugins.fyers import FyersBroker
from app.models.live_order import LiveOrder
from app.models.live_position import LivePosition
from app.services.order_execution_service import OrderExecutionService
from app.services.position_sync_service import PositionSyncService
from app.services.risk_manager import RiskConfig, RiskManager

# A quote as FyersBroker.get_quote() returns it: one entry of the Fyers "d" list.
SBIN_QUOTE = {"n": "NSE:SBIN-EQ", "s": "ok", "v": {"lp": 812.5, "bid": 812.4, "ask": 812.6}}


class FakeFyersClient:
    """Stands in for the fyers-apiv3 client and records every call."""

    def __init__(
        self,
        positions_response: dict[str, Any] | None = None,
        quotes_response: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.positions_response = positions_response or {"s": "ok", "netPositions": []}
        self.quotes_response = quotes_response or {"s": "ok", "d": [SBIN_QUOTE]}

    def place_order(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("place_order", data))
        return {"s": "ok", "id": "FY-1", "message": "Order placed"}

    def positions(self) -> dict[str, Any]:
        self.calls.append(("positions", None))
        return self.positions_response

    def quotes(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("quotes", data))
        return self.quotes_response


@pytest.fixture(autouse=True)
def reset_circuit_breaker() -> Generator[None, None, None]:
    FyersBroker._record_success()
    yield
    FyersBroker._record_success()


def make_broker(client: FakeFyersClient | None) -> FyersBroker:
    broker = FyersBroker.__new__(FyersBroker)
    broker.client = client
    broker.connect = lambda: None  # never reach the real Fyers login
    return broker


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


def test_unknown_order_type_is_rejected_before_broker() -> None:
    client = FakeFyersClient()

    response = make_broker(client).place_order(
        {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1, "type": "STOP", "price": 1}
    )

    assert response["status"] == "REJECTED"
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
