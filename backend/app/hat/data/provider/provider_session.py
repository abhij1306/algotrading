"""ProviderSession: the lifecycle of the provider data socket.

One session owns at most one `DataSocket` at a time. It

- connects on a background runner with a bounded wait, a retry count and backoff, fetching
  the current token from its `TokenSource` on every attempt (a re-login takes effect on the
  next connect);
- tracks the connection state (`SessionState`);
- replays every wanted symbol (`desired_symbols`) on each (re)connect, including the SDK's own
  auto-reconnects, which re-fire `on_connect` on the same socket;
- closes the old socket before building a new one, and ignores callbacks from sockets it has
  already let go of;
- shields itself from its callbacks: an exception in the message sink or in the replay is
  logged and the session carries on.

Market-hours policy and the explicit-disconnect rule stay with `LiveFeed` (`app.hat.data.feed`), the
only place a session is built (HAT-OVR-006).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .data_socket import (
    SYMBOL_UPDATE,
    DataSocket,
    ProviderMessage,
    SocketCallbacks,
    SocketFactory,
    TokenSource,
)
from .metrics import feed_metrics, log_exception_throttled

logger = logging.getLogger(__name__)

# Fyers rejects large subscribe payloads; send symbols in batches of this size.
MAX_SYMBOLS_PER_CALL = 25


class SessionState(StrEnum):
    IDLE = "idle"  # never started, or stopped without an explicit disconnect
    CONNECTING = "connecting"  # the runner is connecting or backing off
    CONNECTED = "connected"
    DROPPED = "dropped"  # the socket closed or failed after connecting
    FAILED = "failed"  # the runner gave up after its last attempt
    CLOSED = "closed"  # explicit disconnect


class ProviderTokenMissingError(RuntimeError):
    """The token source has no token: nobody is logged in to the provider."""


@dataclass(frozen=True)
class RetryPolicy:
    """Connect runner tuning."""

    max_attempts: int = 10
    connect_timeout_s: float = 15.0
    poll_s: float = 0.1
    backoff_base_s: float = 5.0
    backoff_max_s: float = 60.0

    def backoff_s(self, failures: int) -> float:
        """Pause after the `failures`-th failed attempt: 5, 10, 20, 40, then 60s."""
        return min(self.backoff_base_s * (2 ** (failures - 1)), self.backoff_max_s)


Spawn = Callable[[Callable[[], None]], None]


def spawn_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="provider-session", daemon=True).start()


def _unique(symbols: Iterable[str]) -> list[str]:
    return sorted({s for s in symbols if s})


class ProviderSession:
    """Connects, reconnects and replays one provider socket; see the module docstring."""

    def __init__(
        self,
        socket_factory: SocketFactory,
        token_source: TokenSource,
        desired_symbols: Callable[[], Iterable[str]],
        on_message: Callable[[ProviderMessage], None],
        *,
        retry: RetryPolicy | None = None,
        spawn: Spawn = spawn_daemon_thread,
        metrics_prefix: str = "feed.provider",
    ) -> None:
        self._socket_factory = socket_factory
        self._token_source = token_source
        self._desired_symbols = desired_symbols
        self._on_message = on_message
        self.retry = retry or RetryPolicy()
        self._spawn = spawn
        self._metrics_prefix = metrics_prefix
        # Guards the fields below. Never held while calling into a socket: closing a socket
        # joins the SDK threads, and those threads run our callbacks, which take this lock.
        self._lock = threading.Lock()
        self._state = SessionState.IDLE
        self._socket: DataSocket | None = None
        # Bumped whenever the current socket is let go of; callbacks carry the generation
        # of the socket they belong to, so late callbacks from an old socket are ignored.
        self._generation = 0
        self._replayed_generation = -1
        # Stop signal of the current runner; each start() gets a fresh event.
        self._stop = threading.Event()

    # --- state ------------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def has_socket(self) -> bool:
        with self._lock:
            return self._socket is not None

    def is_connected(self) -> bool:
        """Connected, and the socket agrees (a silent drop reads as not connected)."""
        with self._lock:
            socket = self._socket if self._state is SessionState.CONNECTED else None
        return socket is not None and self._socket_is_up(socket)

    @property
    def busy(self) -> bool:
        """A connect is running, or the socket is up: start() would do nothing."""
        return self.state is SessionState.CONNECTING or self.is_connected()

    # --- lifecycle --------------------------------------------------------------------

    def start(self) -> bool:
        """Connect on a background runner. False (and no new runner) when already busy."""
        if self.busy:
            return False
        with self._lock:
            if self._state is SessionState.CONNECTING:
                return False
            # A new runner supersedes any earlier one that may still be backing off.
            self._stop.set()
            stop = threading.Event()
            self._stop = stop
            self._state = SessionState.CONNECTING
        try:
            self._spawn(lambda: self._run(stop))
        except Exception:
            # No runner: do not stay CONNECTING, or nothing would ever start one again.
            with self._lock:
                stop.set()
                if self._stop is stop:
                    self._state = SessionState.FAILED
            raise
        return True

    def stop(self) -> None:
        """Stop the runner and close the socket (off-hours, shutdown); start() may follow."""
        self._teardown(SessionState.IDLE)

    def close(self) -> None:
        """Explicit disconnect: stop the runner and close the socket."""
        self._teardown(SessionState.CLOSED)

    def _teardown(self, final_state: SessionState) -> None:
        with self._lock:
            self._stop.set()
            self._state = final_state
            socket = self._release_socket_locked()
        self._close_quietly(socket)

    def _release_socket_locked(self) -> DataSocket | None:
        socket, self._socket = self._socket, None
        self._generation += 1
        return socket

    # --- subscriptions ----------------------------------------------------------------

    def subscribe(self, symbols: Iterable[str]) -> bool:
        """Subscribe provider-format symbols; False when not connected (replay covers them)."""
        return self._send_if_connected("subscribe", symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> bool:
        """Unsubscribe provider-format symbols; False when not connected."""
        return self._send_if_connected("unsubscribe", symbols)

    def _send_if_connected(self, op: str, symbols: Iterable[str]) -> bool:
        with self._lock:
            socket = self._socket if self._state is SessionState.CONNECTED else None
        if socket is None:
            return False
        self._send(socket, op, symbols)
        return True

    @staticmethod
    def _send(socket: DataSocket, op: str, symbols: Iterable[str]) -> int:
        unique = _unique(symbols)
        send = socket.subscribe if op == "subscribe" else socket.unsubscribe
        for i in range(0, len(unique), MAX_SYMBOLS_PER_CALL):
            send(symbols=unique[i : i + MAX_SYMBOLS_PER_CALL], data_type=SYMBOL_UPDATE)
        return len(unique)

    # --- the connect runner -----------------------------------------------------------

    def _run(self, stop: threading.Event) -> None:
        """Connect with a bounded wait per attempt, retrying with backoff until `stop`."""
        failures = 0
        while not stop.is_set():
            try:
                self._attempt(stop)
                return
            except Exception as exc:
                failures += 1
                log_exception_throttled(
                    logger,
                    "provider_session.connect",
                    "[WS] Provider connect attempt %s/%s failed: %s",
                    failures,
                    self.retry.max_attempts,
                    exc,
                )
                if failures >= self.retry.max_attempts:
                    break
                # Let go of the failed socket before backing off: otherwise an SDK
                # auto-reconnect could bring it up meanwhile, only for the next attempt
                # to close it again.
                self._release_unless_stopped(stop)
                stop.wait(self.retry.backoff_s(failures))
        with self._lock:
            if stop.is_set():
                return
            self._state = SessionState.FAILED
            socket = self._release_socket_locked()
        self._close_quietly(socket)
        self._count("failed")
        logger.error("[WS] Provider connection failed after %s attempts", failures)

    def _release_unless_stopped(self, stop: threading.Event) -> None:
        with self._lock:
            if stop.is_set():
                return  # the teardown that set `stop` released the socket already
            socket = self._release_socket_locked()
        self._close_quietly(socket)

    def _attempt(self, stop: threading.Event) -> None:
        """One connect attempt. Raises when the socket did not connect in time.

        Returns normally once connected, or when `stop` was set meanwhile.
        """
        self._count("connect_attempts")
        token = self._token_source()
        if not token:
            raise ProviderTokenMissingError("no provider token; log in first")
        with self._lock:
            if stop.is_set():
                return
            old = self._release_socket_locked()
            generation = self._generation
        self._close_quietly(old)
        socket = self._socket_factory(token, self._callbacks(generation))
        with self._lock:
            adopted = not stop.is_set() and generation == self._generation
            if adopted:
                self._socket = socket
        if not adopted:
            self._close_quietly(socket)
            return
        socket.connect()  # blocking; the SDK fires on_connect from inside
        if not self._wait_until_up(socket, stop):
            if stop.is_set():
                return
            raise ConnectionError(f"provider not connected within {self.retry.connect_timeout_s}s")
        # The SDK's on_connect normally replayed already; this covers it not firing.
        self._mark_connected(generation, first_only=True)

    def _wait_until_up(self, socket: DataSocket, stop: threading.Event) -> bool:
        """Poll until the socket reports connected; False on timeout or stop."""
        deadline = time.monotonic() + self.retry.connect_timeout_s
        while not stop.is_set():
            if self._socket_is_up(socket):
                return True
            if time.monotonic() >= deadline:
                return False
            stop.wait(self.retry.poll_s)
        return False

    def _mark_connected(self, generation: int, *, first_only: bool = False) -> None:
        """Enter CONNECTED and replay the wanted symbols (once per open of the socket)."""
        with self._lock:
            if generation != self._generation or self._stop.is_set():
                return
            if first_only and self._replayed_generation == generation:
                return
            self._replayed_generation = generation
            self._state = SessionState.CONNECTED
            socket = self._socket
        if socket is None:
            return
        self._count("connected")
        logger.info("[OK] Provider socket connected")
        # A failed replay must not fail the connect: the next reconnect replays again.
        self._guarded("replay", self._replay, socket)

    def _replay(self, socket: DataSocket) -> None:
        # CONNECTED is set before the snapshot, so a symbol added meanwhile is either in the
        # snapshot or subscribed directly by its caller.
        wanted = set(_unique(self._desired_symbols()))
        if not wanted:
            return
        logger.info("Replaying %s subscriptions on connect", len(wanted))
        self._send(socket, "subscribe", wanted)
        # A symbol released while the replay ran may have been unsubscribed before it.
        released = wanted - set(self._desired_symbols())
        if released:
            self._send(socket, "unsubscribe", released)

    # --- SDK callbacks (SDK threads) --------------------------------------------------

    def _callbacks(self, generation: int) -> SocketCallbacks:
        return SocketCallbacks(
            on_message=lambda message: self._handle_message(generation, message),
            on_connect=lambda: self._guarded("on_connect", self._handle_connect, generation),
            on_close=lambda message: self._guarded(
                "on_close", self._handle_close, generation, message
            ),
            on_error=lambda error: self._guarded("on_error", self._handle_error, generation, error),
        )

    def _handle_message(self, generation: int, message: ProviderMessage) -> None:
        if generation != self._generation:
            return
        try:
            self._on_message(message)
        except Exception as exc:
            log_exception_throttled(
                logger, "provider_session.on_message", "[WS] Message handler error: %s", exc
            )

    def _handle_connect(self, generation: int) -> None:
        """First connect and every SDK auto-reconnect: wait for the socket, then replay.

        The SDK fires on_connect right after (re)starting the socket, which can be before
        the handshake finished.
        """
        with self._lock:
            if generation != self._generation:
                return
            socket, stop = self._socket, self._stop
        if socket is None or not self._wait_until_up(socket, stop):
            logger.debug("[WS] Provider on_connect fired but the socket is not up")
            return
        self._mark_connected(generation)

    def _handle_close(self, generation: int, _message: object) -> None:
        logger.info("[WS] Provider socket closed")
        self._mark_dropped(generation)

    def _handle_error(self, generation: int, error: object) -> None:
        logger.error("[WS] Provider socket error: %s", error)
        with self._lock:
            socket = self._socket if generation == self._generation else None
        # The SDK also reports non-fatal errors (a bad symbol) on a healthy socket.
        if socket is not None and not self._socket_is_up(socket):
            self._mark_dropped(generation)

    def _mark_dropped(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._state is not SessionState.CONNECTED:
                return
            self._state = SessionState.DROPPED
        self._count("dropped")

    @staticmethod
    def _guarded(name: str, handler: Callable[..., None], *args: object) -> None:
        """Run a callback; an exception is logged, never raised into the SDK thread."""
        try:
            handler(*args)
        except Exception as exc:
            log_exception_throttled(
                logger, f"provider_session.{name}", "[WS] %s handler error: %s", name, exc
            )

    # --- helpers ----------------------------------------------------------------------

    @staticmethod
    def _socket_is_up(socket: DataSocket) -> bool:
        try:
            return bool(socket.is_connected())
        except Exception:
            return False

    @staticmethod
    def _close_quietly(socket: DataSocket | None) -> None:
        if socket is None:
            return
        try:
            socket.close_connection()
        except Exception as exc:
            logger.debug("[WS] Closing the provider socket failed: %s", exc)

    def _count(self, name: str) -> None:
        feed_metrics.increment(f"{self._metrics_prefix}.{name}")
