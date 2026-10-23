"""HAT's live feed: the one Fyers data socket in the process (HAT-OVR-006, HAT-DATA-110, K3).

`LiveFeed` owns the provider socket through one `ProviderSession` and the `SubscriptionRegistry`
that says which symbols the socket must carry. It

- connects and reconnects the session, only on a trading day from 09:00 IST (the pre-open session,
  so the opening gaps can be captured, HAT-DATA-118) to the close, by HAT's trading calendar
  (`SessionCalendar`); it never runs on a day the calendar does not hold as a trading day;
- turns each provider message into a tick keyed by database symbol (`ticks.normalize_tick`) and
  hands it to the tick listeners on the event-loop thread, in registration order, a failing
  listener isolated from the rest (`add_tick_listener`, the only tick input `LiveData.attach`
  uses);
- subscribes and unsubscribes provider symbols for the registry's owners, and replays the
  registry on every (re)connect;
- follows the Fyers login: `install_auth_hooks` registers `token_change`, `login` and
  `disconnect` on `FyersAuthService`, so a re-login reaches the next socket and a logout closes the
  socket and keeps it closed until the next explicit login.

HAT's other modules see the feed only through the `TickFeed` protocol and the registry (the CONF
single-feed rule), and nothing outside HAT builds a socket or a session.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Callable, Iterable
from datetime import time as clock_time
from typing import Any, Protocol

from app.hat.clock import SYSTEM_CLOCK, Clock

from ..risk.sessions import SessionCalendar
from .calendar import session_calendar
from .provider.data_socket import SocketFactory, TokenSource
from .provider.fyers_websocket import fyers_socket_factory, session_token_valid
from .provider.fyers_websocket import fyers_token as provider_token
from .provider.metrics import feed_metrics, log_exception_throttled
from .provider.provider_session import (
    ProviderSession,
    RetryPolicy,
    SessionState,
    Spawn,
    spawn_daemon_thread,
)
from .provider.ticks import TickPayload, normalize_tick
from .sessions import IST
from .subscriptions import SubscriptionRegistry
from .symbols import SymbolMaster, symbol_master

__all__ = [
    "CONNECT_STATUS_MESSAGES",
    "MONITOR_INTERVAL_S",
    "PREOPEN_FROM",
    "AuthHooks",
    "LiveFeed",
    "CalendarSource",
    "TickListener",
    "calendar_source",
    "connect_status_message",
]

logger = logging.getLogger(__name__)

PREOPEN_FROM = clock_time(9, 0)  # IST: the pre-open session; the feed may connect from here
MONITOR_INTERVAL_S = 30.0
# Pause after an unexpected monitor error so a persistent failure cannot busy-spin.
LOOP_ERROR_PAUSE_S = 1.0

# A tick listener: called with each normalized tick on the event-loop thread.
TickListener = Callable[[TickPayload], None]

# User-facing message for each status that `connect()` returns.
CONNECT_STATUS_MESSAGES: dict[str, str] = {
    "started": "Live feed connection started",
    "already_connected": "Live feed already connected",
    "disconnected": "Live feed was explicitly disconnected",
    "market_closed": "Live feed deferred because the market is closed",
    "token_invalid": "Live feed deferred because the Fyers token is invalid or expired",
    "error": "Live feed connection failed",
}


def connect_status_message(status: str) -> str:
    """The message for a `connect()` status, with a generic one for an unknown status."""
    return CONNECT_STATUS_MESSAGES.get(status, "Live feed connection completed")


CalendarSource = Callable[[], SessionCalendar | None]


def calendar_source(
    session_factory: Callable[[], Any],
    *,
    ttl_s: float = 600.0,
    retry_s: float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> CalendarSource:
    """A source that reads HAT's trading calendar (`session_calendar`) through `session_factory`,
    at most once per `ttl_s`. It returns None when the market has no calendar yet and raises when
    the read fails, so the feed stays closed; either outcome is kept for `retry_s`, so a read made
    off the event loop serves the connect that follows it."""
    cached: list[tuple[float, SessionCalendar | None]] = []

    def source() -> SessionCalendar | None:
        now = monotonic()
        if cached:
            at, calendar = cached[0]
            if now - at < (ttl_s if calendar is not None else min(ttl_s, retry_s)):
                return calendar
        try:
            with session_factory() as session:
                calendar = session_calendar(session)
        except Exception:
            cached[:] = [(now, None)]  # unknown until `retry_s` passes
            raise
        cached[:] = [(now, calendar)]
        return calendar

    return source


class AuthHooks(Protocol):
    """The part of `FyersAuthService` the feed registers on (`app.hat.execution.fyers_auth`)."""

    def add_hook(self, event: Any, hook: Callable[[], None]) -> None: ...  # noqa: ANN401


class LiveFeed:
    """The one provider socket: lifecycle, ticks and subscriptions; see the module docstring."""

    def __init__(
        self,
        *,
        calendar: SessionCalendar | None = None,
        calendar_source: CalendarSource | None = None,
        clock: Clock = SYSTEM_CLOCK,
        symbols: SymbolMaster = symbol_master,
        registry: SubscriptionRegistry | None = None,
        socket_factory: SocketFactory = fyers_socket_factory,
        token_source: TokenSource = provider_token,
        validate_token: Callable[[], bool] = session_token_valid,
        retry: RetryPolicy | None = None,
        spawn: Spawn = spawn_daemon_thread,
    ) -> None:
        self._symbols = symbols
        self._validate_token = validate_token
        # Which days the market trades comes from HAT's trading calendar: a fixed `calendar` or a
        # `calendar_source` that reads it. With neither, or while the source has no calendar, the
        # feed is closed, and a day the calendar does not hold is never taken for a trading day.
        self._calendar = calendar
        self._calendar_source = calendar_source
        self._clock = clock
        self._connect_task: asyncio.Task[str] | None = None
        # Who wants which symbols: HAT's own owner (the registry keeps a refcount per owner).
        self.registry = registry or SubscriptionRegistry(symbols.to_db)
        self.session = ProviderSession(
            socket_factory=socket_factory,
            token_source=token_source,
            desired_symbols=self._desired_provider_symbols,
            on_message=self.handle_message,
            retry=retry,
            spawn=spawn,
        )
        self.loop: asyncio.AbstractEventLoop | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self._tick_listeners: list[TickListener] = []
        # Set by an explicit disconnect (a logout); cleared only by an explicit connect (a login).
        self._explicitly_disconnected = False
        self._last_block_reason: str | None = None

    # --- state ------------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self.session.state

    @property
    def explicitly_disconnected(self) -> bool:
        return self._explicitly_disconnected

    def is_connected(self) -> bool:
        return self.session.is_connected()

    def has_socket(self) -> bool:
        """Whether a provider socket is open or being built (a connect in flight); either may
        carry the token that was current when it was built."""
        return self.session.has_socket or self.session.state is SessionState.CONNECTING

    @property
    def calendar(self) -> SessionCalendar | None:
        """The trading calendar now, or None when it is unknown."""
        if self._calendar is not None:
            return self._calendar
        if self._calendar_source is None:
            return None
        try:
            return self._calendar_source()
        except Exception as exc:  # an unreadable calendar is an unknown calendar
            logger.warning("Trading calendar unavailable: %s", type(exc).__name__)
            return None

    def connection_allowed(self) -> bool:
        """Whether the feed may be connected now: the calendar's session, or the pre-open session
        from 09:00 IST before the open of a trading day of HAT's calendar (HAT-DATA-118). False
        while the calendar is unknown and on a day it does not hold."""
        calendar = self.calendar
        if calendar is None:
            return False
        now = self._clock.now()
        if calendar.is_open(now):
            return True
        local = now.astimezone(IST)
        opens = calendar.open_on(local.date())
        return opens is not None and now < opens and local.time() >= PREOPEN_FROM

    def status(self) -> dict[str, Any]:
        connected = self.is_connected()
        return {
            "connected": connected,
            "provider_state": self.session.state.value,
            "explicitly_disconnected": self._explicitly_disconnected,
            # Symbols waiting for the provider to connect (the session replays them).
            "pending_subscriptions": 0 if connected else len(self.registry.all_symbols()),
            "metrics": feed_metrics.snapshot(),
        }

    # --- ticks ------------------------------------------------------------------------

    def add_tick_listener(self, listener: TickListener) -> Callable[[], None]:
        """Call `listener(tick)` for every inbound tick; returns a function that removes it.

        Listeners run on the event-loop thread, in registration order. A listener must be a
        plain, fast function: it runs inline on the loop. An exception it raises is logged and
        counted and never reaches another listener. With no listener registered the hook costs
        one empty-list check per tick.
        """
        if inspect.iscoroutinefunction(listener):
            raise TypeError("a tick listener must be a plain function, not a coroutine function")
        self._tick_listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):  # already removed
                self._tick_listeners.remove(listener)

        return remove

    def handle_message(self, message: TickPayload) -> None:
        """A provider message, on an SDK thread: key it by database symbol and hand it to the
        event loop. Messages without a symbol (connection acks) are dropped."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            provider_symbol = message.get("symbol")
            if not provider_symbol:
                return
            tick = normalize_tick(message, self._symbols.to_db(provider_symbol))
            loop.call_soon_threadsafe(self._deliver, tick)
        except Exception as exc:  # never raise into the SDK thread
            log_exception_throttled(logger, "feed.tick", "Error processing incoming tick: %s", exc)

    def _deliver(self, tick: TickPayload) -> None:
        feed_metrics.increment("feed.ticks.inbound")
        for listener in tuple(self._tick_listeners):
            try:
                listener(tick)
            except Exception as exc:  # isolate: never break the stream for a listener's bug
                feed_metrics.increment("feed.listener.errors")
                log_exception_throttled(logger, "feed.listener", "Tick listener failed: %s", exc)

    # --- connection -------------------------------------------------------------------

    def connect(self, *, explicit: bool = False, prevalidated_token: bool | None = None) -> str:
        """Connect when the market allows it and the token is valid; returns a status.

        `explicit=True` (a login) lifts a previous explicit disconnect; automatic callers (the
        monitor, a subscribe) stay disconnected until then. Validating the token may call
        Fyers, so an async caller uses `connect_async`.
        """
        if explicit:
            self._explicitly_disconnected = False
        elif self._explicitly_disconnected:
            return "disconnected"
        if self.session.busy:
            self._last_block_reason = None
            return "already_connected"
        valid = prevalidated_token if prevalidated_token is not None else self._token_valid()
        if not valid:
            self._log_blocked("token_invalid", "Live feed not connected: Fyers token is invalid")
            return "token_invalid"
        if not self.connection_allowed():
            self._log_blocked("market_closed", "Live feed not connected: the market is closed")
            return "market_closed"
        self._last_block_reason = None
        try:
            if not self.session.start():
                return "already_connected"
        except Exception as exc:
            logger.warning("Live feed connect failed: %s", type(exc).__name__)
            return "error"
        logger.info("[START] Provider connection started in a background thread")
        return "started"

    async def connect_async(
        self, *, explicit: bool = False, prevalidated_token: bool | None = None
    ) -> str:
        """`connect()` for async callers: the blocking token check runs off the event loop."""
        needs_validation = (
            prevalidated_token is None
            and (explicit or not self._explicitly_disconnected)
            and not self.session.busy
        )
        if needs_validation:
            prevalidated_token = await asyncio.to_thread(self._token_valid)
        # read the calendar off the loop: `connect` then finds the source's cached calendar
        await asyncio.to_thread(lambda: self.calendar)
        return self.connect(explicit=explicit, prevalidated_token=prevalidated_token)

    def disconnect(self) -> None:
        """Explicit disconnect (a logout): close the socket and stay down until an explicit
        connect, so a runner in flight cannot reconnect on its own after a later login."""
        self._explicitly_disconnected = True
        self.session.close()
        logger.info("[Fyers] Live feed disconnected")

    def _token_valid(self) -> bool:
        try:
            return bool(self._validate_token())
        except Exception as exc:  # a failed check is an invalid token, never a crash
            logger.warning("Fyers token check failed: %s", type(exc).__name__)
            return False

    def _log_blocked(self, reason: str, message: str) -> None:
        if self._last_block_reason != reason:
            logger.info(message)
            self._last_block_reason = reason

    def _drop_stale_socket(self) -> None:
        """Close a socket built with an earlier token (a connect in flight too), without an
        explicit disconnect: the next connect builds a new socket from the current token."""
        if self.has_socket():
            self.session.stop()

    # --- the login hooks (K2 follow-up) -----------------------------------------------

    def on_token_change(self) -> None:
        """A token was saved or removed: the open socket carries the old one."""
        self._drop_stale_socket()

    def on_login(self) -> None:
        """A login succeeded: an explicit connect with the new token (the login route runs in a
        worker thread, so the blocking token check is fine). The outcome is logged in one line."""
        self._drop_stale_socket()
        status = self.connect(explicit=True)
        logger.info("[Fyers] Live feed reconnect after login: %s", status)

    def install_auth_hooks(self, service: AuthHooks) -> None:
        """Register the three Fyers login hooks on `service` (`FyersAuthService`). Idempotent."""
        service.add_hook("token_change", self.on_token_change)
        service.add_hook("login", self.on_login)
        service.add_hook("disconnect", self.disconnect)

    # --- lifecycle on the event loop --------------------------------------------------

    def start(
        self, loop: asyncio.AbstractEventLoop, *, prevalidated_token: bool | None = None
    ) -> str:
        """Remember the event loop that ticks are delivered on, start the health monitor and
        connect if the market allows it, through `connect_async` so the calendar and token reads
        stay off the loop. Call on the loop."""
        self.loop = loop
        if self.monitor_task is None or self.monitor_task.done():
            self.monitor_task = loop.create_task(self._monitor_loop())
        self._connect_task = loop.create_task(
            self.connect_async(prevalidated_token=prevalidated_token)
        )
        return "connecting"

    async def stop(self) -> None:
        """App shutdown: stop the monitor and close the socket. Unlike `disconnect()` this does
        not mark an explicit disconnect, so a later start in the same process can connect."""
        task, self.monitor_task = self.monitor_task, None
        running = asyncio.get_running_loop()
        live = task is not None and not task.done() and task.get_loop() is running
        if live and task is not None:
            task.cancel()
        pending, self._connect_task = self._connect_task, None
        if pending is not None and not pending.done() and pending.get_loop() is running:
            pending.cancel()  # a start-up connect must not open a socket after the stop
            await asyncio.gather(pending, return_exceptions=True)
        try:
            await asyncio.to_thread(self.session.stop)  # closing joins the SDK threads
        finally:
            self.loop = None
            if live and task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def check_health(self) -> None:
        """One monitor check: reconnect during market hours, disconnect off-hours."""
        state = self.session.state
        if not await asyncio.to_thread(self.connection_allowed):  # may read the calendar table
            if state in (SessionState.CONNECTING, SessionState.CONNECTED, SessionState.DROPPED):
                logger.info("[WS] Market closed. Cleaning up the provider connection.")
                await asyncio.to_thread(self.session.stop)
            return
        if self.session.is_connected() or state is SessionState.CONNECTING:
            return
        if state in (SessionState.CONNECTED, SessionState.DROPPED):
            logger.info("[WS] Connection dropped during market hours. Reconnecting...")
        feed_metrics.increment("feed.provider.reconnect_triggered")
        await self.connect_async()

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(MONITOR_INTERVAL_S)
                await self.check_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in the feed monitor; continuing")
                feed_metrics.increment("feed.monitor.loop_errors")
                await asyncio.sleep(LOOP_ERROR_PAUSE_S)

    # --- provider subscriptions -------------------------------------------------------

    def _to_provider(self, symbols: Iterable[str]) -> tuple[list[str], list[str]]:
        """(provider symbols, symbols that could not be converted), in input order."""
        converted: list[str] = []
        invalid: list[str] = []
        for symbol in symbols:
            try:
                converted.append(self._symbols.to_fyers(symbol))
            except ValueError:
                invalid.append(symbol)
        return converted, invalid

    def _desired_provider_symbols(self) -> list[str]:
        """Every symbol any owner holds, in provider format: the session's replay set."""
        return self._to_provider(sorted(self.registry.all_symbols()))[0]

    async def subscribe(self, symbols: list[str]) -> None:
        """Subscribe provider symbols, off the event loop. The caller has put them in the
        registry; while the socket is down the registry holds them and the session replays them
        on connect."""
        wanted = sorted({self._symbols.to_db(s) for s in symbols if s} - {""})
        if not self.session.is_connected():
            idle = self.session.state is SessionState.IDLE
            if idle and await asyncio.to_thread(self.connection_allowed):
                await self.connect_async()
            else:
                logger.info("Live feed not connected: %s symbols queued", len(wanted))
            return
        converted, invalid = self._to_provider(wanted)
        if invalid:
            logger.warning("Skipping invalid symbols for provider subscribe: %s", invalid)
        if not converted:
            return
        try:
            await asyncio.to_thread(self.session.subscribe, converted)
        except Exception as exc:
            logger.warning("Live feed subscribe failed: %s", type(exc).__name__)
            return
        # The delta can go stale while the provider call runs: if the last owner released a
        # symbol meanwhile, its unsubscribe may have reached the provider first.
        released = [s for s in wanted if not self.registry.is_held(s)]
        if released:
            await self.unsubscribe(released)

    async def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe provider symbols no owner holds any more, off the event loop."""
        # A release can arrive after another owner re-subscribed: keep held symbols.
        wanted = sorted({self._symbols.to_db(s) for s in symbols if s} - {""})
        wanted = [s for s in wanted if not self.registry.is_held(s)]
        if not self.session.is_connected():
            return
        converted, invalid = self._to_provider(wanted)
        if invalid:
            logger.warning("Skipping invalid symbols for provider unsubscribe: %s", invalid)
        if not converted:
            return
        try:
            await asyncio.to_thread(self.session.unsubscribe, converted)
        except Exception as exc:
            logger.warning("Live feed unsubscribe failed: %s", type(exc).__name__)
            return
        # An owner may have re-subscribed while the provider call ran and its subscribe may have
        # reached the provider first.
        regained = [s for s in wanted if self.registry.is_held(s)]
        if regained:
            await self.subscribe(regained)
