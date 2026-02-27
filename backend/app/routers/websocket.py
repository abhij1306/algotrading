"""
WebSocket API Endpoint
Allows frontend to connect/disconnect/subscribe to live data
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..services.symbol_master import symbol_master
from ..utils.ws_manager import manager
from ..utils.errors import handle_api_error

logger = logging.getLogger(__name__)

router = APIRouter()


class SubscribeRequest(BaseModel):
    symbols: list[str]


@router.post("/connect")
async def connect_websocket():
    """Initialize Fyers WebSocket connection"""
    try:
        from ..services.live_market_service import live_market

        # Ensure live_market captures the running loop for thread-safe broadcasting.
        connect_status = live_market.connect(loop=asyncio.get_running_loop())
        message_map = {
            "started": "WebSocket initialization triggered",
            "already_connected": "WebSocket already connected",
            "market_closed": "WebSocket deferred because market is closed",
            "token_invalid": "WebSocket deferred because Fyers token is invalid/expired",
            "error": "WebSocket initialization failed",
        }
        return {
            "status": connect_status,
            "message": message_map.get(connect_status, "WebSocket initialization completed"),
        }
    except Exception as e:
        raise handle_api_error(e, "Failed to initialize WebSocket")


@router.post("/subscribe")
async def subscribe_symbols(request: SubscribeRequest):
    """Subscribe to symbols for live tick data"""
    try:
        from ..services.live_market_service import live_market

        # Normalize to DB symbols at the API boundary; LiveMarketService handles provider conversion.
        db_symbols = [symbol_master.to_db(s) for s in request.symbols]
        await live_market.subscribe(db_symbols)
        return {"status": "subscribed", "symbols": db_symbols}
    except Exception as e:
        raise handle_api_error(e, "Failed to subscribe to symbols")


@router.post("/disconnect")
def disconnect_websocket():
    """Close WebSocket connection"""
    try:
        from ..services.fyers_websocket import get_websocket_service

        ws_service = get_websocket_service()
        ws_service.disconnect()
        return {"status": "disconnected"}
    except Exception as e:
        raise handle_api_error(e, "Failed to disconnect WebSocket")


@router.get("/status")
def get_websocket_status():
    """Check WebSocket connection status"""
    try:
        from ..services.live_market_service import live_market

        return live_market.get_status()
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket):
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
        pass  # Connection already dead
    finally:
        before_symbols = manager.get_all_subscribed_symbols()
        manager.disconnect(websocket)
        after_symbols = manager.get_all_subscribed_symbols()
        removed_symbols = sorted(before_symbols - after_symbols)
        if removed_symbols:
            try:
                await live_market.unsubscribe(removed_symbols)
            except Exception as unsub_e:
                logger.warning(f"[WebSocket] LiveMarket cleanup unsubscribe failed: {unsub_e}")


async def _safe_send(websocket: WebSocket, data: dict):
    """Send JSON to a WebSocket, swallowing errors if client disconnected."""
    try:
        await websocket.send_json(data)
    except (WebSocketDisconnect, RuntimeError, Exception):
        pass  # Client gone, nothing to do
