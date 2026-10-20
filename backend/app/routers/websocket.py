"""
WebSocket API Endpoint
Allows frontend to connect/disconnect/subscribe to live data
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, WebSocket
from pydantic import BaseModel

from ..services.deferred_startup import startup_status
from ..services.symbol_master import symbol_master
from ..utils.errors import handle_api_error
from ..utils.ws_manager import manager

if TYPE_CHECKING:
    from ..services.live_market_service import LiveMarketService

logger = logging.getLogger(__name__)

router = APIRouter()


class SubscribeRequest(BaseModel):
    symbols: list[str]


@router.post("/connect", response_model=None)
async def connect_websocket() -> dict[str, Any]:
    """Initialize Fyers WebSocket connection"""
    try:
        from ..services.live_market_service import connect_status_message, live_market

        # Ensure live_market captures the running loop for thread-safe broadcasting.
        connect_status = await live_market.connect_async(
            loop=asyncio.get_running_loop(), explicit=True
        )
        return {"status": connect_status, "message": connect_status_message(connect_status)}
    except Exception as e:
        raise handle_api_error(e, "Failed to initialize WebSocket") from e


@router.post("/subscribe", response_model=None)
async def subscribe_symbols(request: SubscribeRequest) -> dict[str, Any]:
    """Subscribe to symbols for live tick data"""
    try:
        from ..services.live_market_service import live_market

        # Normalize to DB symbols at the API boundary;
        # LiveMarketService handles provider conversion.
        db_symbols = [symbol_master.to_db(s) for s in request.symbols]
        await live_market.subscribe_api(db_symbols)
        return {"status": "subscribed", "symbols": db_symbols}
    except Exception as e:
        raise handle_api_error(e, "Failed to subscribe to symbols") from e


@router.post("/disconnect", response_model=None)
def disconnect_websocket() -> dict[str, Any]:
    """Close WebSocket connection"""
    try:
        from ..services.live_market_service import live_market

        live_market.disconnect()
        return {"status": "disconnected"}
    except Exception as e:
        raise handle_api_error(e, "Failed to disconnect WebSocket") from e


@router.get("/status", response_model=None)
async def get_websocket_status(request: Request) -> dict[str, Any]:
    """Check WebSocket connection status.

    `startup` is DEFERRED while the database work skipped at startup (DB down) is still
    being retried, FAILED if that work hit a non-connection error, else COMPLETE.
    """
    try:
        from ..services.live_market_service import live_market

        status = await live_market.get_status()
        return {**status, "startup": startup_status(request.app.state).value}
    except Exception:
        logger.exception("WebSocket status check failed")
        return {"connected": False, "error": "WebSocket status unavailable"}


@router.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for frontend clients (Terminal, Screener).
    Connect to: ws://localhost:8000/api/websocket/stream
    """

    from ..services.live_market_service import live_market

    # Attempt to accept and handshake — may fail if client disconnects early
    connected = await manager.connect(websocket)
    if not connected:
        return  # Client already gone, nothing to do

    try:
        while True:
            try:
                data = await websocket.receive_text()
                await _handle_stream_text(websocket, data, live_market)
            except Exception:
                # WebSocketDisconnect or any other receive/handling failure ends the stream.
                break

    except Exception:
        # Connection already dead
        logger.debug("WebSocket stream ended with an error", exc_info=True)
    finally:
        # manager.disconnect drops this client from the registry and releases the symbols
        # no other owner holds via on_symbols_released.
        await manager.disconnect(websocket)


async def _handle_stream_text(
    websocket: WebSocket, data: str, live_market: "LiveMarketService"
) -> None:
    """Handle one text frame from a stream client: ping, subscribe or unsubscribe."""
    if not data:
        return
    if data == "ping":
        await _safe_send(websocket, {"type": "pong"})
        return

    try:
        message = json.loads(data)
    except json.JSONDecodeError:
        return  # Ignore invalid JSON

    action = message.get("action") or message.get("type")
    if action == "subscribe":
        await _stream_subscribe(websocket, message.get("symbols", []), live_market)
    elif action == "unsubscribe":
        await _stream_unsubscribe(websocket, message.get("symbols", []), live_market)
    elif action == "ping":
        await _safe_send(websocket, {"type": "pong"})


async def _stream_subscribe(
    websocket: WebSocket, symbols: list[str], live_market: "LiveMarketService"
) -> None:
    if not symbols:
        return
    db_symbols = [symbol_master.to_db(s) for s in symbols]
    # The registry returns the symbols no owner held before: the provider subscribe delta.
    newly_added = await manager.subscribe(websocket, db_symbols)

    try:
        if newly_added:
            await live_market.subscribe(newly_added)
    except Exception as sub_e:
        logger.warning(f"[WebSocket] LiveMarket sub failed: {sub_e}")

    await _safe_send(
        websocket,
        {
            "type": "ack",
            "action": "subscribe",
            "count": len(db_symbols),
            "provider_delta": len(newly_added),
        },
    )


async def _stream_unsubscribe(
    websocket: WebSocket, symbols: list[str], live_market: "LiveMarketService"
) -> None:
    if not symbols:
        return
    db_symbols = [symbol_master.to_db(s) for s in symbols]
    # The registry returns the symbols no owner holds any more: the provider unsubscribe delta.
    removed_symbols = await manager.unsubscribe(websocket, db_symbols)
    if removed_symbols:
        await live_market.unsubscribe(removed_symbols)
    await _safe_send(
        websocket,
        {
            "type": "ack",
            "action": "unsubscribe",
            "count": len(db_symbols),
            "provider_delta": len(removed_symbols),
        },
    )


async def _safe_send(websocket: WebSocket, data: dict) -> None:
    """Send JSON to a WebSocket, swallowing errors if client disconnected."""
    # Client gone, nothing to do
    with contextlib.suppress(Exception):
        await websocket.send_json(data)
