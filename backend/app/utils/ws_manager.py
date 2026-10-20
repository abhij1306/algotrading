"""
WebSocket Manager for broadcasting messages to connected clients
Handles connection lifecycle and broadcasting
"""

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from ..services.subscription_registry import SubscriptionRegistry
from .runtime_metrics import runtime_metrics

logger = logging.getLogger(__name__)

ACTIVE_CONNECTIONS_GAUGE = "websocket.connections.active"
ACTIVE_SUBSCRIPTIONS_GAUGE = "websocket.subscriptions.active"
SEND_TIMEOUT_SEC = 1.0
CLIENT_QUEUE_SIZE = 8

_client_ids = itertools.count(1)


@dataclass
class ClientConnection:
    websocket: WebSocket
    # Owner id of this client's symbols in the SubscriptionRegistry.
    client_id: str = field(default_factory=lambda: f"ws-{next(_client_ids)}")
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )
    sender_task: asyncio.Task | None = None


class ConnectionManager:
    def __init__(self, registry: SubscriptionRegistry | None = None) -> None:
        self.connections: dict[WebSocket, ClientConnection] = {}
        # Who wants which symbols (clients and the REST API owner); the provider delta source.
        self.registry = registry or SubscriptionRegistry()
        # Awaited with symbols no owner needs any more after a client is removed.
        self.on_symbols_released: Callable[[list[str]], Awaitable[None]] | None = None
        self._lock = asyncio.Lock()

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
        async with self._lock:
            self.connections[websocket] = client
        runtime_metrics.increment("websocket.connections.accepted")
        runtime_metrics.set_gauge(ACTIVE_CONNECTIONS_GAUGE, float(len(self.connections)))
        logger.debug(f"[WSManager] Client connected. Total: {len(self.connections)}")

        # Send initial welcome — client may have already disconnected (React StrictMode, page nav)
        try:
            await websocket.send_json(
                {
                    "type": "connection_established",
                    "message": "Connected to SmartTrader WebSocket Stream",
                }
            )
        except Exception:
            # Normal disconnect during handshake (React StrictMode, page navigation)
            logger.debug("[WSManager] Client disconnected before handshake completion")
            await self.disconnect(websocket)
            return False

        return True

    async def disconnect(self, websocket: WebSocket) -> set[str]:
        """Remove a client and release provider subscriptions no other owner holds.

        Returns the released symbols (empty if the client was already removed).
        """
        released_symbols: set[str] = set()
        async with self._lock:
            client = self.connections.pop(websocket, None)
            if client:
                # The sender loop disconnects its own client on a send error; cancelling
                # itself would abort the provider release awaited below.
                if client.sender_task and client.sender_task is not asyncio.current_task():
                    client.sender_task.cancel()
                released_symbols = self.registry.drop_owner(client.client_id)
        runtime_metrics.increment("websocket.connections.disconnected")
        runtime_metrics.set_gauge(ACTIVE_CONNECTIONS_GAUGE, float(len(self.connections)))
        self._set_subscriptions_gauge()
        logger.debug(f"[WSManager] Client disconnected. Total: {len(self.connections)}")
        released = sorted(released_symbols)
        if released and self.on_symbols_released:
            try:
                await self.on_symbols_released(released)
            except Exception as exc:
                logger.warning("[WSManager] Releasing provider symbols failed: %s", exc)
        return set(released)

    async def subscribe(self, websocket: WebSocket, symbols: list[str]) -> list[str]:
        """Subscribe a connection to symbols.

        Returns the symbols no owner held before (the provider subscribe delta), sorted;
        empty when the connection is unknown.
        """
        newly_needed: set[str] = set()
        async with self._lock:
            client = self.connections.get(websocket)
            if client:
                newly_needed = self.registry.add(client.client_id, symbols)
                runtime_metrics.increment("websocket.subscriptions.added", float(len(symbols)))
                self._set_subscriptions_gauge()
                logger.debug(
                    "[WSManager] Client subscribed to %s symbols. Total symbols for client: %s",
                    len(symbols),
                    len(self.registry.symbols_of(client.client_id)),
                )
        await asyncio.sleep(0)
        return sorted(newly_needed)

    async def unsubscribe(self, websocket: WebSocket, symbols: list[str]) -> list[str]:
        """Unsubscribe a connection from symbols.

        Returns the symbols no owner holds any more (the provider unsubscribe delta), sorted;
        empty when the connection is unknown.
        """
        no_longer_needed: set[str] = set()
        async with self._lock:
            client = self.connections.get(websocket)
            if client:
                no_longer_needed = self.registry.remove(client.client_id, symbols)
                runtime_metrics.increment("websocket.subscriptions.removed", float(len(symbols)))
                self._set_subscriptions_gauge()
                logger.debug(
                    "[WSManager] Client unsubscribed from %s symbols. Remaining: %s",
                    len(symbols),
                    len(self.registry.symbols_of(client.client_id)),
                )
        await asyncio.sleep(0)
        return sorted(no_longer_needed)

    def _set_subscriptions_gauge(self) -> None:
        runtime_metrics.set_gauge(
            ACTIVE_SUBSCRIPTIONS_GAUGE, float(len(self.registry.all_symbols()))
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Broadcast JSON message to clients.
        Supports 'ticker_batch' (filtered per client) and single 'ticker'
        (broadcast to subscribers).
        """
        async with self._lock:
            active_connections = list(self.connections.values())
        if not active_connections:
            return

        is_batch = message.get("type") == "ticker_batch"
        target_symbol = None
        json_msg = None
        if not is_batch:
            # For non-batch optimization: pre-serialize once for every client.
            try:
                target_symbol, json_msg = self._pre_serialize(message)
            except Exception as e:
                logger.exception(f"[WSManager] Failed to serialize message: {e}")
                return

        for client in active_connections:
            await self._deliver(client, message, target_symbol, json_msg)

    async def _deliver(
        self,
        client: ClientConnection,
        message: dict[str, Any],
        target_symbol: str | None,
        json_msg: str | None,
    ) -> None:
        """Queue the client's share of a broadcast; drop clients that are gone.

        `json_msg` is None for a ticker_batch, which is filtered per client.
        """
        connection = client.websocket
        try:
            if connection.client_state != WebSocketState.CONNECTED:
                await self.disconnect(connection)
                return
            client_subs = self.registry.symbols_of(client.client_id)
            if json_msg is None:
                outbound = self._client_batch(message, client_subs)
            elif target_symbol and target_symbol not in client_subs:
                outbound = None
            else:
                outbound = {"__pre_serialized__": json_msg}
            if outbound is not None:
                await self._enqueue(client, outbound)
        except Exception:
            # Includes WebSocketDisconnect and RuntimeError from a closed socket.
            await self.disconnect(connection)

    @staticmethod
    def _pre_serialize(message: dict[str, Any]) -> tuple[str | None, str]:
        """(target symbol of a single 'ticker' message or None, JSON text of the message)."""
        target_symbol = None
        if message.get("type") == "ticker" and "data" in message:
            target_symbol = message["data"].get("symbol")
        return target_symbol, json.dumps(message)

    @staticmethod
    def _client_batch(
        message: dict[str, Any], client_subs: frozenset[str]
    ) -> dict[str, Any] | None:
        """The part of a ticker_batch a client subscribes to, or None if nothing applies."""
        batch_data = message.get("data", [])
        client_batch = [t for t in batch_data if t.get("symbol") in client_subs]
        if not client_batch:
            return None
        runtime_metrics.increment("websocket.messages.batched_tickers", float(len(client_batch)))
        runtime_metrics.set_gauge("websocket.batch.last_size", float(len(client_batch)))
        return {"type": "ticker_batch", "data": client_batch}

    async def _enqueue(self, client: ClientConnection, outbound: dict[str, Any]) -> None:
        """Queue a message for a client; drop the client if its queue is full."""
        try:
            client.queue.put_nowait(outbound)
            runtime_metrics.increment("websocket.messages.enqueued")
        except asyncio.QueueFull:
            runtime_metrics.increment("websocket.queue_overflow")
            logger.warning("[WSManager] Dropping slow websocket client")
            connection = client.websocket
            await self.disconnect(connection)
            try:
                await connection.close(code=1013)
            except Exception as close_exc:
                logger.debug("[WSManager] Closing slow client failed: %s", close_exc)

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
        except Exception as exc:
            logger.exception(
                "[WSManager] Sender loop failed for client %s", client.websocket, exc_info=exc
            )
            await self.disconnect(client.websocket)

    async def get_health_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            active_connections = list(self.connections.values())
            active_symbols = len(self.registry.all_symbols())
            pending_messages = sum(conn.queue.qsize() for conn in active_connections)
            runtime_metrics.set_gauge(ACTIVE_CONNECTIONS_GAUGE, float(len(active_connections)))
            runtime_metrics.set_gauge(ACTIVE_SUBSCRIPTIONS_GAUGE, float(active_symbols))
            runtime_metrics.set_gauge("websocket.queue.pending_total", float(pending_messages))
        return {
            "active_clients": len(active_connections),
            "active_symbols": active_symbols,
            "pending_messages": pending_messages,
            "captured_at": datetime.now(UTC).isoformat(),
            "metrics": runtime_metrics.snapshot(prefix="websocket.", strip_prefix=True),
        }


# Singleton instance
manager = ConnectionManager()
