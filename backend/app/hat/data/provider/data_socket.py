"""The provider data socket seam: what ProviderSession needs from the Fyers SDK socket.

The Fyers SDK's data socket class (built only in `fyers_websocket.py`) satisfies `DataSocket`
structurally, and
tests use a fake. The SDK takes its callbacks at construction, so a `SocketFactory` builds a
socket from a token and a `SocketCallbacks`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# The SDK data type for symbol (quote) updates.
SYMBOL_UPDATE = "SymbolUpdate"

ProviderMessage = dict[str, Any]


class DataSocket(Protocol):
    """One provider socket. Every method may block; none may be called holding a lock that
    the socket's callbacks take (closing joins the SDK threads that run them)."""

    def connect(self) -> None:
        """Open the socket. The SDK calls `on_connect` from inside, and again after each of
        its own auto-reconnects."""

    def close_connection(self) -> None:
        """Close the socket for good: no SDK auto-reconnect afterwards."""

    def is_connected(self) -> bool:
        """Whether the socket is open now."""
        ...

    def subscribe(self, symbols: list[str], data_type: str = SYMBOL_UPDATE) -> None:
        """Start streaming the provider-format symbols."""

    def unsubscribe(self, symbols: list[str], data_type: str = SYMBOL_UPDATE) -> None:
        """Stop streaming the provider-format symbols."""


@dataclass(frozen=True)
class SocketCallbacks:
    """The SDK callbacks; each runs on an SDK thread."""

    on_message: Callable[[ProviderMessage], None]
    on_connect: Callable[[], None]
    on_close: Callable[[object], None]
    on_error: Callable[[object], None]


# Builds a socket for a token ("client_id:access_token"); raises when the SDK is unavailable.
SocketFactory = Callable[[str, SocketCallbacks], DataSocket]
# The current provider token, or None when nobody is logged in. Called on every connect.
TokenSource = Callable[[], str | None]
