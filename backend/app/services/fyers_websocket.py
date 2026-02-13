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
        self.on_tick_handler: Optional[Callable] = None

    def connect(self):
        """Initialize WebSocket connection using access token"""
        if not data_ws:
            raise Exception("fyers-apiv3 not installed")
        
        # Load access token from unified client
        from .fyers_client import get_fyers_client
        fyers_client = get_fyers_client()
        
        client_id = fyers_client.client_id
        access_token = fyers_client.access_token
        
        if not client_id or not access_token:
            raise Exception("Fyers credentials not found. Please login first.")
        
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
            
            # 2. Call global handler (LiveMarketService)
            if self.on_tick_handler:
                try:
                    self.on_tick_handler(message)
                except Exception as e:
                    print(f"[FyersWS] Tick handler error: {e}")

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
