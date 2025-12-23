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
        self.active_connections: List[WebSocket] = []
        
        # Subscriptions mapping: symbol -> list of websockets (optimization)
        # For now, we'll broadcast all to all (simple pub-sub) for simplicity
        # Optimization can be added if traffic becomes huge
        
    async def connect(self, websocket: WebSocket):
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
