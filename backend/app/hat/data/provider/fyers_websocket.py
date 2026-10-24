"""The Fyers side of the live feed: the SDK data socket factory.

ProviderSession (provider_session.py) owns the socket's lifecycle; this module
only builds `fyers_apiv3` sockets. The token the socket is built with comes from
HAT's Fyers session (`execution.fyers_session.fyers_token`), which `app/main.py`
hands the feed. It also hands the minute-history download (HAT-DATA-119) the
history request of HAT's Fyers session: a REST call on the same session, never a
second socket.
"""

import logging
from collections.abc import Callable, Mapping

from .data_socket import DataSocket, SocketCallbacks

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


def fyers_history() -> Callable[[dict[str, str]], Mapping[str, object]] | None:
    """The SDK's `history` request on HAT's Fyers session, or None when logged out or the token
    is refused (a network call: never on the event loop). Each request asks the session for its
    current model, so a re-login during a long download is picked up and an expired token is
    never used."""
    from app.hat.execution.fyers_session import get_fyers_session

    if not get_fyers_session().validate_token():  # also builds a model missing at startup
        return None

    def history(request: dict[str, str]) -> Mapping[str, object]:
        model = get_fyers_session().fyers
        if model is None:
            raise RuntimeError("logged out")  # the loader reports it as history_failed
        return model.history(data=request)

    return history


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
