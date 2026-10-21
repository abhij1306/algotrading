"""The legacy market/dashboard/websocket side of the live feed (K3).

HAT owns the one Fyers data socket (`app.hat.data.feed.LiveFeed`, HAT-OVR-006). This service no
longer opens or controls a socket: with HAT enabled, `bind_feed` registers a tick listener on HAT's
feed, caches the latest tick per symbol for the legacy readers (`get_latest_tick`,
`get_fresh_tick`) and broadcasts batches to the websocket clients; the legacy websocket clients'
subscriptions go to HAT's registry and feed. With HAT disabled there is no feed and the legacy
live prices are degraded (no ticks). K5b deletes this module with the market, dashboard and
websocket routers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from time import perf_counter
from typing import TYPE_CHECKING, Any

from ..hat.data import feed as hat_feed
from ..hat.data.subscriptions import API_OWNER, SubscriptionRegistry
from ..utils.clock import SYSTEM_CLOCK, Clock
from ..utils.helpers import safe_float
from ..utils.market_hours import MarketHoursPolicy, MarketPhase, dev_mode_enabled
from ..utils.runtime_metrics import runtime_metrics
from ..utils.ws_manager import manager

if TYPE_CHECKING:
    from ..hat.data.feed import LiveFeed

logger = logging.getLogger(__name__)

TickPayload = dict[str, Any]

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
    """Caches and broadcasts the ticks of HAT's feed for the legacy readers; owns no socket."""

    def __init__(self, clock: Clock = SYSTEM_CLOCK) -> None:
        self._market_status = "UNKNOWN"
        self.tick_buffer: dict[str, TickPayload] = {}
        self.latest_values: dict[str, TickPayload] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.broadcast_task: asyncio.Task[None] | None = None
        self.feed: LiveFeed | None = None
        self._unbind: Callable[[], None] | None = None
        # Read once at construction; tests flip it directly.
        self.dev_mode = dev_mode_enabled()
        self.market_hours = MarketHoursPolicy(clock=clock)

    @property
    def registry(self) -> SubscriptionRegistry:
        """Who wants which symbols (websocket clients, the HTTP API owner and, with HAT enabled,
        HAT itself)."""
        return manager.registry

    def bind_feed(self, feed: LiveFeed) -> None:
        """Read ticks from HAT's feed and share its subscription registry with the websocket
        manager (call once at startup, before any client connects)."""
        self.unbind_feed()
        self.feed = feed
        manager.registry = feed.registry
        self._unbind = feed.add_tick_listener(self._on_feed_tick)

    def unbind_feed(self) -> None:
        if self._unbind is not None:
            self._unbind()
        self._unbind = None
        self.feed = None

    def has_feed(self) -> bool:
        return self.feed is not None

    def disconnect(self) -> None:
        """No-op: the socket belongs to HAT, which closes it on a logout (not this route)."""
        logger.info("Legacy live disconnect ignored: HAT owns the live feed")

    async def shutdown(self) -> None:
        """App shutdown: stop the flush loop and detach from HAT's feed (HAT stops the feed)."""
        task, self.broadcast_task = self.broadcast_task, None
        running_loop = asyncio.get_running_loop()
        # A task of an earlier, closed loop cannot be cancelled or awaited here; drop it.
        live = task is not None and not task.done() and task.get_loop() is running_loop
        if live and task is not None:
            task.cancel()
        self.unbind_feed()
        self.loop = None
        if live and task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def is_market_open(self) -> bool:
        """Whether the market is open now (DEV_MODE counts as open); updates _market_status."""
        phase = self.market_hours.phase(dev_mode=self.dev_mode)
        self._market_status = MARKET_STATUS_BY_PHASE[phase]
        return phase.is_open

    def _on_feed_tick(self, tick: TickPayload) -> None:
        """A normalized tick from HAT's feed, on the event-loop thread."""
        symbol = tick.get("symbol")
        if not symbol:
            return
        self.latest_values[symbol] = tick
        self.tick_buffer[symbol] = tick
        runtime_metrics.increment("live_market.ticks.inbound")
        runtime_metrics.set_gauge("live_market.buffered_symbols", float(len(self.tick_buffer)))
        runtime_metrics.set_gauge("live_market.latest_values", float(len(self.latest_values)))

    async def _flush_loop(self) -> None:
        """Background task to flush buffered ticks every 200ms for low-latency updates"""
        while True:
            try:
                await asyncio.sleep(0.2)
                if not self.tick_buffer:
                    continue

                # Atomic swap: safe and prevents data loss
                batch = self.tick_buffer
                self.tick_buffer = {}

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

    async def connect_async(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        prevalidated_token: bool | None = None,
        explicit: bool = False,
    ) -> str:
        """`connect()` for async callers: HAT's blocking token check runs off the event loop."""
        self._capture_loop(loop)
        self._ensure_flush_task()
        if self.feed is None:
            return "hat_disabled"
        return await self.feed.connect_async(prevalidated_token=prevalidated_token)

    def connect(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        prevalidated_token: bool | None = None,
        explicit: bool = False,
    ) -> str:
        """Start the broadcast loop on `loop` and ask HAT's feed to connect when the market is
        open. `explicit` no longer lifts a disconnect: only a Fyers login does that."""
        self._capture_loop(loop)
        self._ensure_flush_task()
        if self.feed is None:
            return "hat_disabled"
        return self.feed.connect(prevalidated_token=prevalidated_token)

    def _capture_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Remember the event loop that the broadcast task runs on."""
        if loop:
            self.loop = loop
        elif not self.loop:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("LiveMarketService connected outside async loop context")
                self.loop = None

    def _ensure_flush_task(self) -> None:
        if not self.loop:
            return
        if self.broadcast_task is None or self.broadcast_task.done():
            self.broadcast_task = self.loop.create_task(self._flush_loop())

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe the symbols at HAT's feed (the registry already holds them)."""
        if self.feed is not None:
            await self.feed.subscribe(symbols)

    async def subscribe_api(self, symbols: list[str]) -> None:
        """Subscribe on behalf of the HTTP API; these symbols are never released by client drops."""
        self.registry.add(API_OWNER, symbols)
        await self.subscribe(symbols)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Release the symbols no owner holds at HAT's feed."""
        if self.feed is not None:
            await self.feed.unsubscribe(symbols)

    async def get_status(self) -> dict[str, Any]:
        feed = self.feed
        connected = feed is not None and feed.is_connected()
        return {
            "market_status": self._market_status,
            "fyers_connected": connected,
            "provider_state": feed.state.value if feed is not None else "hat_disabled",
            # Symbols waiting for the provider to connect (the session replays them).
            "pending_subscriptions": 0 if connected else len(self.registry.all_symbols()),
            "metrics": {
                **runtime_metrics.snapshot(prefix="live_market.", strip_prefix=True),
                "feed": feed.status()["metrics"] if feed is not None else {},
            },
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


# User-facing message for each status that connect()/connect_async() returns: HAT's, plus the
# one the legacy service adds.
CONNECT_STATUS_MESSAGES: dict[str, str] = {
    **hat_feed.CONNECT_STATUS_MESSAGES,
    "hat_disabled": "Live feed unavailable: HAT is disabled (set HAT_ENABLED to receive ticks)",
}


def connect_status_message(status: str) -> str:
    """The message for a connect() status, with a generic one for an unknown status."""
    return CONNECT_STATUS_MESSAGES.get(status, "Live feed connection completed")


# Singleton
live_market = LiveMarketService()


def get_live_market_service() -> LiveMarketService:
    """Get the singleton LiveMarketService instance."""
    return live_market


# Release provider subscriptions when a client is dropped (slow client, dead socket).
manager.on_symbols_released = live_market.unsubscribe
