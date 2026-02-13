"""
WebSocket Manager for broadcasting messages to connected clients
Handles connection lifecycle and broadcasting
"""
from typing import List, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # List of active websocket connections
        """
        Create a ConnectionManager instance and prepare internal state for tracking WebSocket clients and an optional event loop.
        
        Attributes:
            active_connections (List[WebSocket]): List tracking currently connected WebSocket clients.
            loop: Optional event loop reference used for thread-safe broadcasting; initialized to None.
        
        Notes:
            Broadcasting currently targets all connected clients (simple pub-sub). A subscriptions mapping (symbol -> websockets) may be added later for optimization.
        """
        self.active_connections: List[WebSocket] = []
        self.loop = None
        
        # Subscriptions mapping: symbol -> list of websockets (optimization)
        # For now, we'll broadcast all to all (simple pub-sub) for simplicity
        # Optimization can be added if traffic becomes huge
        
    def set_loop(self, loop):
        """
        Set the asyncio event loop used for thread-safe broadcasting.
        
        Parameters:
            loop (asyncio.AbstractEventLoop | None): Event loop to assign; pass None to unset.
        """
        self.loop = loop
        logger.info(f"[WSManager] Event loop set: {loop is not None}")

    async def connect(self, websocket: WebSocket):
        """
        Accepts an incoming WebSocket connection and registers it with the manager.
        
        Parameters:
            websocket (WebSocket): The incoming FastAPI WebSocket to accept and add to the active connections list.
        """
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket client connected. Total: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: Dict[str, Any]):
        """Broadcast JSON message to all connected clients"""
        if not self.active_connections:
            return
            
        json_msg = json.dumps(message)
        
        # Iterate over copy to allow safe removal during iteration if needed
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json_msg)
            except WebSocketDisconnect:
                self.disconnect(connection)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                self.disconnect(connection)

# Singleton instance
manager = ConnectionManager()