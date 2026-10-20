"""Risk fixes (trading plan 7a, 1e): warnings reach the caller, PAPER orders never use up
LIVE limits, quantities are whole numbers, the daily loss is per user, market hours come
from MarketHoursPolicy, and LIVE positions fail closed when the broker sync fails."""

from collections.abc import Callable, Generator
from datetime import datetime
from decimal import Decimal
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.brokers.base import BrokerUnavailableError
from app.models.live_order import LiveOrder
from app.models.live_position import LivePosition
from app.routers import trading
from app.services.position_sync_service import position_sync_service
from app.services.risk_manager import (
    InvalidQuantityError,
    RiskConfig,
    RiskManager,
    RiskStatus,
    coerce_quantity,
    risk_manager,
)
from app.utils.cache import clear_cache
from app.utils.clock import FixedClock
from app.utils.market_hours import IST, MarketHoursPolicy

CLIENT_ORDER_ID = "5b0e4c2a-7d1f-4e3b-9a6c-2f8d1e0b7c45"
SECRET = "token=abc123 internal broker detail"
LOSS_WARNING = "Approaching daily loss limit. Current P&L: ₹-4500.00"
OPEN_POLICY = MarketHoursPolicy(FixedClock(datetime(2026, 10, 20, 10, 0, tzinfo=IST)))


