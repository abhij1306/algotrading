from __future__ import annotations

import asyncio
import datetime
import logging
import os
import threading
import time
from time import perf_counter
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..utils.helpers import safe_float
from ..utils.logger import log_exception_throttled
from ..utils.runtime_metrics import runtime_metrics
from ..utils.ws_manager import manager
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .fyers_websocket import FyersWebSocketService

TickPayload = dict[str, Any]

# IST Timezone
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_TIME = datetime.time(9, 15)
MARKET_CLOSE_TIME = datetime.time(15, 30)

# Provider connect runner tuning.
WS_MAX_RETRIES = 10
WS_CONNECT_TIMEOUT_S = 15.0
WS_CONNECT_POLL_S = 0.1
WS_BACKOFF_BASE_S = 5.0
WS_BACKOFF_MAX_S = 60.0
# Pause after an unexpected loop error so a persistent failure cannot busy-spin.
LOOP_ERROR_PAUSE_S = 1.0
# Fyers tick fields carrying the exchange time, in epoch seconds, most precise first.
TICK_TIME_FIELDS = ("exch_feed_time", "last_traded_time")
DEFAULT_TICK_MAX_AGE_SEC = 60.0


def tick_max_age_sec() -> float:
    """Max age of a cached tick used as a live price (env PAPER_TICK_MAX_AGE_SEC)."""
    max_age = safe_float(os.getenv("PAPER_TICK_MAX_AGE_SEC"), DEFAULT_TICK_MAX_AGE_SEC)
    return max_age if max_age > 0 else DEFAULT_TICK_MAX_AGE_SEC


def tick_time(tick: TickPayload) -> float:
    """Exchange time of a tick in epoch seconds, or 0.0 when it carries none."""
    return next(
        (safe_float(tick.get(f), 0.0) for f in TICK_TIME_FIELDS if tick.get(f) is not None),
        0.0,
    )


