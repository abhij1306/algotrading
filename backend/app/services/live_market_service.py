
import os
import asyncio
import datetime
import logging
from typing import List, Optional
import pytz
from .fyers_websocket import get_websocket_service, FyersWebSocketService
from .symbol_master import symbol_master
from ..utils.ws_manager import manager

logger = logging.getLogger(__name__)

# IST Timezone
IST = pytz.timezone('Asia/Kolkata')
MARKET_OPEN_TIME = datetime.time(9, 15)
MARKET_CLOSE_TIME = datetime.time(15, 30)

class LiveMarketService:
    """
    Orchestrates live market data flow.
    Enforces Market Hours (9:15 - 15:30 IST).
    """
    
    def __init__(self):
        self.ws_service: Optional[FyersWebSocketService] = None
        self._market_status = "UNKNOWN"
        self.tick_buffer = {}
        self.latest_values = {}
        self.loop = None
        self.broadcast_task = None
        self.dev_mode = os.getenv("DEV_MODE", "False").lower() == "true"

    def is_market_open(self) -> bool:
        """Check if current IST time is within market hours"""
        if self.dev_mode:
            self._market_status = "OPEN (DEV)"
            return True

        now = datetime.datetime.now(IST)
        # Weekends check
        if now.weekday() >= 5: # 5=Sat, 6=Sun
            self._market_status = "CLOSED_WEEKEND"
            return False
            
        current_time = now.time()
        is_open = MARKET_OPEN_TIME <= current_time <= MARKET_CLOSE_TIME
        self._market_status = "OPEN" if is_open else "CLOSED_OFF_HOURS"
        return is_open

    async def _update_buffer(self, tick):
        """Async method to update buffer on main loop"""
        # Symbol is already converted to DB format by handle_tick_incoming
        symbol = tick.get("symbol")
        self.tick_buffer[symbol] = tick
        self.latest_values[symbol] = tick

    async def _flush_loop(self):
        """Background task to flush buffered ticks every 1s"""
        try:
            while True:
                await asyncio.sleep(1)
                if not self.tick_buffer:
                    continue
                
                # Atomic Swap: Safe and prevents data loss
                batch = self.tick_buffer
                self.tick_buffer = {}
                
                # Broadcast individual updates (throttled)
                for symbol, tick in batch.items():
                    # Format as per User Contract: {"type": "ticker", "data": ...}
                    msg = {"type": "ticker", "data": tick}
                    await manager.broadcast(msg)
        except asyncio.CancelledError:
            logger.info("Broadcast flush loop cancelled")
        except Exception as e:
            logger.error(f"Error in flush loop: {e}")

    def handle_tick_incoming(self, tick):
        """Entry point for ticks from Fyers Thread"""
        if self.loop and not self.loop.is_closed():
            try:
                # Convert symbol to DB_FORMAT before buffering
                fyers_symbol = tick.get("symbol")
                if fyers_symbol:
                    db_symbol = symbol_master.to_db(fyers_symbol)
                    tick_db = tick.copy()
                    tick_db["symbol"] = db_symbol
                    asyncio.run_coroutine_threadsafe(self._update_buffer(tick_db), self.loop)
            except Exception as e:
                logger.error(f"Error processing incoming tick: {e}")

    def connect(self, loop=None):
        """Connect to external data provider if market is open"""
        # FIXED: Properly capture and set event loop
        if loop:
            self.loop = loop
        else:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("LiveMarketService connected outside async loop context")
                self.loop = None

        if self.is_market_open():
            logger.info(f"Market is OPEN ({self._market_status}). Connecting to Fyers...")
            
            # Start flush loop if not running
            if self.loop and (self.broadcast_task is None or self.broadcast_task.done()):
                self.broadcast_task = self.loop.create_task(self._flush_loop())

            try:
                self.ws_service = get_websocket_service()
                
                # CRITICAL FIX: Set event loop for WebSocket BEFORE connecting
                if self.loop:
                    self.ws_service.set_loop(self.loop)
                    logger.info("✅ Event loop set for WebSocket service")
                
                # Register handlers
                self.ws_service.on_tick_handler = self.handle_tick_incoming

                # Check if already connected
                if self.ws_service.ws and hasattr(self.ws_service.ws, 'is_connected') and self.ws_service.ws.is_connected():
                    logger.info("✅ WebSocket already connected")
                else:
                    # Run connection in a separate thread to avoid blocking startup
                    import threading
                    threading.Thread(target=self.ws_service.connect, daemon=True).start()
                    logger.info("🚀 WebSocket connection started in background thread")
                      
            except Exception as e:
                logger.error(f"Failed to connect to Fyers: {e}")
        else:
            logger.info(f"Market is CLOSED ({self._market_status}). Skipping Fyers connection.")
            # Ensure broadcast loop is stopped if market closed? 
            # Ideally yes, but keeping it ensures we don't leak tasks.
            if self.broadcast_task and not self.broadcast_task.done():
                self.broadcast_task.cancel()

    async def subscribe(self, symbols: List[str]):
        """Subscribe to symbols non-blocking"""
        if not self.ws_service:
            if self.is_market_open():
                self.connect()
            else:
                logger.warning("Cannot subscribe: Market is CLOSED.")
                return

        if self.ws_service and self.ws_service.ws and self.ws_service.ws.is_connected():
            try:
                # Convert to Fyers format
                fyers_symbols = symbol_master.batch_to_fyers(symbols)

                # CRITICAL FIX: Run blocking SDK call in executor
                await asyncio.get_running_loop().run_in_executor(
                    None, 
                    self.ws_service.subscribe, 
                    fyers_symbols
                )
                logger.info(f"Subscribed to {len(fyers_symbols)} symbols")
            except Exception as e:
                logger.error(f"Fyers subscription failed: {e}")
        else:
            logger.warning("Fyers WebSocket not connected. Subscription queued.")

    async def unsubscribe(self, symbols: List[str]):
        """Unsubscribe non-blocking"""
        if self.ws_service and self.ws_service.ws and self.ws_service.ws.is_connected():
            try:
                fyers_symbols = symbol_master.batch_to_fyers(symbols)
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    self.ws_service.unsubscribe,
                    fyers_symbols
                )
            except Exception as e:
                logger.error(f"Fyers unsubscription failed: {e}")

    def get_status(self):
        return {
            "market_status": self._market_status,
            "fyers_connected": (self.ws_service is not None and 
                                self.ws_service.ws is not None and 
                                self.ws_service.ws.is_connected())
        }

    def get_latest_tick(self, symbol: str) -> Optional[dict]:
        """
        Get latest tick for symbol
        Returns cached live tick if available.
        """
        return self.latest_values.get(symbol)

    def get_latest_ticks(self, symbols: List[str]) -> dict:
        """Get latest ticks for multiple symbols"""
        return {
            symbol: self.latest_values.get(symbol)
            for symbol in symbols
            if symbol in self.latest_values
        }

# Singleton
live_market = LiveMarketService()
