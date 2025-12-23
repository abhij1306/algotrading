"""
Fyers WebSocket Service
Handles live market data streaming using fyers-apiv3 WebSocket
"""
import os
import json
from typing import Dict, List, Callable
from datetime import datetime
import asyncio
from ..utils.ws_manager import manager as Manager

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None
    print("[FyersWS] fyers-apiv3 not installed. WebSocket features unavailable.")

class FyersWebSocketService:
    """
    Manages Fyers WebSocket connections for live tick data
    """
    
    def __init__(self):
        self.ws = None
        self.access_token = None
        self.subscribed_symbols = set()
        self.callbacks: Dict[str, List[Callable]] = {}
        
    def connect(self):
        """Initialize WebSocket connection using access token"""
        if not data_ws:
            raise Exception("fyers-apiv3 not installed")
        
        # Load access token from file
        token_file = os.path.join(os.path.dirname(__file__), "..", "..", "fyers_access_token.json")
        if not os.path.exists(token_file):
            raise Exception("Fyers access token not found. Please login first.")
        
        with open(token_file, 'r') as f:
            token_data = json.load(f)
        
        client_id = token_data.get("client_id")
        access_token = token_data.get("access_token")
        
        if not client_id or not access_token:
            raise Exception("Invalid token data")
        
        # Create WebSocket instance
        self.access_token = f"{client_id}:{access_token}"
        self.ws = data_ws.FyersDataSocket(
            access_token=self.access_token,
            log_path="",
            litemode=False  # Full mode for OHLCV data
        )
        
        # Assign callbacks
        self.ws.on_message = self._on_message
        self.ws.on_error = self._on_error
        self.ws.on_close = self._on_close
        self.ws.on_open = self._on_open
        
        # Connect
        self.ws.connect()
        print("[FyersWS] WebSocket connected")
    
    def subscribe(self, symbols: List[str], callback: Callable = None):
        """
        Subscribe to symbols for live data
        Args:
            symbols: List of symbols in Fyers format (e.g., ["NSE:SBIN-EQ", "NSE:INFY-EQ"])
            callback: Optional callback function to receive tick data
        """
        if not self.ws:
            raise Exception("WebSocket not connected. Call connect() first.")
        
        # Add symbols to subscription set
        self.subscribed_symbols.update(symbols)
        
        # Subscribe via WebSocket
        self.ws.subscribe(symbols=symbols, data_type="SymbolUpdate")
        
        # Register callback
        if callback:
            for symbol in symbols:
                if symbol not in self.callbacks:
                    self.callbacks[symbol] = []
                self.callbacks[symbol].append(callback)
        
        print(f"[FyersWS] Subscribed to {len(symbols)} symbols")
    
    def unsubscribe(self, symbols: List[str]):
        """Unsubscribe from symbols"""
        if not self.ws:
            return
        
        self.ws.unsubscribe(symbols=symbols)
        self.subscribed_symbols -= set(symbols)
        
        for symbol in symbols:
            if symbol in self.callbacks:
                del self.callbacks[symbol]
    
    async def _broadcast_to_clients(self, message):
        """Helper to broadcast async from sync callback"""
        if Manager:
            await Manager.broadcast(message)

    def _on_message(self, message):
        """Handle incoming WebSocket message"""
        try:
            # message format: {"symbol": "NSE:SBIN-EQ", "ltp": 500.0, "ch": 2.5, ...}
            symbol = message.get("symbol")
            
            # 1. Call registered internal callbacks (Strategies)
            if symbol in self.callbacks:
                for callback in self.callbacks[symbol]:
                    try:
                        callback(message)
                    except Exception as e:
                        print(f"[FyersWS] Callback error: {e}")
            
            # 2. PROD: Broadcast to frontend clients via WebSocket Manager
            # We need to run async broadcast from this sync callback
            if Manager:
                try:
                    # Create task for broadcasting to avoid blocking
                    # Note: Fyers WS runs in its own thread, usually. 
                    # We need an event loop to run async manager.broadcast
                    
                    # Simplest approach for production-grade bridge:
                    # Fire-and-forget broadcast slightly separated
                    
                    # Using asyncio.run_coroutine_threadsafe if loop is available,
                    # or creating a new loop if needed.
                    # Since this runs inside FastAPI (mostly), we can try finding running loop.
                    
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(Manager.broadcast(message))
                        else:
                            # Fallback just in case
                            loop.run_until_complete(Manager.broadcast(message))
                    except RuntimeError:
                        # If no loop in this thread
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(Manager.broadcast(message))
                        
                except Exception as e:
                    print(f"[FyersWS] Broadcast error: {e}")

            # Print for debugging (optional - reduce noise in prod)
            # print(f"[FyersWS] {symbol}: LTP={message.get('ltp')}")
        
        except Exception as e:
            print(f"[FyersWS] Error processing message: {e}")

    def _on_error(self, error):
        """Handle WebSocket error"""
        print(f"[FyersWS] Error: {error}")
    
    def _on_close(self, message):
        """Handle WebSocket close"""
        print(f"[FyersWS] Connection closed: {message}")
    
    def _on_open(self):
        """Handle WebSocket open"""
        print("[FyersWS] Connection opened")
    
    def disconnect(self):
        """Close WebSocket connection"""
        if self.ws:
            self.ws.close()
            self.ws = None
            self.subscribed_symbols.clear()
            self.callbacks.clear()
            print("[FyersWS] Disconnected")


# Global singleton instance
_ws_instance = None

def get_websocket_service() -> FyersWebSocketService:
    """Get or create WebSocket service instance"""
    global _ws_instance
    if _ws_instance is None:
        _ws_instance = FyersWebSocketService()
    return _ws_instance
