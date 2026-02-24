"""
Fyers WebSocket Service
Handles live market data streaming using fyers-apiv3 WebSocket

FIXED: Thread-safe message handling with event loop integration
"""

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None
    logger.warning("[FyersWS] fyers-apiv3 not installed. WebSocket features unavailable.")


class FyersWebSocketService:
    """
    Manages Fyers WebSocket connections for live tick data.
    Streamlined for performance: Direct callback dispatch (no internal queuing).
    Thread safety is delegated to the consumer (LiveMarketService).
    """

    def __init__(self):
        self.ws = None
        self.access_token = None
        self.subscribed_symbols = set()
        self.callbacks: dict[str, list[Callable]] = {}
        self.on_tick_handler: Callable | None = None
        self.max_symbols_per_call = 25

    def _chunk(self, items: list[str], size: int) -> list[list[str]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """
        API Compatibility Stub.
        Loop management is now handled by the consumer (LiveMarketService) directly.
        """
        pass

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
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
            reconnect_retry=10,
        )

        # Connect (blocking call - should be run in thread)
        self.ws.connect()
        logger.info("[FyersWS] WebSocket connected")

    def subscribe(self, symbols: list[str], callback: Callable = None):
        """
        Subscribe to symbols for live data
        """
        if not self.ws:
            raise Exception("WebSocket not connected. Call connect() first.")

        unique_symbols = sorted({s for s in symbols if s})
        if not unique_symbols:
            return

        # Add symbols to subscription set
        self.subscribed_symbols.update(unique_symbols)

        # Subscribe via WebSocket in chunks (prevents provider payload/rejection issues)
        for batch in self._chunk(unique_symbols, self.max_symbols_per_call):
            self.ws.subscribe(symbols=batch, data_type="SymbolUpdate")

        # Register callback
        if callback:
            for symbol in unique_symbols:
                if symbol not in self.callbacks:
                    self.callbacks[symbol] = []
                self.callbacks[symbol].append(callback)

        logger.info("[FyersWS] Subscribed to %s symbols", len(unique_symbols))

    def unsubscribe(self, symbols: list[str]):
        """Unsubscribe from symbols"""
        if not self.ws:
            return

        unique_symbols = sorted({s for s in symbols if s})
        if not unique_symbols:
            return

        for batch in self._chunk(unique_symbols, self.max_symbols_per_call):
            self.ws.unsubscribe(symbols=batch)
        self.subscribed_symbols -= set(unique_symbols)

        for symbol in unique_symbols:
            if symbol in self.callbacks:
                del self.callbacks[symbol]

    def _on_message(self, message):
        """
        Handle incoming WebSocket message (called from WebSocket thread)
        Directly invokes handler to minimize latency.
        """
        try:
            symbol = message.get("symbol")

            # 1. Call registered callbacks (Strategy-specific)
            if symbol in self.callbacks:
                for callback in self.callbacks[symbol]:
                    try:
                        callback(message)
                    except Exception as e:
                        logger.error("[FyersWS] Callback error: %s", e)

            # 2. Main Tick Handler (LiveMarketService)
            if self.on_tick_handler:
                try:
                    self.on_tick_handler(message)
                except Exception as e:
                    logger.error("[FyersWS] Main handler error: %s", e)

        except Exception as e:
            logger.error("[FyersWS] Error processing message: %s", e)

    def _on_error(self, error):
        """Handle WebSocket error"""
        try:
            # Log actual errors at ERROR level with context
            logger.error("[FyersWS] ERROR: %s", str(error))
        except Exception:
            logger.error("[FyersWS] ERROR: <unicode error>")

    def _on_close(self, message):
        """Handle WebSocket close"""
        try:
            # Normal close is logged at INFO level
            logger.info("[FyersWS] Connection closed: %s", str(message))
        except Exception:
            logger.info("[FyersWS] Connection closed")

    def _on_open(self):
        """Handle WebSocket open"""
        logger.info("[FyersWS] Connection established")

    def disconnect(self):
        """Close WebSocket connection"""
        if self.ws:
            self.ws.close()
            self.ws = None
            self.subscribed_symbols.clear()
            self.callbacks.clear()
            logger.info("[FyersWS] Disconnected")


# Global singleton instance
_ws_instance = None


def get_websocket_service() -> FyersWebSocketService:
    """Get or create WebSocket service instance"""
    global _ws_instance
    if _ws_instance is None:
        _ws_instance = FyersWebSocketService()
    return _ws_instance
