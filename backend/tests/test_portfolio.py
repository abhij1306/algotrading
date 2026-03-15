from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_read_main() -> None:
    # The health endpoint is at /api/system/health
    response = client.get("/api/system/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data


def test_root_endpoint() -> None:
    # Verify the root health-style endpoint contract.
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert data["status"] == "ok"


def test_portfolio_stats() -> None:
    # Test the portfolio stats endpoint
    response = client.get("/api/portfolio/stats")
    assert response.status_code == 200
    data = response.json()
    assert "totalValue" in data
    assert "dayChange" in data


def test_backtest_strategies_available() -> None:
    # Test the backtest strategies endpoint - returns dict with "strategies" key
    response = client.get("/api/backtest/strategies")
    assert response.status_code == 200
    data = response.json()
    assert "strategies" in data
    assert isinstance(data["strategies"], list)
