
import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_read_main():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "3.0.0"}

def test_market_status():
    response = client.get("/api/market/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_open" in data
    assert "message" in data

def test_live_monitor_endpoint():
    # This endpoint we fixed to return empty list instead of 500
    response = client.get("/api/portfolio/strategies/monitor")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_strategies_available():
    response = client.get("/api/portfolio/strategies/available")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
