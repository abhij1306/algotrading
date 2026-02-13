"""
Fyers WebSocket Service
Handles live market data streaming using fyers-apiv3 WebSocket

FIXED: Thread-safe message handling with event loop integration
"""
import asyncio
import threading
from typing import Dict, List, Callable, Optional, Any, Set
from collections import deque

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None
    print("[FyersWS] fyers-apiv3 not installed. WebSocket features unavailable.")


class FyersWebSocketService:
    """
    Manages Fyers WebSocket connections for live tick data
    
    FIXED: Thread-safe message queueing and event loop integration
    """
    
    # Class-level message queue for thread safety
    _message_queue: deque = deque(maxlen=1000)
    _queue_lock = threading.Lock()
    
    def __init__(self):
        self.ws = None
        self.access_token = None
        self.subscribed_symbols = set()
        self.callbacks: Dict[str, List[Callable]] = {}
        self.on_tick_handler: Optional[Callable] = None
        
        # ADDED: Event loop for async operations
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """
        Set event loop for thread-safe broadcasting
        CRITICAL: Call this from main async context before starting WebSocket
        
        Args:
            loop: The asyncio event loop from the main thread
        """
        self.loop = loop
        print(f"[FyersWS] Event loop set: {loop is not None}")
        
        # Process any queued messages
        if loop and not loop.is_closed():
            self._process_queued_messages()

    def connect(self):
        """Initialize WebSocket connection using access token"""
        if not data_ws:
            raise Exception("fyers-apiv3 not installed")
        
        # Load access token from unified client
        from .fyers_client import get_fyers_client
        fyers_client = get_fyers_client()
        
        if fyers_client is None:
            raise Exception("Fyers credentials not found. Please login first.")

        client_id = fyers_client.client_id
        access_token = fyers_client.access_token
        
        if not client_id or not access_token:
            raise Exception("Fyers credentials not found. Please login first.")
        
        # Create WebSocket instance
        self.access_token = f"{client_id}:{access_token}"
        
        # Prevent log spam by using empty log path
        self.ws = data_ws.FyersDataSocket(
            access_token=self.access_token,
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=10,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message
        )
        
        # Connect (blocking call - should be run in thread)
        self.ws.connect()
        print("[FyersWS] WebSocket connected")
    
    def subscribe(self, symbols: List[str], callback: Callable = None):
        """
        Subscribe to symbols for live data
        Args:
            symbols: List of symbols in Fyers format (e.g., ["NSE:SBIN-EQ"])
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
        """
        Handle incoming WebSocket message (called from WebSocket thread)
        FIXED: Thread-safe queueing instead of direct async calls
        """
        try:
            symbol = message.get("symbol")
            
            # 1. Call registered callbacks (Strategy-specific)
            if symbol in self.callbacks:
                for callback in self.callbacks[symbol]:
                    try:
                        callback(message)
                    except Exception as e:
                        print(f"[FyersWS] Callback error: {e}")
            
            # 2. Queue message for async processing (LiveMarketService)
            if self.on_tick_handler:
                if self.loop and not self.loop.is_closed():
                    # Schedule handler in the event loop (thread-safe)
                    asyncio.run_coroutine_threadsafe(
                        self._async_tick_handler(message),
                        self.loop
                    )
                else:
                    # Queue for later if loop not available
                    with FyersWebSocketService._queue_lock:
                        FyersWebSocketService._message_queue.append(message)
                        print(f"[FyersWS] Message queued (loop unavailable). Queue size: {len(FyersWebSocketService._message_queue)}")

        except Exception as e:
            print(f"[FyersWS] Error processing message: {e}")
    
    async def _async_tick_handler(self, message):
        """Async wrapper for tick handler (runs in event loop)"""
        try:
            if self.on_tick_handler:
                self.on_tick_handler(message)
        except Exception as e:
            print(f"[FyersWS] Tick handler error: {e}")
    
    def _process_queued_messages(self):
        """Process any queued messages when loop becomes available"""
        with FyersWebSocketService._queue_lock:
            queued = list(FyersWebSocketService._message_queue)
            FyersWebSocketService._message_queue.clear()
        
        if queued and self.loop and not self.loop.is_closed():
            for msg in queued:
                asyncio.run_coroutine_threadsafe(
                    self._async_tick_handler(msg),
                    self.loop
                )
            print(f"[FyersWS] Processed {len(queued)} queued messages")

    def _on_error(self, error):
        """Handle WebSocket error"""
        print(f"[FyersWS] Error: {error}")
    
    def _on_close(self, message):
        """Handle WebSocket close"""
        print(f"[FyersWS] Connection closed: {message}")
    
    def _on_open(self):
        """Handle WebSocket open"""
        print("[FyersWS] ✅ Connection opened")
    
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
