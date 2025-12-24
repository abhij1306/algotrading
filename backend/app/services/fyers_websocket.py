"""
Fyers WebSocket Service
Handles live market data streaming using fyers-apiv3 WebSocket
"""
import os
import json
from typing import Dict, List, Callable, Optional
from datetime import datetime
import asyncio
import threading
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
        
        # Capture the main event loop for thread-safe broadcasting
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
            print("[FyersWS] Warning: No running event loop captured during init.")

    def connect(self):
        """Initialize WebSocket connection using access token"""
        if not data_ws:
            raise Exception("fyers-apiv3 not installed")
        
        # Load access token from file
        token_file = os.path.join(os.path.dirname(__file__), "..", "..", "fyers", "config", "access_token.json")
        if not os.path.exists(token_file):
            # Try alternate path
            token_file = os.path.join(os.getcwd(), "fyers", "config", "access_token.json")
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

        # Update loop if not set (e.g., if connect called from main thread later)
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
    
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
            if Manager and self.loop:
                # Thread-safe broadcast
                asyncio.run_coroutine_threadsafe(Manager.broadcast(message), self.loop)
            elif Manager:
                 # Fallback: if no loop captured (rare in FastAPI), try to get one or warn
                 # This part is risky but kept for edge cases
                 try:
                     loop = asyncio.get_event_loop()
                     if loop.is_running():
                         loop.create_task(Manager.broadcast(message))
                 except Exception:
                     pass # Fail silently to avoid crashing the socket thread

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
