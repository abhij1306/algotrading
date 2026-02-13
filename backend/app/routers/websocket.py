"""
WebSocket API Endpoint
Allows frontend to connect/disconnect/subscribe to live data
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List

router = APIRouter()

class SubscribeRequest(BaseModel):
    symbols: List[str]

@router.post("/connect")
async def connect_websocket():
    """Initialize Fyers WebSocket connection"""
    try:
        from ..services.live_market_service import live_market
        live_market.connect()
        return {"status": "connected", "message": "WebSocket initialization triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/subscribe")
async def subscribe_symbols(request: SubscribeRequest):
    """Subscribe to symbols for live tick data"""
    try:
        from ..services.live_market_service import live_market
        
        # Convert symbols to Fyers format if needed (e.g., SBIN -> NSE:SBIN-EQ)
        fyers_symbols = []
        for symbol in request.symbols:
            if ":" not in symbol:  # Not in Fyers format
                fyers_symbols.append(f"NSE:{symbol}-EQ")
            else:
                fyers_symbols.append(symbol)
        
        await live_market.subscribe(fyers_symbols)
        return {"status": "subscribed", "symbols": fyers_symbols}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/disconnect")
def disconnect_websocket():
    """Close WebSocket connection"""
    try:
        from ..services.fyers_websocket import get_websocket_service
        ws_service = get_websocket_service()
        ws_service.disconnect()
        return {"status": "disconnected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/status")
def get_websocket_status():
    """Check WebSocket connection status"""
    try:
        from ..services.live_market_service import live_market
        return live_market.get_status()
    except Exception as e:
        return {"connected": False, "error": str(e)}

# ==========================================
# Real-time Streaming Endpoint
# ==========================================
from fastapi import WebSocket, WebSocketDisconnect
from ..utils.ws_manager import manager

@router.websocket("/stream")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for frontend clients (Terminal, Screener).
    Connect to: ws://localhost:8000/api/websocket/stream
    """
    await manager.connect(websocket)
    from ..services.live_market_service import live_market
    import json

    try:
        while True:
            # Keep connection open and listen for client messages (e.g. ping/subscribe)
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                action = message.get("action")

                if action == "subscribe":
                    symbols = message.get("symbols", [])
                    if symbols:
                        await live_market.subscribe(symbols)
                        await websocket.send_json({
                            "type": "ack",
                            "action": "subscribe",
                            "count": len(symbols)
                        })

                elif action == "unsubscribe":
                    symbols = message.get("symbols", [])
                    if symbols:
                        await live_market.unsubscribe(symbols)
                        await websocket.send_json({
                            "type": "ack",
                            "action": "unsubscribe",
                            "count": len(symbols)
                        })
                
                elif action == "ping" or data == "ping":
                    await websocket.send_json({"type": "pong"})

            except json.JSONDecodeError:
                if data == "ping":
                    await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        # logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)
