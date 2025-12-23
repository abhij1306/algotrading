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
def connect_websocket():
    """Initialize Fyers WebSocket connection"""
    try:
        from ..services.fyers_websocket import get_websocket_service
        ws_service = get_websocket_service()
        ws_service.connect()
        return {"status": "connected", "message": "WebSocket initialized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/subscribe")
def subscribe_symbols(request: SubscribeRequest):
    """Subscribe to symbols for live tick data"""
    try:
        from ..services.fyers_websocket import get_websocket_service
        ws_service = get_websocket_service()
        
        # Convert symbols to Fyers format if needed (e.g., SBIN -> NSE:SBIN-EQ)
        fyers_symbols = []
        for symbol in request.symbols:
            if ":" not in symbol:  # Not in Fyers format
                fyers_symbols.append(f"NSE:{symbol}-EQ")
            else:
                fyers_symbols.append(symbol)
        
        ws_service.subscribe(fyers_symbols)
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
        from ..services.fyers_websocket import get_websocket_service
        ws_service = get_websocket_service()
        return {
            "connected": ws_service.ws is not None,
            "subscribed_symbols": list(ws_service.subscribed_symbols)
        }
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
    try:
        while True:
            # Keep connection open and listen for client messages (e.g. ping/subscribe)
            # Currently only server->client broadcasting is primary
            data = await websocket.receive_text()
            
            # Simple pong or ack
            if data == "ping":
                await websocket.send_text('{"type":"pong"}')
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        # print(f"WebSocket error: {e}")
        manager.disconnect(websocket)
