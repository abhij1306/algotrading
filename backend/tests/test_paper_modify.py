"""PAPER modify re-runs the fill simulation, PAPER never reaches the broker, and the paper
positions book reports exact (paise-quantized) money."""

from collections.abc import Callable, Generator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models.live_order import LiveOrder
from app.routers import trading
from app.services import order_execution_service as oes
from app.services.risk_manager import risk_manager

CLIENT_ORDER_ID = "5b0e2a77-1c3d-4f8e-9a6b-0d4c2e8f7a15"
REF_PRICE = 810.0
# 810 * (1 + 5 bps) = 810.405 -> 810.41 (paise, half up).
BUY_FILL = 810.41


class FakeBroker:
    """Records every method called on it. A PAPER path must leave `calls` empty."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> Callable[..., dict[str, Any]]:
        def record(*args: object, **kwargs: object) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {"s": "ok", "status": "SUBMITTED", "order_id": "B-1"}

        return record


@pytest.fixture
def orders(db_session: Session) -> Generator[Session, None, None]:
    db_session.query(LiveOrder).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    db_session.query(LiveOrder).delete()
    db_session.commit()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(risk_manager, "_broker", fake)
    return fake


@pytest.fixture
def tick(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """Set the market-data tick the paper simulator reads (0 means no tick)."""

    def set_tick(price: float) -> None:
        monkeypatch.setattr(oes, "latest_tick_price", lambda _symbol: price)

    set_tick(REF_PRICE)
    return set_tick


@pytest.fixture
def service(broker: FakeBroker) -> oes.OrderExecutionService:
    svc = oes.OrderExecutionService()
    svc._broker = broker
    return svc


def add_paper_order(
    db: Session,
    *,
    order_type: str = "LIMIT",
    price: float = 800.0,
    trigger_price: float = 0.0,
    status: str = "SUBMITTED",
) -> LiveOrder:
    order = LiveOrder(
        id="ORD-P1",
        internal_id="ORD-P1",
        user_id="u1",
        symbol="SBIN",
        fyers_symbol="NSE:SBIN-EQ",
        side="BUY",
        quantity=5,
        order_type=order_type,
        product_type="INTRADAY",
        price=price,
        trigger_price=trigger_price,
        status=status,
        filled_qty=0,
        average_price=0.0,
        is_paper=1,
    )
    db.add(order)
    db.commit()
    return order


def row(db: Session, order: LiveOrder) -> tuple[str, str, int, float, float, int, float]:
    db.refresh(order)
    return (
        order.status,
        order.order_type,
        order.quantity,
        order.price,
        order.trigger_price,
        order.filled_qty,
        order.average_price,
    )


def modified(order_status: str, fill_message: str, avg: float, qty: int, charges: float) -> dict:
    return {
        "status": "SUCCESS",
        "message": "Paper Order Modified",
        "order_id": "ORD-P1",
        "order_status": order_status,
        "fill_message": fill_message,
        "average_price": avg,
        "filled_qty": qty,
        "estimated_charges": charges,
    }


# --- Modify re-simulates the fill ---------------------------------------------------


@pytest.mark.usefixtures("tick")
def test_limit_moved_through_the_market_fills(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    order = add_paper_order(orders)

    result = service.modify_order("ORD-P1", {"price": 812.0, "quantity": 7}, orders)

    # 810.41 * 7 * 3.5 bps = 1.9855045 -> 1.99
    assert result == modified("FILLED", "Paper simulated marketable limit fill", BUY_FILL, 7, 1.99)
    assert row(orders, order) == ("FILLED", "LIMIT", 7, 812.0, 0.0, 7, BUY_FILL)
    assert broker.calls == []


@pytest.mark.usefixtures("tick")
def test_limit_moved_but_still_away_from_the_market_keeps_resting(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    order = add_paper_order(orders)

    result = service.modify_order("ORD-P1", {"price": 805.0}, orders)

    assert result == modified("SUBMITTED", "Paper limit resting (not marketable)", 0.0, 0, 0.0)
    assert row(orders, order) == ("SUBMITTED", "LIMIT", 5, 805.0, 0.0, 0, 0.0)
    assert broker.calls == []


@pytest.mark.usefixtures("tick")
def test_stop_limit_modified_past_the_market_triggers_and_fills(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    order = add_paper_order(orders, order_type="SL", price=816.0, trigger_price=815.0)

    result = service.modify_order("ORD-P1", {"trigger_price": 809.0, "price": 811.0}, orders)

    assert result == modified("FILLED", "Paper simulated stop-limit fill", BUY_FILL, 5, 1.42)
    assert row(orders, order) == ("FILLED", "SL", 5, 811.0, 809.0, 5, BUY_FILL)
    assert broker.calls == []


@pytest.mark.usefixtures("tick")
def test_limit_changed_to_stop_market_below_the_market_fills(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker
) -> None:
    order = add_paper_order(orders)

    result = service.modify_order("ORD-P1", {"order_type": "SL-M", "trigger_price": 805.0}, orders)

    assert result == modified("FILLED", "Paper simulated stop-market fill", BUY_FILL, 5, 1.42)
    assert row(orders, order) == ("FILLED", "SL-M", 5, 800.0, 805.0, 5, BUY_FILL)
    assert broker.calls == []


@pytest.mark.usefixtures("tick")
def test_stop_market_modified_but_not_triggered_keeps_resting(
    orders: Session, service: oes.OrderExecutionService
) -> None:
    order = add_paper_order(orders, order_type="SL-M", price=0.0, trigger_price=820.0)

    result = service.modify_order("ORD-P1", {"trigger_price": 815.0}, orders)

    assert result == modified("SUBMITTED", "Paper stop-market pending trigger", 0.0, 0, 0.0)
    assert row(orders, order) == ("SUBMITTED", "SL-M", 5, 0.0, 815.0, 0, 0.0)


def test_modify_without_a_price_to_fill_against_leaves_the_order_unchanged(
    orders: Session,
    service: oes.OrderExecutionService,
    tick: Callable[[float], None],
) -> None:
    order = add_paper_order(orders, order_type="SL-M", price=0.0, trigger_price=820.0)
    tick(0.0)

    result = service.modify_order("ORD-P1", {"trigger_price": 818.0}, orders)

    assert result == {
        "status": "ERROR",
        "message": oes.NO_PAPER_PRICE_MESSAGE.format(symbol="SBIN", order_type="SL-M"),
    }
    assert row(orders, order) == ("SUBMITTED", "SL-M", 5, 0.0, 820.0, 0, 0.0)


@pytest.mark.usefixtures("tick")
def test_modify_to_an_unknown_order_type_is_refused(
    orders: Session, service: oes.OrderExecutionService
) -> None:
    order = add_paper_order(orders)

    result = service.modify_order("ORD-P1", {"order_type": "ICEBERG"}, orders)

    assert result == {
        "status": "ERROR",
        "message": "Invalid order type. Expected MARKET, LIMIT, SL or SL-M.",
    }
    assert row(orders, order) == ("SUBMITTED", "LIMIT", 5, 800.0, 0.0, 0, 0.0)


@pytest.mark.usefixtures("tick")
@pytest.mark.parametrize("status", ["FILLED", "CANCELLED", "REJECTED", "EXPIRED"])
def test_terminal_paper_order_cannot_be_modified(
    orders: Session, service: oes.OrderExecutionService, broker: FakeBroker, status: str
) -> None:
    order = add_paper_order(orders, status=status)

    result = service.modify_order("ORD-P1", {"price": 812.0}, orders)

    assert result == {"status": "ERROR", "message": f"Cannot modify order in {status} state"}
    assert row(orders, order) == (status, "LIMIT", 5, 800.0, 0.0, 0, 0.0)
    assert broker.calls == []


# --- Placement: exact fills, idempotency, no broker -----------------------------------


def paper_params(**overrides: object) -> dict[str, Any]:
    return {
        "symbol": "SBIN",
        "fyers_symbol": "NSE:SBIN-EQ",
        "side": "BUY",
        "quantity": 3,
        "type": "MARKET",
        "product": "INTRADAY",
        "price": 0.0,
        "user_id": "u1",
        "client_order_id": CLIENT_ORDER_ID,
        **overrides,
    }


def test_paper_fill_price_and_charges_are_quantized_half_up(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    tick: Callable[[float], None],
) -> None:
    tick(333.33)

    result = service.place_order(paper_params(), orders, mode="PAPER")

    # 333.33 * 1.0005 = 333.496665 -> 333.50; 333.50 * 3 * 3.5 bps = 0.350175 -> 0.35
    assert (result["status"], result["average_price"], result["estimated_charges"]) == (
        "FILLED",
        333.5,
        0.35,
    )
    assert broker.calls == []


def test_paper_retry_after_a_modify_returns_the_modified_order(
    orders: Session,
    service: oes.OrderExecutionService,
    broker: FakeBroker,
    tick: Callable[[float], None],
) -> None:
    resting = paper_params(type="LIMIT", price=800.0)
    first = service.place_order(resting, orders, mode="PAPER")
    service.modify_order(first["order_id"], {"price": 812.0}, orders)
    retry = service.place_order(resting, orders, mode="PAPER")

    assert first["status"] == "SUBMITTED"
    assert retry == {
        "order_id": first["order_id"],
        "status": "FILLED",
        "message": oes.DUPLICATE_ORDER_MESSAGE,
        "average_price": BUY_FILL,
        "filled_qty": 3,
        "client_order_id": CLIENT_ORDER_ID,
        "duplicate": True,
    }
    assert orders.query(LiveOrder).count() == 1
    assert broker.calls == []


# --- Paper positions book -------------------------------------------------------------


def add_fill(db: Session, n: int, side: str, qty: int, price: float) -> None:
    db.add(
        LiveOrder(
            id=f"F-{n}",
            internal_id=f"F-{n}",
            user_id=trading.SINGLE_USER_ID,
            symbol="PENNY",
            side=side,
            quantity=qty,
            order_type="MARKET",
            product_type="INTRADAY",
            status="FILLED",
            filled_qty=qty,
            average_price=price,
            instrument_type="EQ",
            is_paper=1,
            created_at=datetime(2026, 1, 5, 9, 15) + timedelta(seconds=n),
        )
    )


def test_paper_book_sums_small_fills_without_float_drift(
    orders: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trading.live_market, "get_latest_tick", lambda _symbol: {"ltp": 0.3})
    add_fill(orders, 0, "BUY", 4, 0.1)
    for n in range(1, 4):
        add_fill(orders, n, "SELL", 1, 0.2)
    orders.commit()

    positions = trading._build_paper_positions(orders)

    assert positions == [
        {
            "id": "PAPER-PENNY",
            "symbol": "PENNY",
            "fyers_symbol": None,
            "side": "LONG",
            "quantity": 1,
            "net_qty": 1,
            "entry_price": 0.1,
            "current_price": 0.3,
            "unrealized_pnl": 0.2,
            "realized_pnl": 0.3,
            "net_pnl": 0.5,
            "product_type": "INTRADAY",
            "instrument_type": "EQ",
            "mode": "PAPER",
        }
    ]


def test_paper_book_reversal_reports_the_new_side(
    orders: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trading.live_market, "get_latest_tick", lambda _symbol: None)
    add_fill(orders, 0, "BUY", 10, 100.0)
    add_fill(orders, 1, "SELL", 25, 90.0)
    orders.commit()

    [position] = trading._build_paper_positions(orders)

    assert (
        position["side"],
        position["net_qty"],
        position["entry_price"],
        position["current_price"],
        position["unrealized_pnl"],
        position["realized_pnl"],
        position["net_pnl"],
    ) == ("SHORT", -15, 90.0, 90.0, 0.0, -100.0, -100.0)
