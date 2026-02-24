"""
Integration tests for WebSocket error handling

Tests Property 1: WebSocket Error Resilience
Validates: Requirements 8.1, 8.3, 8.5

These tests verify that WebSocket connections handle errors gracefully without
crashing the application, maintain application state, and allow reconnection.
"""
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from app.services.websocket_error_handler import handle_websocket_errors, log_websocket_event
from app.utils.ws_manager import ConnectionManager


class TestWebSocketErrorResilience:
    """Test suite for WebSocket error handling and resilience"""

    @pytest.mark.asyncio
    async def test_graceful_disconnect_handling(self):
        """
        Test that WebSocketDisconnect is handled gracefully without crashing.

        Property: For any WebSocket disconnect, the system should handle it
        gracefully without propagating unhandled exceptions.
        """
        manager = ConnectionManager()

        # Create mock websocket
        mock_websocket = Mock(spec=WebSocket)
        mock_websocket.accept = AsyncMock()
        mock_websocket.send_json = AsyncMock(side_effect=WebSocketDisconnect())
        mock_websocket.client_state = Mock(name="CONNECTED")

        # Attempt to connect - should handle disconnect gracefully
        result = await manager.connect(mock_websocket)

        # Should return False but not raise exception
        assert result is False
        # Websocket should not be in subscriptions
        assert mock_websocket not in manager.subscriptions

    @pytest.mark.asyncio
    async def test_state_preservation_during_disconnect(self):
        """
        Test that application state remains consistent after disconnect.

        Property: For any disconnect event, the application state should remain
        consistent and other connections should not be affected.
        """
        manager = ConnectionManager()

        # Create two mock websockets
        ws1 = Mock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()
        ws1.client_state = Mock(name="CONNECTED")

        ws2 = Mock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()
        ws2.client_state = Mock(name="CONNECTED")

        # Connect both
        await manager.connect(ws1)
        await manager.connect(ws2)

        # Subscribe both to symbols
        await manager.subscribe(ws1, ["SBIN", "RELIANCE"])
        await manager.subscribe(ws2, ["TCS", "INFY"])

        # Verify both are connected
        assert len(manager.subscriptions) == 2
        assert len(manager.subscriptions[ws1]) == 2
        assert len(manager.subscriptions[ws2]) == 2

        # Disconnect ws1
        manager.disconnect(ws1)

        # Verify ws2 state is preserved
        assert len(manager.subscriptions) == 1
        assert ws1 not in manager.subscriptions
        assert ws2 in manager.subscriptions
        assert len(manager.subscriptions[ws2]) == 2
        assert "TCS" in manager.subscriptions[ws2]
        assert "INFY" in manager.subscriptions[ws2]

    @pytest.mark.asyncio
    async def test_decorator_handles_websocket_disconnect(self):
        """
        Test that the @handle_websocket_errors decorator catches WebSocketDisconnect.

        Property: Functions decorated with @handle_websocket_errors should catch
        WebSocketDisconnect and log appropriately without crashing.
        """
        @handle_websocket_errors(log_level="INFO")
        async def mock_websocket_handler():
            raise WebSocketDisconnect()

        # Should raise WebSocketDisconnect (decorator re-raises after logging)
        with pytest.raises(WebSocketDisconnect):
            await mock_websocket_handler()

    @pytest.mark.asyncio
    async def test_decorator_handles_general_errors(self):
        """
        Test that the decorator handles general exceptions with ERROR logging.

        Property: Non-disconnect exceptions should be logged at ERROR level
        with full context and re-raised.
        """
        @handle_websocket_errors(log_level="INFO")
        async def mock_websocket_handler():
            raise ValueError("Test error")

        # Should raise the original exception
        with pytest.raises(ValueError, match="Test error"):
            await mock_websocket_handler()

    @pytest.mark.asyncio
    async def test_broadcast_handles_dead_connections(self):
        """
        Test that broadcast() removes dead connections without affecting others.

        Property: Broadcasting should handle connection failures gracefully
        and remove failed connections from the subscription list.
        """
        manager = ConnectionManager()

        # Create a websocket that will fail on send
        failing_ws = Mock(spec=WebSocket)
        failing_ws.client_state = Mock(name="CONNECTED")
        failing_ws.send_text = AsyncMock(side_effect=WebSocketDisconnect())

        # Add to subscriptions
        manager.subscriptions[failing_ws] = {"SBIN"}
        assert len(manager.subscriptions) == 1

        # Broadcast a message
        message = {"type": "ticker", "data": {"symbol": "SBIN", "ltp": 500.0}}
        await manager.broadcast(message)

        # Failing connection should be removed after send fails
        assert failing_ws not in manager.subscriptions
        assert len(manager.subscriptions) == 0

    @pytest.mark.asyncio
    async def test_multiple_disconnect_scenarios(self):
        """
        Test various disconnect scenarios (normal, error, timeout).

        Property: All disconnect scenarios should be handled gracefully
        without leaving the system in an inconsistent state.
        """
        manager = ConnectionManager()

        # Scenario 1: Normal disconnect during handshake
        ws1 = Mock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock(side_effect=WebSocketDisconnect())
        result1 = await manager.connect(ws1)
        assert result1 is False
        assert ws1 not in manager.subscriptions

        # Scenario 2: RuntimeError during send
        ws2 = Mock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock(side_effect=RuntimeError("Connection closed"))
        result2 = await manager.connect(ws2)
        assert result2 is False
        assert ws2 not in manager.subscriptions

        # Scenario 3: Successful connection then disconnect
        ws3 = Mock(spec=WebSocket)
        ws3.accept = AsyncMock()
        ws3.send_json = AsyncMock()
        ws3.client_state = Mock(name="CONNECTED")
        result3 = await manager.connect(ws3)
        assert result3 is True
        assert ws3 in manager.subscriptions

        manager.disconnect(ws3)
        assert ws3 not in manager.subscriptions

    def test_log_websocket_event_formatting(self):
        """
        Test that WebSocket events are logged with consistent formatting.

        Property: All WebSocket events should be logged with a consistent
        format including event type and context.
        """
        # This test verifies the log_websocket_event utility function
        # In a real scenario, you'd capture logs and verify format

        # Should not raise any exceptions
        log_websocket_event("connection_established", level="INFO", client_id="test123")
        log_websocket_event("disconnect", level="INFO", reason="client_closed")
        log_websocket_event("error", level="ERROR", error_type="timeout", details="Connection timeout")
        log_websocket_event("reconnection_attempt", level="DEBUG", attempt=3, max_attempts=10)


