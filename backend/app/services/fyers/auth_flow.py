"""What the legacy app hangs on HAT's Fyers login (`app.hat.execution.fyers_auth`): after a login
or a token change it resets the legacy `FyersClient`, reconnects the legacy live feed, and a logout
disconnects that feed first. HAT's auth service imports no feed, so the legacy side registers these
hooks (`install_legacy_hooks`, called once at startup). K3 and K4 delete them with the legacy feed
and client.
"""

import asyncio
import concurrent.futures
import logging

from app.hat.execution.fyers_auth import FyersAuthService, get_fyers_auth

logger = logging.getLogger(__name__)


def _reset_fyers_client() -> None:
    from app.services.fyers_client import reset_fyers_client

    reset_fyers_client()


def _disconnect_live_feed() -> None:
    """The same explicit disconnect as POST /api/websocket/disconnect: closes the socket, stops
    the runner and keeps the feed down until an explicit connect, so a runner in flight cannot
    reconnect on its own after a later login."""
    from app.services.live_market_service import live_market

    live_market.disconnect()
    logger.info("[Fyers] Live feed disconnected")


def _connect_live_feed() -> None:
    """The same explicit connect as POST /api/websocket/connect, scheduled on the app's event
    loop (the login route runs in a worker thread). Market hours and DEV_MODE apply as usual.

    Fire and forget: the login response does not wait for the provider, and the outcome is
    logged in one line.
    """
    from app.services.live_market_service import live_market

    loop = live_market.loop
    if loop is None or loop.is_closed():
        logger.info("[Fyers] Live feed not reconnected after login: no app event loop yet")
        return
    if live_market.has_provider_socket():
        # The open socket was built with the previous token. Close it so the explicit connect
        # below builds a new socket from the refreshed client (and so the new token).
        live_market.disconnect()
    future = asyncio.run_coroutine_threadsafe(
        live_market.connect_async(loop=loop, explicit=True), loop
    )
    future.add_done_callback(_log_connect_outcome)


def _log_connect_outcome(future: concurrent.futures.Future[str]) -> None:
    error = future.exception()
    if error is not None:
        logger.warning("[Fyers] Live feed reconnect after login failed: %s", type(error).__name__)
    else:
        logger.info("[Fyers] Live feed reconnect after login: %s", future.result())


def install_legacy_hooks(service: FyersAuthService | None = None) -> None:
    """Register the legacy client reset and feed hooks on `service` (the process-wide one by
    default). Idempotent."""
    service = service if service is not None else get_fyers_auth()
    service.add_hook("token_change", _reset_fyers_client)
    service.add_hook("login", _connect_live_feed)
    service.add_hook("disconnect", _disconnect_live_feed)
