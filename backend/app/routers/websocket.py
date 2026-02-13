"""
WebSocket API Endpoint
Allows frontend to connect/disconnect/subscribe to live data
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from ..services.symbol_master import symbol_master

router = APIRouter()

class SubscribeRequest(BaseModel):
    symbols: List[str]

@router.post("/connect")
async def connect_websocket():
    """
    Trigger initialization of the Fyers live-market WebSocket connection.
    
    Returns:
        dict: A status object containing `status` (e.g., "connected") and `message` describing the result.
    
    Raises:
        HTTPException: If connection initialization fails; the exception detail contains the underlying error message.
    """
    try:
        from ..services.live_market_service import live_market
        live_market.connect()
        return {"status": "connected", "message": "WebSocket initialization triggered"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/subscribe")
async def subscribe_symbols(request: SubscribeRequest):
    """
    Subscribe the given symbols to the live market feed.
    
    Parameters:
    	request (SubscribeRequest): Request containing `symbols` in client format; symbols are converted to Fyers format before subscribing.
    
    Returns:
    	response (dict): Dictionary with keys:
    		- "status": the string "subscribed"
    		- "symbols": list of subscribed symbols in Fyers format
    
    Raises:
    	HTTPException: If subscribing fails; raised with status_code 500 and the error detail.
    """
    try:
        from ..services.live_market_service import live_market
        
        # Convert symbols to Fyers format using Symbol Master
        fyers_symbols = symbol_master.batch_to_fyers(request.symbols)
        
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
    """
    Return the current WebSocket connection status.
    
    Returns:
        status (dict): A mapping containing:
            - "connected" (bool): `True` if the WebSocket is connected, `False` otherwise.
            - "error" (str): Error message when unable to retrieve status; present only if an error occurred.
    """
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
    Handle a WebSocket connection that accepts subscription commands and pings from frontend clients.
    
    Supports incoming messages in either JSON or plain text:
    - JSON messages with an "action" field:
      - "subscribe" with "symbols": list[str] — subscribes to the provided symbols and sends an acknowledgment JSON: {"type": "ack", "action": "subscribe", "count": <n>}.
      - "unsubscribe" with "symbols": list[str] — unsubscribes the provided symbols and sends an acknowledgment JSON: {"type": "ack", "action": "unsubscribe", "count": <n>}.
      - "ping" — responds with {"type": "pong"}.
    - Plain text "ping" — responds with {"type": "pong"}.
    
    Side effects:
    - Calls into the live market service to perform subscribe/unsubscribe operations.
    - Registers and deregisters the WebSocket connection with the connection manager on connect/disconnect.
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