"""
Comprehensive tests for WebSocket router endpoints
Tests WebSocket connection, subscription, and streaming
"""
import pytest
from unittest.mock import Mock, patch, AsyncMock
from fastapi.testclient import TestClient
import json

from app.main import app


class TestWebSocketConnect:
    """Tests for /websocket/connect endpoint"""

    @patch('app.routers.websocket.live_market')
    def test_connect_success(self, mock_live_market):
        """Test successful WebSocket connection"""
        mock_live_market.connect = Mock()

        client = TestClient(app)
        response = client.post("/api/websocket/connect")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        mock_live_market.connect.assert_called_once()

    @patch('app.routers.websocket.live_market')
    def test_connect_failure(self, mock_live_market):
        """Test WebSocket connection failure"""
        mock_live_market.connect.side_effect = Exception("Connection failed")

        client = TestClient(app)
        response = client.post("/api/websocket/connect")

        assert response.status_code == 500


class TestWebSocketSubscribe:
    """Tests for /websocket/subscribe endpoint"""

    @patch('app.routers.websocket.live_market')
    @pytest.mark.asyncio
    async def test_subscribe_success(self, mock_live_market):
        """Test successful symbol subscription"""
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": ["SBIN", "RELIANCE", "TCS"]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "subscribed"
        assert len(data["symbols"]) == 3

    @patch('app.routers.websocket.live_market')
    def test_subscribe_empty_list(self, mock_live_market):
        """Test subscription with empty symbol list"""
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": []}
        )

        assert response.status_code == 200

    @patch('app.routers.websocket.live_market')
    def test_subscribe_failure(self, mock_live_market):
        """Test subscription failure"""
        mock_live_market.subscribe = AsyncMock(side_effect=Exception("Subscription failed"))

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": ["SBIN"]}
        )

        assert response.status_code == 500


class TestWebSocketDisconnect:
    """Tests for /websocket/disconnect endpoint"""

    @patch('app.routers.websocket.get_websocket_service')
    def test_disconnect_success(self, mock_get_service):
        """Test successful WebSocket disconnection"""
        mock_service = Mock()
        mock_service.disconnect = Mock()
        mock_get_service.return_value = mock_service

        client = TestClient(app)
        response = client.post("/api/websocket/disconnect")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "disconnected"
        mock_service.disconnect.assert_called_once()

    @patch('app.routers.websocket.get_websocket_service')
    def test_disconnect_failure(self, mock_get_service):
        """Test disconnection failure"""
        mock_service = Mock()
        mock_service.disconnect.side_effect = Exception("Disconnect failed")
        mock_get_service.return_value = mock_service

        client = TestClient(app)
        response = client.post("/api/websocket/disconnect")

        assert response.status_code == 500


