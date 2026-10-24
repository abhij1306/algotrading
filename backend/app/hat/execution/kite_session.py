"""The Kite (Zerodha) SDK session: the token file read through `kite_tokens`, and the
`KiteConnect` client built from it (as `fyers_session` is for Fyers, HAT-OPS-051).

The session fails closed. A token that is missing, unreadable, without an API key or past its
06:00 IST expiry is never used: `kite` answers None, so every broker call fails as not logged in.
The token file is checked on every access (one `stat`), so a new login or a logout reaches the
next call without a restart, and `reset_kite_session` (the login flow's token-change hook) drops
the client at once. Logs name the file, never the token.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ..clock import SYSTEM_CLOCK, Clock
from .fyers_tokens import StoredToken, TokenStore, TokenStoreError
from .kite_tokens import kite_token_store

__all__ = [
    "KITE_HTTP_TIMEOUT",
    "KiteSession",
    "get_kite_session",
    "kite_model",
    "reset_kite_session",
]

logger = logging.getLogger(__name__)

# The (connect, read) seconds of every Kite HTTP request: the guard gives up on a call after a
# second or two; this ends the request itself, so its worker thread comes back.
KITE_HTTP_TIMEOUT = (3.05, 10.0)

ModelFactory = Callable[..., Any]
_UNREAD = object()  # the token file's state before the first read (a file may have no mtime)


def kite_model(*, api_key: str, access_token: str | None = None) -> Any:  # noqa: ANN401
    """A `KiteConnect` client from `kite_ipv4.sdk` (so its requests leave over IPv4), with
    `KITE_HTTP_TIMEOUT` on every request; untyped like the SDK."""
    from .kite_ipv4 import sdk

    return sdk().KiteConnect(api_key=api_key, access_token=access_token, timeout=KITE_HTTP_TIMEOUT)


class KiteSession:
    """One `KiteConnect` client for the token on disk. `store`, `clock` and `model_factory` are
    injectable so a test runs it on a temporary file, a frozen clock and a stand-in; production
    passes none."""

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
        model_factory: ModelFactory = kite_model,
    ) -> None:
        self._store = store if store is not None else kite_token_store
        self._clock = clock
        self._model_factory = model_factory
        self._lock = threading.Lock()
        self._mtime: object = _UNREAD
        self._token: StoredToken | None = None
        self._model: Any = None

    @property
    def kite(self) -> Any:  # noqa: ANN401 - the untyped KiteConnect client
        """The client for the saved token, or None when there is no usable token."""
        with self._lock:
            try:
                mtime = self._store.mtime_ns()
                if mtime != self._mtime:
                    self._model = None
                    self._token = None if mtime is None else self._store.load()
                    self._mtime = mtime
            except TokenStoreError:
                logger.warning("Kite token file unreadable; no Kite session")
                self.reset_locked()
                return None
            token = self._token
            if token is None or not token.is_usable(self._clock.now()):
                self._model = None
                return None
            if self._model is None:
                self._model = self._model_factory(
                    api_key=token.client_id, access_token=token.access_token
                )
            return self._model

    @property
    def user_id(self) -> str | None:
        """The Kite user id of the token in use, if any."""
        return self._token.user_id if self.kite is not None and self._token else None

    def reset(self) -> None:
        """Forget the token and the client, so the next access reads the file again."""
        with self._lock:
            self.reset_locked()

    def reset_locked(self) -> None:
        self._mtime = _UNREAD
        self._token = None
        self._model = None


_session = KiteSession()


def get_kite_session() -> KiteSession:
    """The process-wide Kite session over the default token file."""
    return _session


def reset_kite_session() -> None:
    """Drop the process-wide session's client so the next call reloads the token file."""
    _session.reset()
