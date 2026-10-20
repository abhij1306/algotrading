"""The Fyers side of the live feed: the SDK data socket factory and the token source.

ProviderSession (services/live/provider_session.py) owns the socket's lifecycle; this module
only builds `fyers_apiv3` sockets and reads the current token.
"""

import logging

from .live.data_socket import DataSocket, SocketCallbacks

logger = logging.getLogger(__name__)

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None
    logger.warning("[FyersWS] fyers-apiv3 not installed. WebSocket features unavailable.")

# Auto-reconnect attempts the SDK makes on its own before it gives up on a socket.
SDK_RECONNECT_RETRIES = 10


class FyersWebSocketError(RuntimeError):
    """The Fyers data socket cannot be built: the SDK is not installed."""


def fyers_token() -> str | None:
    """The current "client_id:access_token" from the Fyers client, or None if logged out.

    Read on every connect, so a re-login reaches the next socket.
    """
    from .fyers_client import get_fyers_client

    client = get_fyers_client()
    client_id = getattr(client, "client_id", None)
    access_token = getattr(client, "access_token", None)
    if not client_id or not access_token:
        return None
    return f"{client_id}:{access_token}"


def fyers_socket_factory(token: str, callbacks: SocketCallbacks) -> DataSocket:
    """A Fyers SDK data socket with the SDK's own auto-reconnect on."""
    if data_ws is None:
        raise FyersWebSocketError("fyers-apiv3 not installed")
    return data_ws.FyersDataSocket(
        access_token=token,
        log_path="",  # no SDK log files
        litemode=False,
        write_to_file=False,
        reconnect=True,
        on_connect=callbacks.on_connect,
        on_close=callbacks.on_close,
        on_error=callbacks.on_error,
        on_message=callbacks.on_message,
        reconnect_retry=SDK_RECONNECT_RETRIES,
    )
