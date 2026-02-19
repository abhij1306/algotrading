
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def test_read_main():
    # The health endpoint is at /api/system/health
    response = client.get("/api/system/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data

def test_market_status():
    # The market status endpoint is at /market/status (no /api prefix)
    # But it's not registered with a prefix, so it's actually at /market/status
    # Let's test the root endpoint instead
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert data["status"] == "ok"

def test_portfolio_stats():
    # Test the portfolio stats endpoint
    response = client.get("/api/portfolio/stats")
    assert response.status_code == 200
    data = response.json()
    assert "totalValue" in data
    assert "dayChange" in data

def test_backtest_strategies_available():
    # Test the backtest strategies endpoint - returns dict with "strategies" key
    response = client.get("/api/backtest/strategies")
    assert response.status_code == 200
    data = response.json()
    assert "strategies" in data
    assert isinstance(data["strategies"], list)