class LiveMarketService:
    """
    Orchestrates live market data flow.
    Enforces Market Hours (9:15 - 15:30 IST).
    """

    def __init__(self) -> None:
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
        # Set by an explicit POST /disconnect; cleared only by an explicit connect.
        self._explicitly_disconnected = False
        # Symbols subscribed via the HTTP API (no ConnectionManager client owns them).
        self.api_subscriptions: set[str] = set()
        self._last_connect_block_reason: str | None = None
        self._last_subscribe_market_closed_log_ts = 0.0
        # Set by disconnect() to wake a runner that is backing off; cleared by explicit connect.
        self._disconnect_event = threading.Event()
        # True once the current connect attempt has replayed subscriptions (avoids double replay).
        self._replayed_for_attempt = False
        # Stop signal of the current connect runner thread; each runner gets a fresh event.
        self._runner_stop = threading.Event()

    def on_ws_connected(self) -> None:
        """Callback when WebSocket connection is established"""
        self._replayed_for_attempt = True
        self.ws_connected = True
        self._is_connecting = False
        runtime_metrics.increment("live_market.provider.connected")
        logger.info("[OK] WebSocket connection established")

        # Replay everything clients currently want (survives reconnects) plus anything queued.
        wanted = (
            manager.get_all_subscribed_symbols()
            | self.api_subscriptions
            | self.pending_subscriptions
        )
        self.pending_subscriptions.clear()
        if wanted:
            symbols = sorted(wanted)
            logger.info(f"Replaying {len(symbols)} subscriptions on connect")
            # We use create_task because this is called from the thread runner via on_ws_connected
            if self.loop and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(self.subscribe(symbols), self.loop)

    def on_provider_open(self) -> None:
        """SDK on-open hook: runs on the first connect and on every SDK auto-reconnect.

        The SDK invokes its on_connect callback right after (re)starting the socket, which can
        be before the handshake finished, so wait briefly for the connection before replaying.
        """
        if self._explicitly_disconnected:
            return
        if not self._wait_for_provider_connection(WS_CONNECT_TIMEOUT_S):
            logger.debug("[WS] Provider open callback fired but socket is not connected")
            return
        self.on_ws_connected()

    def _provider_is_connected(self) -> bool:
        ws = self.ws_service.ws if self.ws_service else None
        if ws is None or not hasattr(ws, "is_connected"):
            return False
        try:
            return bool(ws.is_connected())
        except Exception:
            return False

    def _wait_for_provider_connection(self, timeout: float) -> bool:
        """Poll until the provider socket reports connected; False on timeout or disconnect."""
        deadline = time.monotonic() + timeout
        while True:
            if self._explicitly_disconnected or self._runner_stop.is_set():
                return False
            if self._provider_is_connected():
                return True
            if time.monotonic() >= deadline:
                return False
            self._disconnect_event.wait(WS_CONNECT_POLL_S)

    def _attempt_provider_connect(self) -> None:
        """One provider connect attempt. Raises when the provider did not connect in time.

        Returns normally once connected, or when an explicit disconnect arrived meanwhile.
        """
        runtime_metrics.increment("live_market.provider.connect_attempts")
        self._replayed_for_attempt = False
        self.ws_service.connect()
        if self._explicitly_disconnected or self._runner_stop.is_set():
            # disconnect() or off-hours cleanup raced with connect(): no live socket left behind.
            self.ws_service.disconnect()
            return
        if not self._wait_for_provider_connection(WS_CONNECT_TIMEOUT_S):
            if self._explicitly_disconnected or self._runner_stop.is_set():
                return
            raise ConnectionError(f"provider not connected within {WS_CONNECT_TIMEOUT_S}s")
        if not self._replayed_for_attempt:
            self.on_ws_connected()

    def _stop_requested(self, stop: threading.Event) -> bool:
        return self._explicitly_disconnected or stop.is_set()

    def _run_provider_connect(self, stop: threading.Event | None = None) -> None:
        """Blocking connect with bounded wait, retry count and backoff (runs in a thread).

        Stops early when `stop` (this runner's event) is set by an explicit disconnect
        or by the monitor's off-hours cleanup.
        """
        stop = stop or self._runner_stop
        retry_count = 0
        last_error: Exception = ConnectionError("provider connection failed")
        while retry_count < WS_MAX_RETRIES and not self._stop_requested(stop):
            try:
                logger.debug(f"[WS] Connection attempt {retry_count + 1}/{WS_MAX_RETRIES}")
                self._attempt_provider_connect()
                return
            except Exception as e:
                last_error = e
                retry_count += 1
                log_exception_throttled(
                    logger,
                    "live_market.connect_retry",
                    "[WS] Connection attempt %s failed: %s",
                    retry_count,
                    e,
                )
                if retry_count >= WS_MAX_RETRIES:
                    break
                # Exponential backoff: 5, 10, 20, 40, 60...; wakes early on explicit disconnect.
                delay = min(WS_BACKOFF_BASE_S * (2 ** (retry_count - 1)), WS_BACKOFF_MAX_S)
                logger.debug(f"[WS] Reconnection attempt in {delay} seconds...")
                stop.wait(delay)
        if not self._stop_requested(stop):
            self.on_ws_failure(last_error)

    def disconnect(self) -> None:
        """Explicit disconnect: close the provider socket; stay down until an explicit connect."""
        self._explicitly_disconnected = True
        self._disconnect_event.set()
        self._runner_stop.set()
        if self.ws_service:
            self.ws_service.disconnect()
        self.ws_connected = False
        self._is_connecting = False

    def on_ws_failure(self, error: Exception) -> None:
        """Callback when WebSocket connection fails"""
        self.ws_connected = False
        self._is_connecting = False
        runtime_metrics.increment("live_market.provider.failed")
        logger.error(f"[ERROR] WebSocket connection failed: {error}")

    def is_market_open(self) -> bool:
        """Check if current IST time is within market hours"""
        if self.dev_mode:
            self._market_status = "OPEN (DEV)"
            return True

        now = datetime.datetime.now(IST)
        # Weekends check
        if now.weekday() >= 5:  # Saturday or Sunday
            self._market_status = "CLOSED_WEEKEND"
            return False

        current_time = now.time()
        is_open = MARKET_OPEN_TIME <= current_time <= MARKET_CLOSE_TIME
        self._market_status = "OPEN" if is_open else "CLOSED_OFF_HOURS"
        return is_open

    async def _update_buffer(self, tick: TickPayload) -> None:
        """Async method to update buffer on main loop"""
        # Symbol is already converted to DB format by handle_tick_incoming
        symbol = tick.get("symbol")
        self.tick_buffer[symbol] = tick
        self.latest_values[symbol] = tick
        runtime_metrics.increment("live_market.ticks.inbound")
        runtime_metrics.set_gauge("live_market.buffered_symbols", float(len(self.tick_buffer)))
        runtime_metrics.set_gauge("live_market.latest_values", float(len(self.latest_values)))
        await asyncio.sleep(0)

    async def _flush_loop(self) -> None:
        """Background task to flush buffered ticks every 200ms for low-latency updates"""
        while True:
            try:
                await asyncio.sleep(0.2)
                if not self.tick_buffer:
                    continue

                # Atomic Swap: Safe and prevents data loss
                batch = self.tick_buffer
                self.tick_buffer = {}

                # Broadcast BATCH update (Significantly reduces overhead)
                # Payload: {"type": "ticker_batch", "data": [tick1, tick2, ...]}
                if batch:
                    started = perf_counter()
                    msg = {"type": "ticker_batch", "data": list(batch.values())}
                    await manager.broadcast(msg)
                    duration_ms = (perf_counter() - started) * 1000.0
                    runtime_metrics.increment("live_market.flush.batches")
                    runtime_metrics.increment("live_market.flush.ticks", float(len(batch)))
                    runtime_metrics.observe_ms("live_market.flush.broadcast_ms", duration_ms)
                    runtime_metrics.set_gauge(
                        "live_market.buffered_symbols", float(len(self.tick_buffer))
                    )

            except asyncio.CancelledError:
                logger.info("Broadcast flush loop cancelled")
                raise
            except Exception:
                logger.exception("Error in flush loop; continuing")
                runtime_metrics.increment("live_market.flush.loop_errors")
                await asyncio.sleep(LOOP_ERROR_PAUSE_S)

    async def _check_connection_health(self) -> None:
        """One monitor check: reconnect during market hours, disconnect off-hours."""
        if not self.is_market_open():
            # Ensure we are disconnected off-hours
            if self.ws_connected or self._is_connecting:
                logger.info("[WS] Market closed. Cleaning up connection.")
                # Stop a runner that is retrying so it cannot reconnect off-hours.
                self._runner_stop.set()
                if self.ws_service:
                    self.ws_service.disconnect()
                self.ws_connected = False
                self._is_connecting = False
            return

        if self._provider_is_connected():
            # The socket may be connected while our flag is still false.
            self.ws_connected = True
            return

        if self.ws_connected:
            logger.info("[WS] Connection dropped during market hours. Triggering reconnect...")
            runtime_metrics.increment("live_market.provider.reconnect_triggered")
            self.ws_connected = False
            await self.connect_async()
        elif not self._is_connecting:
            logger.debug("[WS] Market is open but not connected. Triggering connection...")
            runtime_metrics.increment("live_market.provider.reconnect_triggered")
            await self.connect_async()

    async def _monitor_loop(self) -> None:
        """
        Background task to monitor connection health.
        Reconnects if connection drops during market hours.
        Inspired by OpenAlgo's connection pooling and failover logic.
        """
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30s
                await self._check_connection_health()
            except asyncio.CancelledError:
                logger.info("Connection monitor cancelled")
                raise
            except Exception:
                logger.exception("Error in monitor loop; continuing")
                runtime_metrics.increment("live_market.monitor.loop_errors")
                await asyncio.sleep(LOOP_ERROR_PAUSE_S)

    def handle_tick_incoming(self, tick: TickPayload) -> None:
        """Entry point for ticks from Fyers Thread - buffer for batch broadcast"""
        if not self.loop or self.loop.is_closed():
            return
        try:
            fyers_symbol = tick.get("symbol")
            if not fyers_symbol:
                return
            # Convert symbol to DB_FORMAT before buffering
            db_symbol = symbol_master.to_db(fyers_symbol)
            tick_db = self._normalize_tick(tick, db_symbol)

            # Update latest values cache
            self.latest_values[db_symbol] = tick_db
            runtime_metrics.set_gauge("live_market.latest_values", float(len(self.latest_values)))

            # Buffer for batch broadcast to avoid duplicate immediate sends.
            asyncio.run_coroutine_threadsafe(self._update_buffer(tick_db), self.loop)
        except Exception as e:
            log_exception_throttled(
                logger, "live_market.tick", "Error processing incoming tick: %s", e
            )

    @staticmethod
    def _normalize_tick(tick: TickPayload, db_symbol: str) -> TickPayload:
        """Copy of a Fyers tick keyed by DB symbol, with change_pct, change and volume."""
        tick_db = tick.copy()
        tick_db["symbol"] = db_symbol

        # Fyers sends 'chp' (change percent); otherwise derive it from the previous close.
        if "chp" in tick_db:
            tick_db["change_pct"] = tick_db["chp"]
        elif "ltp" in tick_db and "prev_close_price" in tick_db:
            prev_close = tick_db["prev_close_price"]
            if prev_close and prev_close > 0:
                tick_db["change_pct"] = ((tick_db["ltp"] - prev_close) / prev_close) * 100

        # Normalize absolute change for consumers expecting `change`
        if "ch" in tick_db and "change" not in tick_db:
            tick_db["change"] = tick_db["ch"]

        # Ensure volume is passed through (Fyers sends 'volume' or 'v')
        if "volume" not in tick_db:
            if "v" in tick_db:
                tick_db["volume"] = tick_db["v"]
            elif "vol_traded_today" in tick_db:
                tick_db["volume"] = tick_db["vol_traded_today"]
        return tick_db

    @staticmethod
    def _validate_fyers_token() -> bool:
        from .fyers_client import get_fyers_client

        fyers = get_fyers_client()
        return bool(fyers and fyers.validate_token())

    async def connect_async(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        prevalidated_token: bool | None = None,
        explicit: bool = False,
    ) -> str:
        """`connect()` for async callers: the blocking token check runs off the event loop."""
        needs_validation = (
            prevalidated_token is None
            and (explicit or not self._explicitly_disconnected)
            and not (self._is_connecting or self.ws_connected)
        )
        if needs_validation:
            prevalidated_token = await asyncio.to_thread(self._validate_fyers_token)
        return self.connect(loop=loop, prevalidated_token=prevalidated_token, explicit=explicit)

    def connect(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        prevalidated_token: bool | None = None,
        explicit: bool = False,
    ) -> str:
        """Connect to external data provider if market is open and token is valid.

        `explicit=True` (user-initiated connect) lifts a previous explicit disconnect;
        automatic callers (monitor loop, subscribe) stay disconnected until then.
        """
        if explicit:
            self._explicitly_disconnected = False
            self._disconnect_event.clear()
        elif self._explicitly_disconnected:
            return "disconnected"

        self._capture_loop(loop)
        self._ensure_background_tasks()

        if self._is_connecting or self.ws_connected:
            self._last_connect_block_reason = None
            return "already_connected"

        token_valid = prevalidated_token
        if token_valid is None:
            token_valid = self._validate_fyers_token()
        if not token_valid:
            if self._last_connect_block_reason != "token_invalid":
                logger.debug("Live market connect skipped: Fyers token is invalid or expired.")
                self._last_connect_block_reason = "token_invalid"
            return "token_invalid"

        if not self.is_market_open():
            if self._last_connect_block_reason != "market_closed":
                logger.info(
                    f"Market is CLOSED ({self._market_status}). Skipping Fyers connection for now."
                )
                self._last_connect_block_reason = "market_closed"
            return "market_closed"

        self._last_connect_block_reason = None
        logger.info(f"Market is OPEN ({self._market_status}). Connecting to Fyers...")
        try:
            return self._start_provider_connection()
        except Exception as e:
            self._is_connecting = False
            logger.exception(f"Failed to connect to Fyers: {e}")
            return "error"

    def _capture_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Remember the event loop that ticks and background tasks are scheduled on."""
        if loop:
            self.loop = loop
        elif not self.loop:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("LiveMarketService connected outside async loop context")
                self.loop = None

    def _ensure_background_tasks(self) -> None:
        """Start the monitor and broadcast flush loops if they are not running."""
        if not self.loop:
            return
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = self.loop.create_task(self._monitor_loop())
        if self.broadcast_task is None or self.broadcast_task.done():
            self.broadcast_task = self.loop.create_task(self._flush_loop())

    def _start_provider_connection(self) -> str:
        """Attach to the provider socket, connecting it on a background thread if needed.

        Returns "started" when a connect thread was launched, "already_connected" when the
        provider socket was already connected.
        """
        self._is_connecting = True
        # A new connection supersedes any stop request aimed at an earlier runner.
        self._runner_stop = threading.Event()
        from .fyers_websocket import get_websocket_service

        self.ws_service = get_websocket_service()

        # Register handlers
        self.ws_service.on_tick_handler = self.handle_tick_incoming
        # Fires on first connect and every SDK auto-reconnect (replays subscriptions).
        self.ws_service.on_open_handler = self.on_provider_open

        ws = self.ws_service.ws
        if ws and hasattr(ws, "is_connected") and ws.is_connected():
            logger.info("[OK] WebSocket already connected")
            self.ws_connected = True
            self._is_connecting = False
            return "already_connected"

        # Run connection in a separate thread to avoid blocking startup
        self._ws_thread = threading.Thread(
            target=self._run_provider_connect, args=(self._runner_stop,), daemon=True
        )
        self._ws_thread.start()
        logger.info("[START] WebSocket connection started in background thread")
        return "started"

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to symbols non-blocking"""
        symbols = list({symbol_master.to_db(s) for s in symbols if s})
        if not self.ws_service:
            if self.is_market_open():
                await self.connect_async()
            else:
                self.pending_subscriptions.update(symbols)
                now = time.time()
                if now - self._last_subscribe_market_closed_log_ts > 60:
                    logger.info("Live subscribe deferred: market is closed")
                    self._last_subscribe_market_closed_log_ts = now
                return

        if not self._provider_socket_connected():
            # Add to pending subscriptions
            self.pending_subscriptions.update(symbols)
            logger.warning(
                f"Fyers WebSocket not connected. {len(symbols)} symbols queued for subscription."
            )
            return

        try:
            # Convert to Fyers format one-by-one to avoid rejecting the whole batch.
            fyers_symbols, invalid_symbols = self._to_fyers_symbols(symbols)
            if invalid_symbols:
                logger.warning(
                    "Skipping invalid symbols for provider subscribe: %s", invalid_symbols
                )
            if not fyers_symbols:
                return

            # CRITICAL FIX: Run blocking SDK call in executor
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.ws_service.subscribe, fyers_symbols)
            logger.info(f"Subscribed to {len(fyers_symbols)} symbols")
        except Exception as e:
            logger.exception(f"Fyers subscription failed: {e}")

    def _provider_socket_connected(self) -> bool:
        return bool(self.ws_service and self.ws_service.ws and self.ws_service.ws.is_connected())

    @staticmethod
    def _to_fyers_symbols(symbols: list[str] | set[str]) -> tuple[list[str], list[str]]:
        """(Fyers symbols, symbols that could not be converted), in input order."""
        fyers_symbols: list[str] = []
        invalid_symbols: list[str] = []
        for symbol in symbols:
            try:
                fyers_symbols.append(symbol_master.to_fyers(symbol))
            except Exception:
                invalid_symbols.append(symbol)
        return fyers_symbols, invalid_symbols

    async def subscribe_api(self, symbols: list[str]) -> None:
        """Subscribe on behalf of the HTTP API; these symbols are never released by client drops."""
        self.api_subscriptions.update(symbol_master.to_db(s) for s in symbols if s)
        await self.subscribe(symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe non-blocking"""
        symbols_set: set[str] = set()
        for symbol in symbols:
            if not symbol:
                continue
            try:
                symbols_set.add(symbol_master.to_db(symbol))
            except Exception as exc:
                logger.warning("Skipping invalid symbol for unsubscribe %s: %s", symbol, exc)
        # A release can arrive after another client re-subscribed; keep wanted symbols.
        symbols_set -= manager.get_all_subscribed_symbols() | self.api_subscriptions
        if symbols_set:
            self.pending_subscriptions.difference_update(symbols_set)

        if not self._provider_socket_connected():
            return
        try:
            fyers_symbols: list[str] = []
            for symbol in symbols_set:
                try:
                    fyers_symbols.append(symbol_master.to_fyers(symbol))
                except Exception as exc:
                    logger.warning("Skipping invalid symbol for unsubscribe %s: %s", symbol, exc)
            if not fyers_symbols:
                return
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self.ws_service.unsubscribe, fyers_symbols)
        except Exception as e:
            logger.exception(f"Fyers unsubscription failed: {e}")

    async def get_status(self) -> dict[str, Any]:
        return {
            "market_status": self._market_status,
            "fyers_connected": (
                self.ws_service is not None
                and self.ws_service.ws is not None
                and self.ws_service.ws.is_connected()
            ),
            "pending_subscriptions": len(self.pending_subscriptions),
            "metrics": runtime_metrics.snapshot(prefix="live_market.", strip_prefix=True),
            "websocket": await manager.get_health_snapshot(),
        }

    def get_latest_tick(self, symbol: str) -> dict | None:
        """
        Get latest tick for symbol
        Returns cached live tick if available.
        """
        return self.latest_values.get(symbol)

    def get_fresh_tick(self, symbol: str) -> dict | None:
        """Cached tick usable as a live price, else None.

        The cache keeps ticks after a disconnect or market close, so a tick counts only
        while the market is open (DEV_MODE counts as open) and its exchange time is within
        tick_max_age_sec(). A tick without a time is stale.
        """
        tick = self.latest_values.get(symbol)
        if not isinstance(tick, dict) or not self.is_market_open():
            return None
        ts = tick_time(tick)
        if ts <= 0 or time.time() - ts > tick_max_age_sec():
            return None
        return tick

    def get_fresh_ticks(self, symbols: list[str]) -> dict[str, dict]:
        """Fresh ticks (see get_fresh_tick) of the symbols that have one."""
        fresh = {symbol: self.get_fresh_tick(symbol) for symbol in symbols}
        return {symbol: tick for symbol, tick in fresh.items() if tick is not None}

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


# Release provider subscriptions when a client is dropped (slow client, dead socket).
manager.on_symbols_released = live_market.unsubscribe