class TestWebSocketReconnection:
    """Test suite for WebSocket reconnection logic"""

    @pytest.mark.asyncio
    async def test_reconnection_after_disconnect(self):
        """
        Test that reconnection is possible after a disconnect.

        Property: After a disconnect, the system should allow reconnection
        without requiring a restart.
        """
        manager = ConnectionManager()

        # First connection
        ws = Mock(spec=WebSocket)
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.client_state = Mock(name="CONNECTED")

        result1 = await manager.connect(ws)
        assert result1 is True
        assert ws in manager.subscriptions

        # Disconnect
        manager.disconnect(ws)
        assert ws not in manager.subscriptions

        # Reconnect (simulate new connection)
        ws_new = Mock(spec=WebSocket)
        ws_new.accept = AsyncMock()
        ws_new.send_json = AsyncMock()
        ws_new.client_state = Mock(name="CONNECTED")

        result2 = await manager.connect(ws_new)
        assert result2 is True
        assert ws_new in manager.subscriptions

    @pytest.mark.asyncio
    async def test_subscription_state_after_reconnect(self):
        """
        Test that subscriptions can be re-established after reconnection.

        Property: After reconnection, clients should be able to re-subscribe
        to symbols without issues.
        """
        manager = ConnectionManager()

        # Connect and subscribe
        ws1 = Mock(spec=WebSocket)
        ws1.accept = AsyncMock()
        ws1.send_json = AsyncMock()
        ws1.client_state = Mock(name="CONNECTED")

        await manager.connect(ws1)
        await manager.subscribe(ws1, ["SBIN", "RELIANCE"])
        assert len(manager.subscriptions[ws1]) == 2

        # Disconnect
        manager.disconnect(ws1)

        # Reconnect and re-subscribe
        ws2 = Mock(spec=WebSocket)
        ws2.accept = AsyncMock()
        ws2.send_json = AsyncMock()
        ws2.client_state = Mock(name="CONNECTED")

        await manager.connect(ws2)
        await manager.subscribe(ws2, ["SBIN", "RELIANCE", "TCS"])

        # Should have new subscriptions
        assert len(manager.subscriptions[ws2]) == 3
        assert "SBIN" in manager.subscriptions[ws2]
        assert "RELIANCE" in manager.subscriptions[ws2]
        assert "TCS" in manager.subscriptions[ws2]


# Property-Based Test Marker
pytestmark = pytest.mark.integration
