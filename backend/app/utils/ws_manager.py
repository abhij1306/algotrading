"""
WebSocket Manager for broadcasting messages to connected clients
Handles connection lifecycle and broadcasting
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)
SEND_TIMEOUT_SEC = 1.0
CLIENT_QUEUE_SIZE = 8


@dataclass
class ClientConnection:
    websocket: WebSocket
    subscriptions: set[str] = field(default_factory=set)
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )
    sender_task: asyncio.Task | None = None


class ConnectionManager:
    def __init__(self):
        self.subscriptions: dict[WebSocket, set[str]] = {}
        self.connections: dict[WebSocket, ClientConnection] = {}
        self._global_subscriptions: set[str] = set()  # Cache for O(1) lookup
        self.loop = None

    def set_loop(self, loop):
        """Set event loop for thread-safe broadcasting"""
        self.loop = loop
        logger.debug(f"[WSManager] Event loop set: {loop is not None}")

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

        client = ClientConnection(websocket=websocket)
        client.sender_task = asyncio.create_task(self._sender_loop(client))
        self.connections[websocket] = client
        self.subscriptions[websocket] = client.subscriptions
        logger.debug(f"[WSManager] Client connected. Total: {len(self.connections)}")

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
        client = self.connections.pop(websocket, None)
        self.subscriptions.pop(websocket, None)
        if client:
            removed_symbols = set(client.subscriptions)
            if client.sender_task:
                client.sender_task.cancel()
            # Rebuild cached global symbol set after removing this client.
            self._global_subscriptions = (
                set().union(*(conn.subscriptions for conn in self.connections.values()))
                if self.connections
                else set()
            )
        logger.debug(f"[WSManager] Client disconnected. Total: {len(self.connections)}")
        return removed_symbols

    async def subscribe(self, websocket: WebSocket, symbols: list[str]):
        """Subscribe a specific connection to symbols"""
        client = self.connections.get(websocket)
        if client:
            for symbol in symbols:
                client.subscriptions.add(symbol)
                self._global_subscriptions.add(symbol)  # Update global cache
            logger.debug(
                f"[WSManager] Client subscribed to {len(symbols)} symbols. Total symbols for client: {len(client.subscriptions)}"
            )
        await asyncio.sleep(0)

    async def unsubscribe(self, websocket: WebSocket, symbols: list[str]):
        """Unsubscribe a specific connection from symbols"""
        client = self.connections.get(websocket)
        if client:
            for symbol in symbols:
                client.subscriptions.discard(symbol)
            # Rebuild global cache after unsubscribe
            self._global_subscriptions = (
                set().union(*(conn.subscriptions for conn in self.connections.values()))
                if self.connections
                else set()
            )
            logger.debug(
                f"[WSManager] Client unsubscribed from {len(symbols)} symbols. Remaining: {len(client.subscriptions)}"
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
        if not self.connections and not self.subscriptions:
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

        all_connections = set(self.connections.keys()) | set(self.subscriptions.keys())
        for connection in list(all_connections):
            try:
                client = self.connections.get(connection)
                if connection.client_state != WebSocketState.CONNECTED:
                    self.disconnect(connection)
                    continue

                client_subs = self.subscriptions.get(
                    connection, client.subscriptions if client else set()
                )
                if is_batch:
                    batch_data = message.get("data", [])
                    client_batch = [t for t in batch_data if t.get("symbol") in client_subs]
                    if not client_batch:
                        continue
                    outbound = {"type": "ticker_batch", "data": client_batch}
                else:
                    if target_symbol and target_symbol not in client_subs:
                        continue
                    outbound = {"__pre_serialized__": json_msg}

                if client:
                    try:
                        client.queue.put_nowait(outbound)
                    except asyncio.QueueFull:
                        logger.warning("[WSManager] Dropping slow websocket client")
                        self.disconnect(connection)
                else:
                    if "__pre_serialized__" in outbound:
                        await connection.send_text(str(outbound["__pre_serialized__"]))
                    else:
                        await connection.send_json(outbound)
            except (WebSocketDisconnect, RuntimeError):
                self.disconnect(connection)
            except Exception:
                self.disconnect(connection)

    async def _sender_loop(self, client: ClientConnection) -> None:
        try:
            while True:
                payload = await client.queue.get()
                if client.websocket.client_state != WebSocketState.CONNECTED:
                    break

                if "__pre_serialized__" in payload:
                    await asyncio.wait_for(
                        client.websocket.send_text(str(payload["__pre_serialized__"])),
                        timeout=SEND_TIMEOUT_SEC,
                    )
                else:
                    await asyncio.wait_for(
                        client.websocket.send_json(payload),
                        timeout=SEND_TIMEOUT_SEC,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.disconnect(client.websocket)


# Singleton instance
manager = ConnectionManager()