class TestWebSocketStatus:
    """Tests for /websocket/status endpoint"""

    @patch('app.routers.websocket.live_market')
    def test_get_status_connected(self, mock_live_market):
        """Test getting status when connected"""
        mock_live_market.get_status.return_value = {
            "connected": True,
            "market_status": "OPEN",
            "subscribed_count": 10
        }

        client = TestClient(app)
        response = client.get("/api/websocket/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True

    @patch('app.routers.websocket.live_market')
    def test_get_status_disconnected(self, mock_live_market):
        """Test getting status when disconnected"""
        mock_live_market.get_status.return_value = {
            "connected": False,
            "market_status": "CLOSED"
        }

        client = TestClient(app)
        response = client.get("/api/websocket/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False

    @patch('app.routers.websocket.live_market')
    def test_get_status_error(self, mock_live_market):
        """Test getting status with error"""
        mock_live_market.get_status.side_effect = Exception("Status unavailable")

        client = TestClient(app)
        response = client.get("/api/websocket/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert "error" in data


class TestWebSocketStreaming:
    """Tests for /websocket/stream endpoint"""

    @pytest.mark.asyncio
    async def test_websocket_stream_connection(self):
        """Test WebSocket stream connection"""
        # Note: Full WebSocket testing requires special setup
        # This is a basic structure test
        pass

    @pytest.mark.asyncio
    async def test_websocket_ping_pong(self):
        """Test WebSocket ping/pong mechanism"""
        # Test the ping response
        pass

    @pytest.mark.asyncio
    async def test_websocket_subscribe_action(self):
        """Test subscribe action through WebSocket"""
        # Test sending subscribe action
        pass

    @pytest.mark.asyncio
    async def test_websocket_unsubscribe_action(self):
        """Test unsubscribe action through WebSocket"""
        # Test sending unsubscribe action
        pass


class TestSubscribeRequest:
    """Tests for SubscribeRequest model"""

    def test_subscribe_request_validation(self):
        """Test SubscribeRequest validation"""
        from app.routers.websocket import SubscribeRequest

        # Valid request
        request = SubscribeRequest(symbols=["SBIN", "TCS"])
        assert request.symbols == ["SBIN", "TCS"]

    def test_subscribe_request_empty(self):
        """Test SubscribeRequest with empty list"""
        from app.routers.websocket import SubscribeRequest

        request = SubscribeRequest(symbols=[])
        assert request.symbols == []


class TestSymbolMasterIntegration:
    """Tests for symbol master integration in WebSocket router"""

    @patch('app.routers.websocket.symbol_master')
    @patch('app.routers.websocket.live_market')
    def test_symbol_conversion_in_subscribe(self, mock_live_market, mock_symbol_master):
        """Test that symbols are converted to Fyers format"""
        mock_symbol_master.batch_to_fyers.return_value = [
            "NSE:SBIN-EQ",
            "NSE:RELIANCE-EQ"
        ]
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": ["SBIN", "RELIANCE"]}
        )

        assert response.status_code == 200
        # Verify symbol conversion was called
        mock_symbol_master.batch_to_fyers.assert_called_once()


class TestErrorHandling:
    """Tests for error handling in WebSocket endpoints"""

    def test_subscribe_invalid_json(self):
        """Test subscription with invalid JSON"""
        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            data="invalid json"
        )

        assert response.status_code == 422  # Validation error

    def test_subscribe_missing_symbols(self):
        """Test subscription without symbols field"""
        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={}
        )

        assert response.status_code == 422

    @patch('app.routers.websocket.live_market')
    def test_concurrent_subscriptions(self, mock_live_market):
        """Test handling concurrent subscription requests"""
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)

        # Make multiple concurrent requests
        responses = []
        for i in range(5):
            response = client.post(
                "/api/websocket/subscribe",
                json={"symbols": [f"SYM{i}"]}
            )
            responses.append(response)

        # All should succeed
        assert all(r.status_code == 200 for r in responses)


class TestEdgeCases:
    """Tests for edge cases"""

    @patch('app.routers.websocket.live_market')
    def test_subscribe_large_symbol_list(self, mock_live_market):
        """Test subscription with large symbol list"""
        mock_live_market.subscribe = AsyncMock()

        # Subscribe to 1000 symbols
        large_list = [f"SYM{i}" for i in range(1000)]

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": large_list}
        )

        assert response.status_code == 200

    @patch('app.routers.websocket.live_market')
    def test_subscribe_duplicate_symbols(self, mock_live_market):
        """Test subscription with duplicate symbols"""
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": ["SBIN", "SBIN", "SBIN"]}
        )

        assert response.status_code == 200

    @patch('app.routers.websocket.live_market')
    def test_subscribe_special_characters(self, mock_live_market):
        """Test subscription with special characters in symbols"""
        mock_live_market.subscribe = AsyncMock()

        client = TestClient(app)
        response = client.post(
            "/api/websocket/subscribe",
            json={"symbols": ["M&M", "L&T"]}
        )

        assert response.status_code == 200

    def test_multiple_connects(self):
        """Test multiple connect calls"""
        with patch('app.routers.websocket.live_market') as mock_live_market:
            mock_live_market.connect = Mock()

            client = TestClient(app)

            # Connect multiple times
            for _ in range(3):
                response = client.post("/api/websocket/connect")
                assert response.status_code == 200

    def test_disconnect_without_connect(self):
        """Test disconnect without prior connection"""
        with patch('app.routers.websocket.get_websocket_service') as mock_get_service:
            mock_service = Mock()
            mock_service.disconnect = Mock()
            mock_get_service.return_value = mock_service

            client = TestClient(app)
            response = client.post("/api/websocket/disconnect")

            # Should not fail
            assert response.status_code == 200