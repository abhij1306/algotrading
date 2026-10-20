"""
Test fixtures and configuration for pytest
"""

from collections.abc import Generator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.brokers.plugins import fyers as fyers_plugin
from app.database import Base, get_db
from app.main import app
from app.services.live_market_service import LiveMarketService, live_market
from app.services.order_execution_service import order_execution_service
from app.services.position_sync_service import position_sync_service
from app.services.risk_manager import risk_manager
from app.utils import market_hours
from app.utils.clock import FixedClock
from app.utils.market_hours import MarketHoursPolicy

from fakes.broker import FakeBroker
from fakes.market import MARKET_CLOSED_INSTANT, MARKET_OPEN_INSTANT, FreezeMarket

# Test database URL (use in-memory SQLite for tests)
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///./test.db"


@pytest.fixture(scope="session")
def test_engine() -> Generator[Engine, None, None]:
    """Create test database engine"""
    engine = create_engine(
        SQLALCHEMY_TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def freeze_market(monkeypatch: pytest.MonkeyPatch) -> FreezeMarket:
    """The one way to pin market hours in a test.

    `freeze_market(True)` puts "now" mid-session (MARKET_OPEN_INSTANT), `freeze_market(False)`
    on a Saturday (MARKET_CLOSED_INSTANT), and `freeze_market(instant)` at any aware
    datetime (build it with `tzinfo=IST`). DEV_MODE is turned off so it cannot reopen the
    market. It swaps in a `MarketHoursPolicy` on a `FixedClock` for every owner of a policy:
    the risk manager, the `live_market` singleton plus any `LiveMarketService` passed after
    the instant, and the module helpers `is_market_open` / `is_market_hours` /
    `require_market_open`. Returns the clock, so a test can `set` or `advance` it.
    """

    def freeze(at: bool | datetime, *services: LiveMarketService) -> FixedClock:
        if isinstance(at, bool):
            at = MARKET_OPEN_INSTANT if at else MARKET_CLOSED_INSTANT
        clock = FixedClock(at)
        policy = MarketHoursPolicy(clock)
        monkeypatch.setenv("DEV_MODE", "false")
        monkeypatch.setattr(risk_manager, "market_hours", policy)
        for service in (live_market, *services):
            monkeypatch.setattr(service, "market_hours", policy)
            monkeypatch.setattr(service, "dev_mode", False)
        helper = market_hours.is_market_open
        monkeypatch.setattr(market_hours, "is_market_open", lambda policy=policy: helper(policy))
        return clock

    return freeze


@pytest.fixture
def fake_broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    """A `FakeBroker` behind every broker handle the app holds: the order execution, risk
    manager and position sync singletons (which the live exit service shares), and
    `FyersBroker()` constructed by a route. A service a test builds itself needs
    `service._broker = fake_broker`."""
    fake = FakeBroker()
    for owner in (order_execution_service, risk_manager, position_sync_service):
        monkeypatch.setattr(owner, "_broker", fake)
    monkeypatch.setattr(fyers_plugin, "FyersBroker", lambda: fake)
    return fake


@pytest.fixture
def db_session(test_engine: Engine) -> Generator[Session, None, None]:
    """Create a new database session for each test"""
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Create a test client with database override"""

    def override_get_db() -> Generator[Session, None, None]:
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_portfolio_data() -> dict[str, object]:
    """Sample portfolio data for testing"""
    return {
        "name": "Test Portfolio",
        "positions": [
            {
                "symbol": "RELIANCE",
                "quantity": 10,
                "entry_price": 2500.0,
            },
            {
                "symbol": "TCS",
                "quantity": 5,
                "entry_price": 3200.0,
            },
        ],
    }


@pytest.fixture
def sample_strategy_data() -> dict[str, object]:
    """Sample strategy data for testing"""
    return {
        "strategy_id": "TEST_STRATEGY_V1",
        "description": "Test momentum strategy",
        "timeframe": "1D",
        "holding_period": 5,
        "regime": "TREND",
        "lifecycle_status": "RESEARCH",
        "regime_notes": "Fails in sideways markets",
    }
