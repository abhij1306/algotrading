"""
Test fixtures and configuration for pytest
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, get_db

# Test database URL (use in-memory SQLite for tests)
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///./test.db"

@pytest.fixture(scope="session")
def test_engine():
    """Create test database engine"""
    engine = create_engine(
        SQLALCHEMY_TEST_DATABASE_URL,
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="function")
def db_session(test_engine):
    """Create a new database session for each test"""
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture(scope="function")
def client(db_session):
    """Create a test client with database override"""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass
    
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

@pytest.fixture
def sample_portfolio_data():
    """Sample portfolio data for testing"""
    return {
        "name": "Test Portfolio",
        "positions": [
            {
                "symbol": "RELIANCE",
                "quantity": 10,
                "entry_price": 2500.0
            },
            {
                "symbol": "TCS",
                "quantity": 5,
                "entry_price": 3200.0
            }
        ]
    }

@pytest.fixture
def sample_strategy_data():
    """Sample strategy data for testing"""
    return {
        "strategy_id": "TEST_STRATEGY_V1",
        "description": "Test momentum strategy",
        "timeframe": "1D",
        "holding_period": 5,
        "regime": "TREND",
        "lifecycle_status": "RESEARCH",
        "regime_notes": "Fails in sideways markets"
    }
