"""
WebSocket API Endpoint
Allows frontend to connect/disconnect/subscribe to live data
"""

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..services.symbol_master import symbol_master
from ..utils.errors import handle_api_error
from ..utils.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter()


class SubscribeRequest(BaseModel):
    symbols: list[str]


@router.post("/connect", response_model=None)
async def connect_websocket() -> dict[str, Any]:
    """Initialize Fyers WebSocket connection"""
    try:
        from ..services.live_market_service import live_market

        # Ensure live_market captures the running loop for thread-safe broadcasting.
        connect_status = await live_market.connect_async(
            loop=asyncio.get_running_loop(), explicit=True
        )
        message_map = {
            "started": "WebSocket initialization triggered",
            "already_connected": "WebSocket already connected",
            "disconnected": "WebSocket was explicitly disconnected",
            "market_closed": "WebSocket deferred because market is closed",
            "token_invalid": "WebSocket deferred because Fyers token is invalid/expired",
            "error": "WebSocket initialization failed",
        }
        return {
            "status": connect_status,
            "message": message_map.get(connect_status, "WebSocket initialization completed"),
        }
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
async def get_websocket_status() -> dict[str, Any]:
    """Check WebSocket connection status"""
    try:
        from ..services.live_market_service import live_market

        return await live_market.get_status()
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for frontend clients (Terminal, Screener).
    Connect to: ws://localhost:8000/api/websocket/stream
    """
    import json

    from ..services.live_market_service import live_market

    # Attempt to accept and handshake — may fail if client disconnects early
    connected = await manager.connect(websocket)
    if not connected:
        return  # Client already gone, nothing to do

    try:
        while True:
            try:
                data = await websocket.receive_text()

                if not data:
                    continue

                if data == "ping":
                    await _safe_send(websocket, {"type": "pong"})
                    continue

                try:
                    message = json.loads(data)
                    action = message.get("action") or message.get("type")

                    if action == "subscribe":
                        symbols = message.get("symbols", [])
                        if symbols:
                            db_symbols = [symbol_master.to_db(s) for s in symbols]
                            before_symbols = manager.get_all_subscribed_symbols()
                            await manager.subscribe(websocket, db_symbols)
                            after_symbols = manager.get_all_subscribed_symbols()
                            newly_added = sorted(after_symbols - before_symbols)

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

                    elif action == "unsubscribe":
                        symbols = message.get("symbols", [])
                        if symbols:
                            db_symbols = [symbol_master.to_db(s) for s in symbols]
                            before_symbols = manager.get_all_subscribed_symbols()
                            await manager.unsubscribe(websocket, db_symbols)
                            after_symbols = manager.get_all_subscribed_symbols()
                            removed_symbols = sorted(before_symbols - after_symbols)
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

                    elif action == "ping":
                        await _safe_send(websocket, {"type": "pong"})

                except json.JSONDecodeError:
                    pass  # Ignore invalid JSON

            except WebSocketDisconnect:
                break
            except Exception:
                break

    except Exception:
        # Connection already dead
        logger.debug("WebSocket stream ended with an error", exc_info=True)
    finally:
        # manager.disconnect releases symbols no other client needs via on_symbols_released.
        await manager.disconnect(websocket)


async def _safe_send(websocket: WebSocket, data: dict) -> None:
    """Send JSON to a WebSocket, swallowing errors if client disconnected."""
    # Client gone, nothing to do
    with contextlib.suppress(Exception):
        await websocket.send_json(data)
