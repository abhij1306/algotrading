"""
Comprehensive tests for WebSocket manager
Tests connection management and broadcasting
"""
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
import json

from app.utils.ws_manager import ConnectionManager, manager


class TestConnectionManager:
    """Tests for ConnectionManager class"""

    def test_manager_initialization(self):
        """Test manager is properly initialized"""
        assert manager is not None
        assert isinstance(manager, ConnectionManager)
        assert manager.active_connections == []

    def test_set_loop(self):
        """Test setting event loop"""
        test_manager = ConnectionManager()
        loop = asyncio.new_event_loop()

        test_manager.set_loop(loop)

        assert test_manager.loop == loop

    @pytest.mark.asyncio
    async def test_connect_websocket(self):
        """Test connecting a WebSocket client"""
        test_manager = ConnectionManager()
        mock_websocket = AsyncMock()

        await test_manager.connect(mock_websocket)

        assert mock_websocket in test_manager.active_connections
        mock_websocket.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_multiple_clients(self):
        """Test connecting multiple clients"""
        test_manager = ConnectionManager()
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws3 = AsyncMock()

        await test_manager.connect(mock_ws1)
        await test_manager.connect(mock_ws2)
        await test_manager.connect(mock_ws3)

        assert len(test_manager.active_connections) == 3
        assert mock_ws1 in test_manager.active_connections
        assert mock_ws2 in test_manager.active_connections
        assert mock_ws3 in test_manager.active_connections

    def test_disconnect_websocket(self):
        """Test disconnecting a WebSocket client"""
        test_manager = ConnectionManager()
        mock_websocket = Mock()
        test_manager.active_connections.append(mock_websocket)

        test_manager.disconnect(mock_websocket)

        assert mock_websocket not in test_manager.active_connections

    def test_disconnect_non_existent(self):
        """Test disconnecting non-existent connection"""
        test_manager = ConnectionManager()
        mock_websocket = Mock()

        # Should not raise error
        test_manager.disconnect(mock_websocket)

        assert len(test_manager.active_connections) == 0

    @pytest.mark.asyncio
    async def test_broadcast_to_all_clients(self):
        """Test broadcasting message to all connected clients"""
        test_manager = ConnectionManager()

        # Create mock clients
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        test_manager.active_connections = [mock_ws1, mock_ws2]

        # Broadcast message
        message = {"type": "ticker", "data": {"symbol": "SBIN", "price": 500.0}}
        await test_manager.broadcast(message)

        # Verify both clients received the message
        expected_json = json.dumps(message)
        mock_ws1.send_text.assert_called_once_with(expected_json)
        mock_ws2.send_text.assert_called_once_with(expected_json)

    @pytest.mark.asyncio
    async def test_broadcast_empty_connections(self):
        """Test broadcasting with no connected clients"""
        test_manager = ConnectionManager()
        test_manager.active_connections = []

        message = {"type": "ticker", "data": {}}

        # Should not raise error
        await test_manager.broadcast(message)

    @pytest.mark.asyncio
    async def test_broadcast_with_disconnected_client(self):
        """Test broadcasting when a client disconnects during send"""
        from fastapi import WebSocketDisconnect

        test_manager = ConnectionManager()

        # Create clients, one will disconnect
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws2.send_text.side_effect = WebSocketDisconnect()

        test_manager.active_connections = [mock_ws1, mock_ws2]

        message = {"type": "ticker", "data": {}}
        await test_manager.broadcast(message)

        # First client should receive message
        mock_ws1.send_text.assert_called_once()

        # Disconnected client should be removed
        assert mock_ws2 not in test_manager.active_connections

    @pytest.mark.asyncio
    async def test_broadcast_with_exception(self):
        """Test broadcasting when send raises exception"""
        test_manager = ConnectionManager()

        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws2.send_text.side_effect = Exception("Network error")

        test_manager.active_connections = [mock_ws1, mock_ws2]

        message = {"type": "ticker", "data": {}}
        await test_manager.broadcast(message)

        # First client should receive message
        mock_ws1.send_text.assert_called_once()

        # Failed client should be removed
        assert mock_ws2 not in test_manager.active_connections

    @pytest.mark.asyncio
    async def test_broadcast_json_serialization(self):
        """Test that broadcast properly serializes JSON"""
        test_manager = ConnectionManager()
        mock_ws = AsyncMock()
        test_manager.active_connections = [mock_ws]

        message = {
            "type": "ticker",
            "data": {
                "symbol": "RELIANCE",
                "price": 2500.50,
                "volume": 100000,
                "change_pct": 1.5
            }
        }

        await test_manager.broadcast(message)

        # Verify JSON is properly formatted
        call_args = mock_ws.send_text.call_args[0][0]
        parsed = json.loads(call_args)
        assert parsed["type"] == "ticker"
        assert parsed["data"]["symbol"] == "RELIANCE"
        assert parsed["data"]["price"] == 2500.50

    @pytest.mark.asyncio
    async def test_concurrent_broadcasts(self):
        """Test multiple concurrent broadcasts"""
        test_manager = ConnectionManager()
        mock_ws = AsyncMock()
        test_manager.active_connections = [mock_ws]

        # Broadcast multiple messages concurrently
        messages = [
            {"type": "ticker", "data": {"symbol": f"SYM{i}", "price": float(i)}}
            for i in range(10)
        ]

        await asyncio.gather(*[test_manager.broadcast(msg) for msg in messages])

        # All messages should be sent
        assert mock_ws.send_text.call_count == 10

    @pytest.mark.asyncio
    async def test_connect_disconnect_sequence(self):
        """Test sequence of connects and disconnects"""
        test_manager = ConnectionManager()

        # Connect 3 clients
        clients = [AsyncMock() for _ in range(3)]
        for client in clients:
            await test_manager.connect(client)

        assert len(test_manager.active_connections) == 3

        # Disconnect middle client
        test_manager.disconnect(clients[1])
        assert len(test_manager.active_connections) == 2
        assert clients[1] not in test_manager.active_connections

        # Connect new client
        new_client = AsyncMock()
        await test_manager.connect(new_client)
        assert len(test_manager.active_connections) == 3

        # Disconnect all
        for client in test_manager.active_connections.copy():
            test_manager.disconnect(client)

        assert len(test_manager.active_connections) == 0


