from __future__ import annotations

import asyncio
import logging
import os
import time
from time import perf_counter
from typing import Any

from ..utils.clock import SYSTEM_CLOCK, Clock
from ..utils.helpers import safe_float
from ..utils.logger import log_exception_throttled
from ..utils.market_hours import MarketHoursPolicy, MarketPhase, dev_mode_enabled
from ..utils.runtime_metrics import runtime_metrics
from ..utils.ws_manager import manager
from .fyers_websocket import fyers_socket_factory, fyers_token
from .live.data_socket import SocketFactory, TokenSource
from .live.provider_session import (
    ProviderSession,
    RetryPolicy,
    SessionState,
    Spawn,
    spawn_daemon_thread,
)
from .live.ticks import TickPayload, normalize_tick
from .subscription_registry import API_OWNER, SubscriptionRegistry
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)

# _market_status shown by get_status() for each market phase.
MARKET_STATUS_BY_PHASE: dict[MarketPhase, str] = {
    MarketPhase.DEV: "OPEN (DEV)",
    MarketPhase.WEEKEND: "CLOSED_WEEKEND",
    MarketPhase.HOLIDAY: "CLOSED_HOLIDAY",
    MarketPhase.PRE_MARKET: "CLOSED_OFF_HOURS",
    MarketPhase.OPEN: "OPEN",
    MarketPhase.POST_MARKET: "CLOSED_OFF_HOURS",
}

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
    Enforces market hours (see app.utils.market_hours); the provider socket itself is
    owned by a ProviderSession (see app.services.live.provider_session).
    """

    def __init__(
        self,
        clock: Clock = SYSTEM_CLOCK,
        *,
        socket_factory: SocketFactory = fyers_socket_factory,
        token_source: TokenSource = fyers_token,
        retry: RetryPolicy | None = None,
        spawn: Spawn = spawn_daemon_thread,
    ) -> None:
        self._market_status = "UNKNOWN"
        self.tick_buffer = {}
        self.latest_values = {}
        self.loop = None
        self.broadcast_task = None
        self.monitor_task = None
        # Read once at construction; tests flip it directly.
        self.dev_mode = dev_mode_enabled()
        self.market_hours = MarketHoursPolicy(clock=clock)
        self.session = ProviderSession(
            socket_factory=socket_factory,
            token_source=token_source,
            desired_symbols=self._desired_provider_symbols,
            on_message=self.handle_tick_incoming,
            retry=retry,
            spawn=spawn,
        )
        # Set by an explicit POST /disconnect; cleared only by an explicit connect.
        self._explicitly_disconnected = False
        self._last_connect_block_reason: str | None = None
        self._last_subscribe_market_closed_log_ts = 0.0

    @property
    def registry(self) -> SubscriptionRegistry:
        """Who wants which symbols (websocket clients and the HTTP API owner)."""
        return manager.registry

    @property
    def ws_service(self) -> LiveMarketService | None:
        """Legacy shim: this service while a provider socket or connect is live, else None.

        routers/auth.py (logout) still calls `ws_service.disconnect()`, and the TokenStore PR
        checks `ws_service is not None`. Returning the service makes that call the explicit
        disconnect(), so a connect in flight or the monitor cannot reconnect after logout.
        """
        busy = self.session.has_socket or self.session.state is SessionState.CONNECTING
        return self if busy else None

    def _desired_provider_symbols(self) -> list[str]:
        """Every symbol any owner holds, in provider format: the session's replay set."""
        fyers_symbols, _invalid = self._to_fyers_symbols(self.registry.all_symbols())
        return fyers_symbols

    def disconnect(self) -> None:
        """Explicit disconnect: close the provider socket; stay down until an explicit connect."""
        self._explicitly_disconnected = True
        self.session.close()

    async def shutdown(self) -> None:
        """App shutdown: stop the monitor and flush loops and close the provider socket.

        Unlike disconnect() this does not mark an explicit disconnect, so a later
        startup in the same process (tests) can connect again.
        """
        tasks = [task for task in (self.monitor_task, self.broadcast_task) if task is not None]
        self.monitor_task = None
        self.broadcast_task = None
        running_loop = asyncio.get_running_loop()
        # Tasks of an earlier, closed loop cannot be cancelled or awaited here; drop them.
        live_tasks = [t for t in tasks if not t.done() and t.get_loop() is running_loop]
        for task in live_tasks:
            task.cancel()
        try:
            self.session.stop()
        finally:
            self.loop = None
            await asyncio.gather(*live_tasks, return_exceptions=True)

    def is_market_open(self) -> bool:
        """Whether the market is open now (DEV_MODE counts as open); updates _market_status."""
        phase = self.market_hours.phase(dev_mode=self.dev_mode)
        self._market_status = MARKET_STATUS_BY_PHASE[phase]
        return phase.is_open

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
        state = self.session.state
        if not self.is_market_open():
            # Ensure we are disconnected off-hours, including a runner that is retrying.
            if state in (SessionState.CONNECTING, SessionState.CONNECTED, SessionState.DROPPED):
                logger.info("[WS] Market closed. Cleaning up connection.")
                self.session.stop()
            return

        if self.session.is_connected() or state is SessionState.CONNECTING:
            return

        if state in (SessionState.CONNECTED, SessionState.DROPPED):
            logger.info("[WS] Connection dropped during market hours. Triggering reconnect...")
        else:
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
            tick_db = normalize_tick(tick, db_symbol)

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
            and not self.session.busy
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
        elif self._explicitly_disconnected:
            return "disconnected"

        self._capture_loop(loop)
        self._ensure_background_tasks()

        if self.session.busy:
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
            if not self.session.start():
                return "already_connected"
        except Exception as e:
            logger.exception(f"Failed to connect to Fyers: {e}")
            return "error"
        logger.info("[START] Provider connection started in background thread")
        return "started"

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

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to symbols non-blocking"""
        symbols = list({symbol_master.to_db(s) for s in symbols if s})
        if not self.session.is_connected():
            # The registry still holds the symbols; the session replays them on connect.
            if self.session.state is SessionState.IDLE:
                await self._connect_for_subscribe()
            else:
                logger.warning(
                    f"Fyers WebSocket not connected. {len(symbols)} symbols queued "
                    "for subscription."
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
            await loop.run_in_executor(None, self.session.subscribe, fyers_symbols)
            logger.info(f"Subscribed to {len(fyers_symbols)} symbols")
        except Exception as e:
            logger.exception(f"Fyers subscription failed: {e}")
            return
        # The subscribe delta can go stale while the provider call runs: if the last owner
        # released a symbol meanwhile, its unsubscribe may have reached the provider first.
        released = sorted(s for s in symbols if not self.registry.is_held(s))
        if released:
            await self.unsubscribe(released)

    async def _connect_for_subscribe(self) -> None:
        """A subscribe while the session is idle: connect when the market is open."""
        if self.is_market_open():
            await self.connect_async()
            return
        now = time.time()
        if now - self._last_subscribe_market_closed_log_ts > 60:
            logger.info("Live subscribe deferred: market is closed")
            self._last_subscribe_market_closed_log_ts = now

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
        self.registry.add(API_OWNER, symbols)
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
        # A release can arrive after another owner re-subscribed; keep held symbols.
        symbols_set = {s for s in symbols_set if not self.registry.is_held(s)}

        if not self.session.is_connected():
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
            await loop.run_in_executor(None, self.session.unsubscribe, fyers_symbols)
        except Exception as e:
            logger.exception(f"Fyers unsubscription failed: {e}")
            return
        # The check above is not atomic with the provider call: an owner may have
        # re-subscribed meanwhile and its subscribe may have reached the provider first.
        regained = sorted(s for s in symbols_set if self.registry.is_held(s))
        if regained:
            await self.subscribe(regained)

    async def get_status(self) -> dict[str, Any]:
        connected = self.session.is_connected()
        return {
            "market_status": self._market_status,
            "fyers_connected": connected,
            "provider_state": self.session.state.value,
            # Symbols waiting for the provider to connect (the session replays them).
            "pending_subscriptions": 0 if connected else len(self.registry.all_symbols()),
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


# User-facing message for each status that connect()/connect_async() returns.
CONNECT_STATUS_MESSAGES: dict[str, str] = {
    "started": "WebSocket initialization triggered",
    "already_connected": "WebSocket already connected",
    "disconnected": "WebSocket was explicitly disconnected",
    "market_closed": "WebSocket deferred because market is closed",
    "token_invalid": "WebSocket deferred because Fyers token is invalid/expired",
    "error": "WebSocket initialization failed",
}


def connect_status_message(status: str) -> str:
    """The message for a connect() status, with a generic one for an unknown status."""
    return CONNECT_STATUS_MESSAGES.get(status, "WebSocket initialization completed")


# Singleton
live_market = LiveMarketService()


def get_live_market_service() -> LiveMarketService:
    """Get the singleton LiveMarketService instance."""
    return live_market


# Release provider subscriptions when a client is dropped (slow client, dead socket).
manager.on_symbols_released = live_market.unsubscribe
