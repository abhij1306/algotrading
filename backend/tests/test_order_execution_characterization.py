"""Characterization tests that pin current paper-fill, modify and position-sync
behaviour before those functions are refactored for readability."""

from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.live_order import LiveOrder
from app.models.live_position import LivePosition
from app.services import order_execution_service as oes
from app.services.position_sync_service import PositionSyncService

# --- Paper fill simulation ---------------------------------------------------
# Reference price 100.0, quantity 10, default 5 bps slippage and 3.5 bps fees.


@pytest.mark.parametrize(
    ("side", "order_type", "price", "trigger", "status", "fill", "qty", "charges", "message"),
    [
        ("BUY", "MARKET", 0.0, 0.0, "FILLED", 100.05, 10, 0.35, "Paper simulated market fill"),
        (
            "BUY",
            "LIMIT",
            101.0,
            0.0,
            "FILLED",
            100.05,
            10,
            0.35,
            "Paper simulated marketable limit fill",
        ),
        (
            "BUY",
            "LIMIT",
            99.0,
            0.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper limit resting (not marketable)",
        ),
        (
            "BUY",
            "LIMIT",
            100.0,
            0.0,
            "FILLED",
            100.0,
            10,
            0.35,
            "Paper simulated marketable limit fill",
        ),
        ("BUY", "SL", 101.0, 99.0, "FILLED", 100.05, 10, 0.35, "Paper simulated stop-limit fill"),
        (
            "BUY",
            "SL",
            101.0,
            101.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        (
            "BUY",
            "SL",
            99.0,
            99.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        (
            "BUY",
            "SL",
            99.0,
            101.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        ("BUY", "SL-M", 0.0, 99.0, "FILLED", 100.05, 10, 0.35, "Paper simulated stop-market fill"),
        ("BUY", "SL-M", 0.0, 101.0, "SUBMITTED", 0.0, 0, 0.0, "Paper stop-market pending trigger"),
        ("BUY", "SL-M", 0.0, 100.0, "FILLED", 100.05, 10, 0.35, "Paper simulated stop-market fill"),
        (
            "BUY",
            "ICEBERG",
            0.0,
            0.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper unsupported order type ICEBERG",
        ),
        ("SELL", "MARKET", 0.0, 0.0, "FILLED", 99.95, 10, 0.35, "Paper simulated market fill"),
        (
            "SELL",
            "LIMIT",
            101.0,
            0.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper limit resting (not marketable)",
        ),
        (
            "SELL",
            "LIMIT",
            99.0,
            0.0,
            "FILLED",
            99.95,
            10,
            0.35,
            "Paper simulated marketable limit fill",
        ),
        (
            "SELL",
            "LIMIT",
            100.0,
            0.0,
            "FILLED",
            100.0,
            10,
            0.35,
            "Paper simulated marketable limit fill",
        ),
        (
            "SELL",
            "SL",
            101.0,
            99.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        (
            "SELL",
            "SL",
            101.0,
            101.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        (
            "SELL",
            "SL",
            99.0,
            99.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper stop-limit pending trigger/fill",
        ),
        ("SELL", "SL", 99.0, 101.0, "FILLED", 99.95, 10, 0.35, "Paper simulated stop-limit fill"),
        ("SELL", "SL-M", 0.0, 99.0, "SUBMITTED", 0.0, 0, 0.0, "Paper stop-market pending trigger"),
        ("SELL", "SL-M", 0.0, 101.0, "FILLED", 99.95, 10, 0.35, "Paper simulated stop-market fill"),
        ("SELL", "SL-M", 0.0, 100.0, "FILLED", 99.95, 10, 0.35, "Paper simulated stop-market fill"),
        (
            "SELL",
            "ICEBERG",
            0.0,
            0.0,
            "SUBMITTED",
            0.0,
            0,
            0.0,
            "Paper unsupported order type ICEBERG",
        ),
    ],
)
def test_paper_fill_outcome_per_order_type(
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    order_type: str,
    price: float,
    trigger: float,
    status: str,
    fill: float,
    qty: int,
    charges: float,
    message: str,
) -> None:
    monkeypatch.setattr(oes, "latest_tick_price", lambda _symbol: 100.0)
    params = {
        "side": side,
        "type": order_type,
        "quantity": 10,
        "price": price,
        "trigger_price": trigger,
    }

    result = oes.OrderExecutionService()._simulate_paper_fill(params, "SBIN")

    assert result == {
        "status": status,
        "fill_price": fill,
        "filled_qty": qty,
        "estimated_charges": charges,
        "slippage_bps": 5.0,
        "fill_source": "paper_sim",
        "message": message,
    }


def test_paper_fill_applies_custom_slippage_and_fees(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oes, "latest_tick_price", lambda _symbol: 200.0)
    params = {
        "side": "buy",
        "type": "market",
        "quantity": 3,
        "paper_slippage_bps": 10,
        "paper_fee_bps": 20,
    }

    result = oes.OrderExecutionService()._simulate_paper_fill(params, "SBIN")

    assert result == {
        "status": "FILLED",
        "fill_price": 200.2,
        "filled_qty": 3,
        "estimated_charges": 1.2,
        "slippage_bps": 10.0,
        "fill_source": "paper_sim",
        "message": "Paper simulated market fill",
    }


# --- Modify order ------------------------------------------------------------


class StubModifyBroker:
    """Broker stand-in whose modify_order returns a fixed reply or raises."""

    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((order_id, params))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.query(LiveOrder).delete()
    db_session.commit()


def add_order(db: Session, *, is_paper: int, status: str = "SUBMITTED") -> LiveOrder:
    order = LiveOrder(
        id="ORD-1",
        internal_id="ORD-1",
        user_id="u1",
        symbol="SBIN",
        fyers_symbol="NSE:SBIN-EQ",
        side="BUY",
        quantity=5,
        order_type="LIMIT",
        product_type="INTRADAY",
        price=800.0,
        trigger_price=0.0,
        status=status,
        is_paper=is_paper,
    )
    db.add(order)
    db.commit()
    return order


def test_modify_unknown_order_is_an_error(orders: Session) -> None:
    result = oes.OrderExecutionService().modify_order("NOPE", {"price": 1.0}, orders)

    assert result == {"status": "ERROR", "message": "Order not found"}


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"s": "error", "message": "Order already executed"}, "Order already executed"),
        ({"s": "error"}, "Modify failed"),
        (RuntimeError("broker down"), "broker down"),
    ],
)
def test_live_modify_failure_leaves_order_unchanged(
    orders: Session, reply: dict[str, Any] | Exception, expected: str
) -> None:
    order = add_order(orders, is_paper=0)
    service = oes.OrderExecutionService()
    broker = StubModifyBroker(reply)
    service._broker = broker

    result = service.modify_order("ORD-1", {"price": 801.0}, orders)

    assert result == {"status": "ERROR", "message": expected}
    assert broker.calls == [
        ("ORD-1", {"order_type": "LIMIT", "quantity": 5, "price": 801.0, "trigger_price": 0.0})
    ]
    orders.refresh(order)
    assert order.price == 800.0


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"quantity": "many"}, "Quantity must be a positive integer"),
        ({"quantity": 0}, "Quantity must be a positive integer"),
        ({"price": "cheap"}, "Price must be a positive number"),
        ({"price": -1}, "Price must be a positive number"),
        ({"trigger_price": "x"}, "Trigger price must be a positive number"),
    ],
)
def test_paper_modify_rejects_bad_values(
    orders: Session, params: dict[str, Any], message: str
) -> None:
    add_order(orders, is_paper=1)

    result = oes.OrderExecutionService().modify_order("ORD-1", params, orders)

    assert result == {"status": "ERROR", "message": message}