class FakeBroker:
    """Records every call. LTP 800, funds 1,000,000, every order accepted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.positions: list[dict[str, Any]] | Exception = []
        # None: the quote has no price; an Exception: the quote call fails.
        self.ltp: float | Exception | None = 800.0

    def get_quote(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("get_quote", symbol))
        if isinstance(self.ltp, Exception):
            raise self.ltp
        return {"n": symbol, "v": {"lp": self.ltp}} if self.ltp else {"n": symbol, "v": {}}

    def get_funds(self) -> dict[str, float]:
        self.calls.append(("get_funds", None))
        return {"available": 1_000_000.0, "used": 0.0, "total": 1_000_000.0}

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("place_order", order))
        return {"status": "SUBMITTED", "order_id": "B-1"}

    def get_positions(self) -> list[dict[str, Any]]:
        self.calls.append(("get_positions", None))
        if isinstance(self.positions, Exception):
            raise self.positions
        return self.positions

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)


@pytest.fixture
def rows(db_session: Session) -> Generator[Session, None, None]:
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()
    yield db_session
    db_session.rollback()
    for model in (LiveOrder, LivePosition):
        db_session.query(model).delete()
    db_session.commit()


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(risk_manager, "_broker", fake)
    monkeypatch.setattr(trading.order_execution_service, "_broker", fake)
    return fake


def manager_for(
    broker: FakeBroker,
    config: RiskConfig | None = None,
    market_hours: MarketHoursPolicy = OPEN_POLICY,
) -> RiskManager:
    manager = RiskManager(config, market_hours=market_hours)
    manager._broker = broker  # type: ignore[assignment]
    return manager


@pytest.fixture
def summary_db(monkeypatch: pytest.MonkeyPatch, test_engine: Engine) -> None:
    """get_exposure_summary opens its own session: point it at the test database."""
    monkeypatch.setattr("app.services.risk_manager.SessionLocal", sessionmaker(bind=test_engine))


def add_position(
    db: Session, user_id: str, *, realized: float = 0.0, symbol: str = "INFY", net_qty: int = 0
) -> None:
    db.add(
        LivePosition(
            id=f"{user_id}-NSE:{symbol}-EQ-INTRADAY",
            user_id=user_id,
            symbol=symbol,
            fyers_symbol=f"NSE:{symbol}-EQ",
            product_type="INTRADAY",
            side="LONG",
            net_qty=net_qty,
            ltp=800.0,
            net_avg=790.0,
            realized_pl=realized,
            last_synced_at=datetime.now(),
        )
    )
    db.commit()


def add_order(db: Session, n: int, is_paper: int | None) -> None:
    db.add(
        LiveOrder(
            id=f"ORD-{n}",
            internal_id=f"ORD-{n}",
            user_id="u1",
            symbol="SBIN",
            side="BUY",
            quantity=1,
            order_type="LIMIT",
            product_type="INTRADAY",
            status="FILLED",
            is_paper=is_paper,
        )
    )


def live_order(**overrides: object) -> dict[str, Any]:
    return {
        "mode": "LIVE",
        "symbol": "SBIN",
        "side": "BUY",
        "quantity": 2,
        "product": "INTRADAY",
        "type": "LIMIT",
        "price": 800.0,
        "is_live_confirmation_ack": True,
        "client_order_id": CLIENT_ORDER_ID,
        **overrides,
    }


def risk_params(**overrides: object) -> dict[str, Any]:
    return {
        "symbol": "SBIN",
        "side": "BUY",
        "quantity": 2,
        "type": "LIMIT",
        "price": 800.0,
        "product": "INTRADAY",
        **overrides,
    }


# --- Warnings reach the API response -----------------------------------------------


@pytest.mark.parametrize(
    ("realized", "override", "status_code", "expected"),
    [
        # 90% of the 5000 limit lost: the warning needs an override reason.
        (
            -4500.0,
            None,
            400,
            {"detail": f"Risk warning requires override reason: {LOSS_WARNING}"},
        ),
        (
            -4500.0,
            "hedging an open short",
            200,
            {
                "order_id": "B-1",
                "status": "SUBMITTED",
                "message": "Submitted",
                "client_order_id": CLIENT_ORDER_ID,
                "duplicate": False,
                "warnings": [LOSS_WARNING],
            },
        ),
        (
            -1000.0,
            None,
            200,
            {
                "order_id": "B-1",
                "status": "SUBMITTED",
                "message": "Submitted",
                "client_order_id": CLIENT_ORDER_ID,
                "duplicate": False,
            },
        ),
    ],
)
def test_live_order_warning_reaches_the_api_response(
    client: TestClient,
    rows: Session,
    broker: FakeBroker,
    freeze_market: Callable[[bool], None],
    realized: float,
    override: str | None,
    status_code: int,
    expected: dict[str, Any],
) -> None:
    freeze_market(True)
    add_position(rows, "default_user", realized=realized)

    response = client.post(
        "/api/trading/order",
        headers={"x-user-id": "u1"},
        json=live_order(risk_override_reason=override),
    )

    assert (response.status_code, response.json()) == (status_code, expected)
    assert broker.count("place_order") == (1 if status_code == 200 else 0)


@pytest.mark.parametrize(
    ("realized", "can_trade", "status", "code"),
    [
        (-4500.0, True, "WARNING", "DAILY_LOSS_WARNING"),
        (-6000.0, False, "FAIL", "DAILY_LOSS_LIMIT"),
        (0.0, True, "PASS", "RISK_PASS"),
    ],
)
def test_risk_check_route_reports_a_warning_as_tradeable(
    client: TestClient,
    rows: Session,
    broker: FakeBroker,
    freeze_market: Callable[[bool], None],
    realized: float,
    can_trade: bool,
    status: str,
    code: str,
) -> None:
    freeze_market(True)
    add_position(rows, "default_user", realized=realized)

    response = client.post("/api/trading/risk-check", json=risk_params())

    body = response.json()
    assert (body["can_trade"], body["status"], body["code"]) == (can_trade, status, code)


def test_pre_trade_check_lists_every_warning(rows: Session, broker: FakeBroker) -> None:
    add_position(rows, "default_user", realized=-4500.0)
    manager = manager_for(broker)

    result = manager.pre_trade_check(risk_params(), rows, user_id="default_user")

    assert (result.status, result.code, result.message) == (
        RiskStatus.WARNING,
        "DAILY_LOSS_WARNING",
        LOSS_WARNING,
    )
    assert result.details == {"current_pnl": -4500.0, "limit": -5000.0, "warnings": [LOSS_WARNING]}


# --- No assumed price: an order risk cannot price is blocked --------------------------


@pytest.mark.parametrize(
    ("order_type", "price", "quote", "status_code", "expected", "placed"),
    [
        # MARKET with no usable quote: blocked before the broker, whatever the env says.
        (
            "MARKET",
            0.0,
            None,
            400,
            {
                "detail": "Risk Check Failed: Cannot estimate price for SBIN. "
                "Please provide limit price."
            },
            0,
        ),
        (
            "MARKET",
            0.0,
            RuntimeError(SECRET),
            400,
            {
                "detail": "Risk Check Failed: Cannot estimate price for SBIN. "
                "Please provide limit price."
            },
            0,
        ),
        # LIMIT with no quote: valued at its own limit price, so it goes through.
        (
            "LIMIT",
            800.0,
            None,
            200,
            {
                "order_id": "B-1",
                "status": "SUBMITTED",
                "message": "Submitted",
                "client_order_id": CLIENT_ORDER_ID,
                "duplicate": False,
            },
            1,
        ),
    ],
)
def test_live_order_risk_never_assumes_a_price(
    client: TestClient,
    rows: Session,
    broker: FakeBroker,
    freeze_market: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
    order_type: str,
    price: float,
    quote: Exception | None,
    status_code: int,
    expected: dict[str, Any],
    placed: int,
) -> None:
    freeze_market(True)
    # The removed fallback env var must have no effect.
    monkeypatch.setenv("RISK_DEFAULT_PRICE", "900")
    broker.ltp = quote

    response = client.post(
        "/api/trading/order",
        headers={"x-user-id": "u1"},
        json=live_order(type=order_type, price=price),
    )

    assert (response.status_code, response.json()) == (status_code, expected)
    assert broker.count("place_order") == placed
    assert SECRET not in response.text


def test_margin_check_fails_closed_on_an_unpriced_order(broker: FakeBroker) -> None:
    broker.ltp = None
    manager = manager_for(broker)

    result = manager._check_available_margin(risk_params(type="MARKET", price=0))

    assert (result.status, result.code) == (RiskStatus.FAIL, "PRICE_ESTIMATE_FAILED")


# --- PAPER orders never use up LIVE limits -----------------------------------------


@pytest.mark.parametrize(
    ("paper", "live", "legacy", "code"),
    [
        (5, 0, 0, "RISK_PASS"),
        (5, 1, 0, "RISK_PASS"),
        (0, 2, 0, "ORDER_FREQUENCY_LIMIT"),
        # A row with no mode flag is counted as LIVE: fail safe.
        (0, 1, 1, "ORDER_FREQUENCY_LIMIT"),
    ],
)
@pytest.mark.usefixtures("summary_db")
def test_only_live_orders_count_toward_the_daily_order_limit(
    rows: Session, broker: FakeBroker, paper: int, live: int, legacy: int, code: str
) -> None:
    flags = [1] * paper + [0] * live + [None] * legacy
    for n, flag in enumerate(flags):
        add_order(rows, n, flag)
    rows.commit()
    manager = manager_for(broker, RiskConfig(max_orders_per_day=2))

    result = manager.pre_trade_check(risk_params(), rows, user_id="default_user")

    assert result.code == code
    assert manager.get_exposure_summary("default_user")["orders_today"] == live + legacy


# --- Quantity is a positive whole number -------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), (5.0, 5), ("7", 7), (" 7 ", 7), (Decimal("3"), 3), (Decimal("3.0"), 3)],
)
def test_coerce_quantity_accepts_whole_numbers(value: object, expected: int) -> None:
    assert coerce_quantity(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        2.5,
        2.9,
        "2.5",
        "x",
        "",
        None,
        True,
        False,
        float("nan"),
        float("inf"),
        Decimal("1.5"),
        Decimal("NaN"),
        [3],
    ],
)
def test_coerce_quantity_rejects_anything_else(value: object) -> None:
    with pytest.raises(InvalidQuantityError, match="^Quantity must be a positive integer$"):
        coerce_quantity(value)


@pytest.mark.parametrize(
    ("quantity", "code"),
    [
        (2.5, "INVALID_QUANTITY"),
        ("abc", "INVALID_QUANTITY"),
        (0, "INVALID_QUANTITY"),
        ("1001", "POSITION_SIZE_LIMIT"),
        ("3", "RISK_PASS"),
    ],
)
def test_pre_trade_check_coerces_quantity(
    rows: Session, broker: FakeBroker, quantity: object, code: str
) -> None:
    manager = manager_for(broker)

    result = manager.pre_trade_check(risk_params(quantity=quantity), rows, user_id="default_user")

    assert result.code == code


@pytest.mark.parametrize(
    ("new_qty", "old_qty", "code"),
    [(2.5, 2, "INVALID_QUANTITY"), (3, "two", "INVALID_QUANTITY"), ("3", 2, "RISK_PASS")],
)
def test_pre_modify_check_coerces_quantity(
    rows: Session, broker: FakeBroker, new_qty: object, old_qty: object, code: str
) -> None:
    manager = manager_for(broker)

    result = manager.pre_modify_check(
        risk_params(quantity=new_qty),
        {"quantity": old_qty, "type": "LIMIT", "price": 800.0},
        800.0,
        rows,
        user_id="default_user",
    )

    assert result.code == code


# --- Daily loss is per user ---------------------------------------------------------


@pytest.mark.parametrize(
    ("losses", "code"),
    [
        ({"someone_else": -6000.0}, "RISK_PASS"),
        ({"default_user": -6000.0}, "DAILY_LOSS_LIMIT"),
        # Summed across users this was -6000 and blocked the order.
        ({"default_user": -2000.0, "someone_else": -4000.0}, "RISK_PASS"),
        ({"default_user": -4500.0, "someone_else": -4000.0}, "DAILY_LOSS_WARNING"),
    ],
)
def test_daily_loss_is_checked_for_the_given_user_only(
    rows: Session, broker: FakeBroker, losses: dict[str, float], code: str
) -> None:
    for user_id, realized in losses.items():
        add_position(rows, user_id, realized=realized)
    manager = manager_for(broker)

    result = manager.pre_trade_check(risk_params(), rows, user_id="default_user")

    assert result.code == code


@pytest.mark.usefixtures("summary_db")
def test_exposure_summary_today_pnl_is_per_user(rows: Session) -> None:
    add_position(rows, "default_user", realized=-100.0)
    add_position(rows, "someone_else", realized=-900.0)

    summary = RiskManager().get_exposure_summary("default_user")

    assert summary["today_pnl"] == -100.0


# --- Market hours come from MarketHoursPolicy --------------------------------------


@pytest.mark.parametrize(
    ("instant", "code"),
    [
        (datetime(2026, 10, 20, 9, 14, 59, tzinfo=IST), "MARKET_CLOSED"),
        (datetime(2026, 10, 20, 9, 15, tzinfo=IST), "RISK_PASS"),
        (datetime(2026, 10, 20, 15, 30, tzinfo=IST), "RISK_PASS"),
        (datetime(2026, 10, 20, 15, 30, 1, tzinfo=IST), "MARKET_CLOSED"),
        (datetime(2026, 10, 24, 11, 0, tzinfo=IST), "MARKET_CLOSED"),  # Saturday
    ],
)
def test_risk_market_hours_follow_the_policy(
    rows: Session,
    broker: FakeBroker,
    monkeypatch: pytest.MonkeyPatch,
    instant: datetime,
    code: str,
) -> None:
    monkeypatch.setenv("DEV_MODE", "false")
    manager = manager_for(broker, market_hours=MarketHoursPolicy(FixedClock(instant)))

    result = manager.pre_trade_check(risk_params(), rows, user_id="default_user")

    assert result.code == code
    if code == "MARKET_CLOSED":
        assert result.message == (
            "Market is closed. Orders can only be placed during market hours (09:15 - 15:30 IST)."
        )


# --- LIVE positions fail closed when the broker sync fails -------------------------


@pytest.fixture
def synced(
    monkeypatch: pytest.MonkeyPatch, test_engine: Engine, rows: Session
) -> Generator[FakeBroker, None, None]:
    """position_sync_service on the test database with a FakeBroker, and one stored
    LIVE position from an earlier sync."""
    fake = FakeBroker()
    monkeypatch.setattr(position_sync_service, "_broker", fake)
    monkeypatch.setattr(
        "app.services.position_sync_service.SessionLocal", sessionmaker(bind=test_engine)
    )
    monkeypatch.setattr(position_sync_service, "_last_sync", datetime(2026, 10, 20, 10, 0))
    add_position(rows, "default_user", symbol="SBIN", net_qty=10)
    clear_cache()
    yield fake
    clear_cache()


def broker_position(net_qty: int) -> dict[str, Any]:
    return {
        "symbol": "NSE:SBIN-EQ",
        "side": "LONG",
        "quantity": net_qty,
        "entry_price": 790.0,
        "current_price": 805.0,
        "pnl": 150.0,
        "product_type": "INTRADAY",
    }


@pytest.mark.parametrize(
    "failure",
    [BrokerUnavailableError(SECRET), RuntimeError(SECRET)],
)
def test_live_positions_answer_503_when_the_sync_fails(
    client: TestClient, synced: FakeBroker, failure: Exception
) -> None:
    synced.positions = failure

    response = client.get("/api/trading/positions")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "BROKER_UNAVAILABLE",
            "message": "Live positions could not be refreshed from the broker",
        }
    }
    assert SECRET not in response.text


def test_live_positions_are_served_after_a_successful_sync(
    client: TestClient, synced: FakeBroker
) -> None:
    synced.positions = [broker_position(12)]

    response = client.get("/api/trading/positions")

    assert response.status_code == 200
    body = response.json()
    assert [(p["symbol"], p["net_qty"], p["current_price"]) for p in body] == [("SBIN", 12, 805.0)]


def test_positions_book_marks_live_rows_stale_when_the_sync_fails(
    client: TestClient, synced: FakeBroker
) -> None:
    synced.positions = BrokerUnavailableError(SECRET)

    response = client.get("/api/trading/positions/book")

    assert response.status_code == 200
    body = response.json()
    assert (body["live_stale"], body["live_as_of"]) == (True, "2026-10-20T10:00:00")
    assert [(p["symbol"], p["net_qty"], p["stale"]) for p in body["live_positions"]] == [
        ("SBIN", 10, True)
    ]
    assert body["paper_positions"] == []
    assert SECRET not in response.text


def test_positions_book_is_fresh_after_a_successful_sync(
    client: TestClient, synced: FakeBroker
) -> None:
    synced.positions = [broker_position(12)]

    body = client.get("/api/trading/positions/book").json()

    assert body["live_stale"] is False
    assert [(p["symbol"], p["net_qty"], p["stale"]) for p in body["live_positions"]] == [
        ("SBIN", 12, False)
    ]


class DownSession:
    """A session whose database has gone away."""

    def query(self, *_args: object) -> NoReturn:
        raise OperationalError("SELECT 1", {}, Exception(f"connection refused {SECRET}"))

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.parametrize("path", ["/api/trading/positions", "/api/trading/positions/book"])
def test_a_down_database_during_sync_is_reported_as_a_database_outage(
    client: TestClient, synced: FakeBroker, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    synced.positions = [broker_position(12)]
    monkeypatch.setattr("app.services.position_sync_service.SessionLocal", DownSession)

    response = client.get(path)

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "DATABASE_UNAVAILABLE", "message": "Database unavailable"}
    }
    assert SECRET not in response.text


def test_a_successful_sync_is_never_hidden_by_the_positions_cache(
    client: TestClient, synced: FakeBroker
) -> None:
    synced.positions = [broker_position(12)]
    first = client.get("/api/trading/positions/book").json()
    synced.positions = [broker_position(15)]

    second = client.get("/api/trading/positions/book").json()

    assert [p["net_qty"] for p in first["live_positions"]] == [12]
    assert [p["net_qty"] for p in second["live_positions"]] == [15]
