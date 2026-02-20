
import asyncio
import datetime
import logging
import os

import pytz

from ..utils.ws_manager import manager
from .fyers_websocket import FyersWebSocketService, get_websocket_service
from .symbol_master import symbol_master

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
        self.ws_service: FyersWebSocketService | None = None
        self._market_status = "UNKNOWN"
        self.tick_buffer = {}
        self.latest_values = {}
        self.loop = None
        self.broadcast_task = None
        self.monitor_task = None
        self.dev_mode = os.getenv("DEV_MODE", "False").lower() == "true"
        # Connection state tracking
        self.ws_connected = False
        self._is_connecting = False
        self._ws_thread = None
        self.pending_subscriptions = set()

    def on_ws_connected(self):
        """Callback when WebSocket connection is established"""
        self.ws_connected = True
        self._is_connecting = False
        logger.info("[OK] WebSocket connection established")

        # Process pending subscriptions
        if self.pending_subscriptions:
            symbols = list(self.pending_subscriptions)
            self.pending_subscriptions.clear()
            logger.info(f"Processing {len(symbols)} pending subscriptions: {symbols}")
            # We use create_task because this is called from the thread runner via on_ws_connected
            if self.loop and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(self.subscribe(symbols), self.loop)

    def on_ws_failure(self, error: Exception):
        """Callback when WebSocket connection fails"""
        self.ws_connected = False
        self._is_connecting = False
        logger.error(f"[ERROR] WebSocket connection failed: {error}")

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
        """Background task to flush buffered ticks every 200ms for low-latency updates"""
        try:
            while True:
                await asyncio.sleep(0.2)
                if not self.tick_buffer:
                    continue

                # Atomic Swap: Safe and prevents data loss
                batch = self.tick_buffer
                self.tick_buffer = {}

                # Broadcast BATCH update (Significantly reduces overhead)
                # Payload: {"type": "ticker_batch", "data": [tick1, tick2, ...]}
                if batch:
                    msg = {"type": "ticker_batch", "data": list(batch.values())}
                    await manager.broadcast(msg)

        except asyncio.CancelledError:
            logger.info("Broadcast flush loop cancelled")
        except Exception as e:
            logger.error(f"Error in flush loop: {e}")

    async def _monitor_loop(self):
        """
        Background task to monitor connection health.
        Reconnects if connection drops during market hours.
        Inspired by OpenAlgo's connection pooling and failover logic.
        """
        try:
            while True:
                await asyncio.sleep(30) # Check every 30s

                if self.is_market_open():
                    is_connected = False
                    if self.ws_service and self.ws_service.ws:
                        try:
                            # Safely check connection status
                            if hasattr(self.ws_service.ws, 'is_connected'):
                                is_connected = self.ws_service.ws.is_connected()
                            elif hasattr(self.ws_service.ws, 'is_open'):
                                is_connected = self.ws_service.ws.is_open
                        except Exception:
                            is_connected = False

                    if not is_connected:
                        if self.ws_connected:
                            logger.info("[WS] Connection dropped during market hours. Triggering reconnect...")
                            self.ws_connected = False
                            self.connect()
                        elif not self._is_connecting:
                            logger.debug("[WS] Market is open but not connected. Triggering connection...")
                            self.connect()
                    elif not self.ws_connected:
                        # Case where it's connected but our flag is false
                        self.ws_connected = True
                else:
                    # Ensure we are disconnected off-hours
                    if self.ws_connected or self._is_connecting:
                        logger.info("[WS] Market closed. Cleaning up connection.")
                        if self.ws_service:
                            self.ws_service.disconnect()
                        self.ws_connected = False
                        self._is_connecting = False

        except asyncio.CancelledError:
            logger.info("Connection monitor cancelled")
        except Exception as e:
            logger.error(f"Error in monitor loop: {e}")

    def handle_tick_incoming(self, tick):
        """Entry point for ticks from Fyers Thread - buffer for batch broadcast"""
        if self.loop and not self.loop.is_closed():
            try:
                # Convert symbol to DB_FORMAT before buffering
                fyers_symbol = tick.get("symbol")
                if fyers_symbol:
                    db_symbol = symbol_master.to_db(fyers_symbol)
                    tick_db = tick.copy()
                    tick_db["symbol"] = db_symbol

                    # Ensure change_pct is calculated if not provided
                    # Fyers sends 'chp' (change percent) field
                    if 'chp' in tick_db:
                        tick_db['change_pct'] = tick_db['chp']
                    elif 'ltp' in tick_db and 'prev_close_price' in tick_db:
                        # Calculate change_pct: (current - prev_close) / prev_close * 100
                        ltp = tick_db['ltp']
                        prev_close = tick_db['prev_close_price']
                        if prev_close and prev_close > 0:
                            tick_db['change_pct'] = ((ltp - prev_close) / prev_close) * 100

                    # Normalize absolute change for consumers expecting `change`
                    if 'ch' in tick_db and 'change' not in tick_db:
                        tick_db['change'] = tick_db['ch']

                    # Ensure volume is passed through (Fyers sends 'volume' or 'v')
                    if 'v' in tick_db and 'volume' not in tick_db:
                        tick_db['volume'] = tick_db['v']
                    elif 'vol_traded_today' in tick_db and 'volume' not in tick_db:
                        tick_db['volume'] = tick_db['vol_traded_today']

                    # Update latest values cache
                    self.latest_values[db_symbol] = tick_db

                    # Buffer for batch broadcast (removed immediate broadcast to avoid duplicates)
                    asyncio.run_coroutine_threadsafe(self._update_buffer(tick_db), self.loop)
            except Exception as e:
                logger.error(f"Error processing incoming tick: {e}")

    def connect(self, loop=None):
        """Connect to external data provider if market is open and token is valid"""
        # 1. Capture and set event loop
        if loop:
            self.loop = loop
        elif not self.loop:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("LiveMarketService connected outside async loop context")
                self.loop = None

        # 2. Start monitor loop if not running
        if self.loop and (self.monitor_task is None or self.monitor_task.done()):
            self.monitor_task = self.loop.create_task(self._monitor_loop())
        # Start broadcast flush loop if not running.
        if self.loop and (self.broadcast_task is None or self.broadcast_task.done()):
            self.broadcast_task = self.loop.create_task(self._flush_loop())

        # 4. Check if already connecting or connected
        if self._is_connecting or self.ws_connected:
            return

        # 5. Validate Fyers Token
        from .fyers_client import get_fyers_client
        fyers = get_fyers_client()
        if not fyers or not fyers.validate_token():
            logger.warning("Cannot connect WebSocket: Fyers token is invalid or expired.")
            return

        # 6. Check Market Hours
        if self.is_market_open():
            logger.info(f"Market is OPEN ({self._market_status}). Connecting to Fyers...")

            try:
                self._is_connecting = True
                self.ws_service = get_websocket_service()

                # CRITICAL FIX: Set event loop for WebSocket BEFORE connecting
                if self.loop:
                    self.ws_service.set_loop(self.loop)
                    logger.info("[OK] Event loop set for WebSocket service")

                # Register handlers
                self.ws_service.on_tick_handler = self.handle_tick_incoming

                # Check if already connected
                if self.ws_service.ws and hasattr(self.ws_service.ws, 'is_connected') and self.ws_service.ws.is_connected():
                    logger.info("[OK] WebSocket already connected")
                    self.ws_connected = True
                    self._is_connecting = False
                else:
                    # Run connection in a separate thread to avoid blocking startup
                    import threading

                    def ws_thread_runner():
                        """Thread runner that catches exceptions and reports status with retry logic (Exponential Backoff)"""
                        max_retries = 10
                        retry_count = 0
                        while retry_count < max_retries:
                            try:
                                logger.debug(f"[WS] Connection attempt {retry_count + 1}/{max_retries}")
                                self.ws_service.connect()

                                # If we get here, connection was successful
                                # Manually update state as the thread might not call back immediately
                                if hasattr(self.ws_service.ws, 'is_connected') and self.ws_service.ws.is_connected():
                                     self.on_ws_connected()
                                     break

                            except Exception as e:
                                retry_count += 1
                                logger.error(f"[WS] Connection attempt {retry_count} failed: {e}")

                                if retry_count < max_retries:
                                    import time
                                    # Exponential Backoff: 5, 10, 20, 40, 60...
                                    delay = min(5 * (2 ** (retry_count - 1)), 60)
                                    logger.debug(f"[WS] Reconnection attempt in {delay} seconds...")
                                    time.sleep(delay)
                                else:
                                    self.on_ws_failure(e)

                    self._ws_thread = threading.Thread(target=ws_thread_runner, daemon=True)
                    self._ws_thread.start()
                    logger.info("[START] WebSocket connection started in background thread")

            except Exception as e:
                self._is_connecting = False
                logger.error(f"Failed to connect to Fyers: {e}")
        else:
            logger.info(f"Market is CLOSED ({self._market_status}). Skipping Fyers connection for now.")

    async def subscribe(self, symbols: list[str]):
        """Subscribe to symbols non-blocking"""
        symbols = list(set(symbol_master.to_db(s) for s in symbols if s))
        if not self.ws_service:
            if self.is_market_open():
                self.connect()
            else:
                logger.warning("Cannot subscribe: Market is CLOSED.")
                return

        if self.ws_service and self.ws_service.ws and self.ws_service.ws.is_connected():
            try:
                # Convert to Fyers format one-by-one to avoid rejecting the whole batch.
                fyers_symbols: list[str] = []
                invalid_symbols: list[str] = []
                for symbol in symbols:
                    try:
                        fyers_symbols.append(symbol_master.to_fyers(symbol))
                    except Exception:
                        invalid_symbols.append(symbol)

                if invalid_symbols:
                    logger.warning("Skipping invalid symbols for provider subscribe: %s", invalid_symbols)

                if not fyers_symbols:
                    return

                # CRITICAL FIX: Run blocking SDK call in executor
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    self.ws_service.subscribe,
                    fyers_symbols
                )
                logger.info(f"Subscribed to {len(fyers_symbols)} symbols")
            except Exception as e:
                logger.error(f"Fyers subscription failed: {e}")
        else:
            # Add to pending subscriptions
            self.pending_subscriptions.update(symbols)
            logger.warning(f"Fyers WebSocket not connected. {len(symbols)} symbols queued for subscription.")

    async def unsubscribe(self, symbols: list[str]):
        """Unsubscribe non-blocking"""
        symbols_set = set(symbol_master.to_db(s) for s in symbols if s)
        if symbols_set:
            self.pending_subscriptions.difference_update(symbols_set)

        if self.ws_service and self.ws_service.ws and self.ws_service.ws.is_connected():
            try:
                fyers_symbols: list[str] = []
                for symbol in symbols_set:
                    try:
                        fyers_symbols.append(symbol_master.to_fyers(symbol))
                    except Exception:
                        continue
                if not fyers_symbols:
                    return
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
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

    def get_latest_tick(self, symbol: str) -> dict | None:
        """
        Get latest tick for symbol
        Returns cached live tick if available.
        """
        return self.latest_values.get(symbol)

    def get_latest_ticks(self, symbols: list[str]) -> dict:
        """Get latest ticks for multiple symbols"""
        return {
            symbol: self.latest_values.get(symbol)
            for symbol in symbols
            if symbol in self.latest_values
        }

# Singleton
live_market = LiveMarketService()

def get_live_market_service() -> LiveMarketService:
    """Get the singleton LiveMarketService instance."""
    return live_market