def test_paper_modify_converts_numeric_strings(orders: Session) -> None:
    order = add_order(orders, is_paper=1)

    result = oes.OrderExecutionService().modify_order(
        "ORD-1", {"quantity": "7", "price": "802.5"}, orders
    )

    assert result == {"status": "SUCCESS", "message": "Paper Order Modified"}
    orders.refresh(order)
    assert (order.quantity, order.price) == (7, 802.5)


# --- Position sync -----------------------------------------------------------


class StubPositionsBroker:
    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self.positions = positions

    def get_positions(self) -> list[dict[str, Any]]:
        return self.positions


@pytest.fixture
def position_factory(
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


def broker_position(symbol: str, side: str, qty: int, entry: float, ltp: float, pnl: float) -> dict:
    return {
        "symbol": symbol,
        "product_type": "INTRADAY",
        "side": side,
        "quantity": qty,
        "entry_price": entry,
        "current_price": ltp,
        "pnl": pnl,
    }


def test_position_sync_updates_creates_and_maps_sides(position_factory: sessionmaker) -> None:
    service = PositionSyncService()
    service._broker = StubPositionsBroker(
        [
            broker_position("NSE:SBIN-EQ", "SELL", 3, 810.0, 805.0, 15.0),
            broker_position("NSE:TCS-EQ", "BUY", 2, 3500.0, 3510.0, 20.0),
            broker_position("NSE:INFY-EQ", "SHORT", 4, 1500.0, 1490.0, 40.0),
            broker_position("NSE:ITC-EQ", "FLAT", 1, 400.0, 401.0, 1.0),
        ]
    )

    result = service.sync_positions()

    assert {k: result[k] for k in ("status", "synced", "updated", "closed")} == {
        "status": "success",
        "synced": 3,
        "updated": 1,
        "closed": 0,
    }
    assert result["total_positions"] == 4
    with position_factory() as db:
        rows = [
            (
                p.id,
                p.symbol,
                p.side,
                p.net_qty,
                p.buy_qty,
                p.sell_qty,
                p.buy_avg,
                p.sell_avg,
                p.net_avg,
                p.ltp,
                p.unrealized_pl,
                p.pl_total,
            )
            for p in db.query(LivePosition).order_by(LivePosition.id).all()
        ]
    assert rows == [
        (
            "default_user-NSE:INFY-EQ-INTRADAY",
            "INFY",
            "SHORT",
            -4,
            0,
            4,
            0.0,
            1500.0,
            1500.0,
            1490.0,
            40.0,
            40.0,
        ),
        (
            "default_user-NSE:ITC-EQ-INTRADAY",
            "ITC",
            "LONG",
            1,
            1,
            0,
            400.0,
            0.0,
            400.0,
            401.0,
            1.0,
            1.0,
        ),
        (
            "default_user-NSE:SBIN-EQ-INTRADAY",
            "SBIN",
            "SHORT",
            -3,
            0,
            0,
            0.0,
            0.0,
            810.0,
            805.0,
            15.0,
            15.0,
        ),
        (
            "default_user-NSE:TCS-EQ-INTRADAY",
            "TCS",
            "LONG",
            2,
            2,
            0,
            3500.0,
            0.0,
            3500.0,
            3510.0,
            20.0,
            20.0,
        ),
    ]
