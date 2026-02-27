"""
WebSocket Manager for broadcasting messages to connected clients
Handles connection lifecycle and broadcasting
"""

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        # Subscriptions mapping: websocket -> set of symbols
        # Also serves as the registry of active connections
        self.subscriptions: dict[WebSocket, set] = {}
        self._global_subscriptions: set[str] = set()  # Cache for O(1) lookup
        self.loop = None

    def set_loop(self, loop):
        """Set event loop for thread-safe broadcasting"""
        self.loop = loop
        logger.info(f"[WSManager] Event loop set: {loop is not None}")

    async def connect(self, websocket: WebSocket) -> bool:
        """
        Accept a WebSocket connection and send the initial handshake.
        Returns True if successful, False if the client disconnected before we could finish.
        """
        try:
            await websocket.accept()
        except Exception as e:
            logger.debug(f"[WSManager] Failed to accept WebSocket: {e}")
            return False

        self.subscriptions[websocket] = set()
        logger.info(f"[WSManager] Client connected. Total: {len(self.subscriptions)}")

        # Send initial welcome — client may have already disconnected (React StrictMode, page nav)
        try:
            await websocket.send_json(
                {
                    "type": "connection_established",
                    "message": "Connected to SmartTrader WebSocket Stream",
                }
            )
        except (WebSocketDisconnect, Exception):
            # Normal disconnect during handshake (React StrictMode, page navigation)
            logger.debug("[WSManager] Client disconnected before handshake completion")
            self.disconnect(websocket)
            return False

        return True

    def disconnect(self, websocket: WebSocket) -> set[str]:
        removed_symbols: set[str] = set()
        if websocket in self.subscriptions:
            removed_symbols = set(self.subscriptions[websocket])
            del self.subscriptions[websocket]
            # Rebuild cached global symbol set after removing this client.
            self._global_subscriptions = (
                set().union(*self.subscriptions.values()) if self.subscriptions else set()
            )
        logger.info(f"[WSManager] Client disconnected. Total: {len(self.subscriptions)}")
        return removed_symbols

    async def subscribe(self, websocket: WebSocket, symbols: list[str]):
        """Subscribe a specific connection to symbols"""
        if websocket in self.subscriptions:
            for symbol in symbols:
                self.subscriptions[websocket].add(symbol)
                self._global_subscriptions.add(symbol)  # Update global cache
            logger.info(
                f"[WSManager] Client subscribed to {len(symbols)} symbols. Total symbols for client: {len(self.subscriptions[websocket])}"
            )
        await asyncio.sleep(0)

    async def unsubscribe(self, websocket: WebSocket, symbols: list[str]):
        """Unsubscribe a specific connection from symbols"""
        if websocket in self.subscriptions:
            for symbol in symbols:
                self.subscriptions[websocket].discard(symbol)
            # Rebuild global cache after unsubscribe
            self._global_subscriptions = (
                set().union(*self.subscriptions.values()) if self.subscriptions else set()
            )
            logger.info(
                f"[WSManager] Client unsubscribed from {len(symbols)} symbols. Remaining: {len(self.subscriptions[websocket])}"
            )
        await asyncio.sleep(0)

    def get_all_subscribed_symbols(self) -> set[str]:
        """Get union of subscribed symbols across all active clients (O(1) cached)."""
        # Return a copy so callers can safely diff before/after subscribe/unsubscribe.
        return set(self._global_subscriptions)

    async def broadcast(self, message: dict[str, Any]):
        """
        Broadcast JSON message to clients.
        Supports 'ticker_batch' (filtered per client) and single 'ticker' (broadcast to subscribers).
        """
        if not self.subscriptions:
            return

        try:
            is_batch = message.get("type") == "ticker_batch"

            # For non-batch optimization: pre-serialize
            json_msg = None
            target_symbol = None

            if not is_batch:
                if message.get("type") == "ticker" and "data" in message:
                    target_symbol = message["data"].get("symbol")
                json_msg = json.dumps(message)

        except Exception as e:
            logger.error(f"[WSManager] Failed to serialize message: {e}")
            return

        # Iterate over copy keys for thread safety during iteration
        for connection in list(self.subscriptions.keys()):
            try:
                # Check if connection is still open
                if connection.client_state != WebSocketState.CONNECTED:
                    self.disconnect(connection)
                    continue

                client_subs = self.subscriptions.get(connection)
                if client_subs is None:
                    continue

                if is_batch:
                    # Filter batch for this client
                    batch_data = message.get("data", [])
                    client_batch = [t for t in batch_data if t.get("symbol") in client_subs]

                    if client_batch:
                        # Send specific batch to this client
                        await connection.send_json({"type": "ticker_batch", "data": client_batch})

                else:
                    # Standard broadcast (single message)
                    if target_symbol and target_symbol not in client_subs:
                        continue

                    await connection.send_text(json_msg)

            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(connection)
            except Exception:
                self.disconnect(connection)


# Singleton instance
manager = ConnectionManager()
