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
        """
        Initialize LiveMarketService instance and set up its internal state.
        
        Attributes:
            ws_service: Optional FyersWebSocketService used to manage the websocket connection; None until connected.
            _market_status (str): Current market status label, initialized to "UNKNOWN".
            tick_buffer (dict): Temporary in-memory buffer mapping symbol -> latest tick pending broadcast.
            latest_values (dict): Cache mapping symbol -> most recent tick received (DB-formatted symbol keys).
            loop: Reference to the asyncio event loop used for scheduling tasks; None until set.
            broadcast_task: Background asyncio Task that flushes tick_buffer; None until started.
            dev_mode (bool): True when running in development mode (controlled by DEV_MODE env var).
        """
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
        """
        Store an incoming tick in the in-memory buffer and update the latest-tick cache.
        
        Parameters:
            tick (dict): Market tick object containing a `symbol` key (expected in DB format). The tick will be placed into the per-symbol buffer and recorded as the most recent value for that symbol.
        """
        # Symbol is already converted to DB format by handle_tick_incoming
        symbol = tick.get("symbol")
        self.tick_buffer[symbol] = tick
        self.latest_values[symbol] = tick

    async def _flush_loop(self):
        """
        Continuously flushes buffered ticks once per second and broadcasts each tick.
        
        Runs an infinite loop that, every second, atomically swaps out the current tick buffer and broadcasts each tick as a message of the form `{"type": "ticker", "data": <tick>}` via `manager.broadcast`. The loop runs until cancelled; cancellation is handled gracefully by logging a shutdown message.
        """
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
        """
        Handle an incoming tick from the Fyers thread, convert its symbol to DB format, and schedule it for buffering.
        
        Parameters:
            tick (dict): Tick payload received from Fyers; expected to include a "symbol" key with the Fyers-format symbol.
        """
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
        """
        Ensure the service is connected to the external market data provider when the market is open.
        
        If a running event loop is provided or found, the method uses it for scheduling background tasks. When the market is open this will:
        - start the internal broadcast flush loop if not already running,
        - obtain and configure the WebSocket service, registering the instance's tick handler,
        - initiate the WebSocket connection in a background thread if not already connected.
        
        If the market is closed the method will skip connecting and cancel the broadcast task if it is running.
        
        Parameters:
            loop (asyncio.AbstractEventLoop | None): Optional event loop to use for scheduling background tasks; if None the method will try to use the currently running loop and will log a warning if none is available.
        """
        # Capture the running loop for thread-safe operations
        if loop:
            self.loop = loop
        else:
            try:
                 self.loop = asyncio.get_running_loop()
            except RuntimeError:
                 logger.warning("LiveMarketService connected outside async loop context? Broadcasts might fail.")

        if self.is_market_open():
            logger.info(f"Market is OPEN ({self._market_status}). Connecting to Fyers...")
            
            # Start flush loop if not running
            if self.broadcast_task is None or self.broadcast_task.done():
                self.broadcast_task = self.loop.create_task(self._flush_loop())

            try:
                self.ws_service = get_websocket_service()
                
                # Register Global Handler
                # Support both naming conventions used by other agents
                self.ws_service.message_handler = self.handle_tick_incoming
                self.ws_service.on_tick_handler = self.handle_tick_incoming

                # Check if already connected
                if self.ws_service.ws and hasattr(self.ws_service.ws, 'is_connected') and self.ws_service.ws.is_connected():
                     logger.info("Fyers WebSocket already connected.")
                else:
                     # Run connection in a separate thread to avoid blocking startup
                     import threading
                     threading.Thread(target=self.ws_service.connect, daemon=True).start()
                     
            except Exception as e:
                logger.error(f"Failed to connect to Fyers: {e}")
        else:
            logger.info(f"Market is CLOSED ({self._market_status}). Skipping Fyers connection.")
            # Ensure broadcast loop is stopped if market closed? 
            # Ideally yes, but keeping it ensures we don't leak tasks.
            if self.broadcast_task and not self.broadcast_task.done():
                self.broadcast_task.cancel()

    async def subscribe(self, symbols: List[str]):
        """
        Subscribe to a list of symbols on the Fyers websocket service.
        
        Converts the provided symbols to Fyers format and issues a non-blocking subscription request on the websocket. If the market is closed the method returns without subscribing. If the websocket is not connected the subscription is not sent and a warning is logged.
        
        Parameters:
            symbols (List[str]): Symbols to subscribe for (in DB/internal format); they will be converted to Fyers format before subscribing.
        """
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
        """
        Unsubscribe the provided symbols from the Fyers WebSocket feed without blocking the event loop.
        
        Parameters:
            symbols (List[str]): List of symbols (in DB/standard symbol format expected by the service) to unsubscribe.
        
        Notes:
            If the WebSocket is not connected the function does nothing. Errors during unsubscription are logged.
        """
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
        """
        Return current service status including market state and WebSocket connection.
        
        Returns:
            status (dict): Mapping with keys:
                - "market_status": Current market status string.
                - "fyers_connected": `True` if a Fyers WebSocket instance exists and its underlying websocket reports connected, `False` otherwise.
        """
        return {
            "market_status": self._market_status,
            "fyers_connected": (self.ws_service is not None and 
                                self.ws_service.ws is not None and 
                                self.ws_service.ws.is_connected())
        }

    def get_latest_tick(self, symbol: str) -> Optional[dict]:
        """
        Retrieve the most recent cached tick for a symbol.
        
        Returns:
            dict: The latest tick for the given symbol, or `None` if no tick is cached.
        """
        return self.latest_values.get(symbol)

    def get_latest_ticks(self, symbols: List[str]) -> dict:
        """
        Return the cached latest tick for each requested symbol that is available.
        
        Parameters:
            symbols (List[str]): Sequence of symbol identifiers to query (keys expected in the service's cache).
        
        Returns:
            dict: Mapping from each requested symbol (only those present in the cache) to its latest tick dictionary.
        """
        return {
            symbol: self.latest_values.get(symbol)
            for symbol in symbols
            if symbol in self.latest_values
        }

# Singleton
live_market = LiveMarketService()