class TestGlobalManagerInstance:
    """Tests for global manager singleton"""

    def test_global_manager_exists(self):
        """Test that global manager instance exists"""
        from app.utils.ws_manager import manager

        assert manager is not None
        assert isinstance(manager, ConnectionManager)

    def test_global_manager_persistence(self):
        """Test that global manager persists across imports"""
        from app.utils.ws_manager import manager as manager1
        from app.utils.ws_manager import manager as manager2

        assert manager1 is manager2


class TestEdgeCases:
    """Tests for edge cases"""

    @pytest.mark.asyncio
    async def test_broadcast_large_message(self):
        """Test broadcasting large message"""
        test_manager = ConnectionManager()
        mock_ws = AsyncMock()
        test_manager.active_connections = [mock_ws]

        # Create large message
        large_data = {
            "type": "bulk_update",
            "data": [
                {"symbol": f"SYM{i}", "price": float(i)}
                for i in range(1000)
            ]
        }

        await test_manager.broadcast(large_data)

        # Should handle without issue
        mock_ws.send_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_special_characters(self):
        """Test broadcasting message with special characters"""
        test_manager = ConnectionManager()
        mock_ws = AsyncMock()
        test_manager.active_connections = [mock_ws]

        message = {
            "type": "ticker",
            "data": {
                "symbol": "M&M",
                "company": "Mahindra & Mahindra",
                "note": "Special chars: <>&'\""
            }
        }

        await test_manager.broadcast(message)

        # Should properly escape and send
        call_args = mock_ws.send_text.call_args[0][0]
        parsed = json.loads(call_args)
        assert parsed["data"]["symbol"] == "M&M"

    @pytest.mark.asyncio
    async def test_multiple_managers(self):
        """Test creating multiple manager instances"""
        manager1 = ConnectionManager()
        manager2 = ConnectionManager()

        # They should be independent
        mock_ws = AsyncMock()
        await manager1.connect(mock_ws)

        assert len(manager1.active_connections) == 1
        assert len(manager2.active_connections) == 0

    @pytest.mark.asyncio
    async def test_broadcast_after_loop_set(self):
        """Test broadcasting after setting event loop"""
        test_manager = ConnectionManager()
        loop = asyncio.get_running_loop()
        test_manager.set_loop(loop)

        mock_ws = AsyncMock()
        test_manager.active_connections = [mock_ws]

        message = {"type": "test", "data": {}}
        await test_manager.broadcast(message)

        mock_ws.send_text.assert_called_once()