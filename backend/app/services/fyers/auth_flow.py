"""The Fyers login flow: login URL, auth-code exchange, status and disconnect.

``FyersAuthService`` owns the only ``SessionModel`` use, persists tokens through ``TokenStore``,
keeps the 30s login-status cache, and tells the rest of the app when the token changes (by
default it resets the in-memory Fyers client so the next call loads the new token).

Errors carry fixed messages only; the token, the secret and SDK exception text are never logged
or raised.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .token_store import TokenStore, token_store

logger = logging.getLogger(__name__)

STATUS_CACHE_SECONDS = 30.0
_SESSION_STATE = "fyers_api_state"
AUTH_CODE_REJECTED = "Fyers rejected the auth code. Please log in again."
LOGIN_CANCELLED = "Login was cancelled by a logout. Please log in again."


class FyersAuthError(Exception):
    """A login-flow failure with a fixed, client-safe message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MissingCredentialsError(FyersAuthError):
    """CLIENT_ID, SECRET_KEY or REDIRECT_URI is not set."""


class TokenRejectedError(FyersAuthError):
    """Fyers did not return a token for the auth code."""


class LoginCancelledError(TokenRejectedError):
    """A logout ran while this login was waiting for Fyers; its token was not saved."""


@dataclass(frozen=True)
class FyersCredentials:
    client_id: str
    secret_key: str
    redirect_uri: str


@dataclass(frozen=True)
class FyersStatus:
    connected: bool
    user_id: str | None


class Session(Protocol):
    """The part of ``fyersModel.SessionModel`` the login flow uses."""

    def generate_authcode(self) -> str: ...
    def set_token(self, auth_code: str) -> None: ...
    def generate_token(self) -> Mapping[str, Any]: ...


SessionFactory = Callable[..., Session]

# Each credential: the name the login flow has always read first, then the names
# .env.example also documents (FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_CLIENT_ID).
_CREDENTIAL_ENV: dict[str, tuple[str, ...]] = {
    "client_id": ("CLIENT_ID", "FYERS_APP_ID", "FYERS_CLIENT_ID"),
    "secret_key": ("SECRET_KEY", "FYERS_SECRET_KEY"),
    "redirect_uri": ("REDIRECT_URI",),
}


def credentials_from_env() -> FyersCredentials:
    """Read the app credentials from the environment (loaded from .env at startup)."""
    values: dict[str, str] = {}
    for field_name, names in _CREDENTIAL_ENV.items():
        value = next((os.getenv(name) for name in names if os.getenv(name)), None)
        if not value:
            raise MissingCredentialsError(
                f"{names[0]} not found in environment variables. Please add it to .env file."
            )
        values[field_name] = value
    return FyersCredentials(**values)


def _sdk_session(**kwargs: str) -> Session:
    from fyers_apiv3 import fyersModel

    return fyersModel.SessionModel(**kwargs)


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
    if live_market.ws_service is not None:
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


class FyersAuthService:
    def __init__(
        self,
        store: TokenStore,
        *,
        session_factory: SessionFactory = _sdk_session,
        credentials: Callable[[], FyersCredentials] = credentials_from_env,
        on_token_change: Callable[[], None] = _reset_fyers_client,
        on_disconnect: Callable[[], None] = _disconnect_live_feed,
        on_login: Callable[[], None] = _connect_live_feed,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._session_factory = session_factory
        self._credentials = credentials
        self._on_token_change = on_token_change
        self._on_disconnect = on_disconnect
        self._on_login = on_login
        self._clock = clock
        self._cached_status: FyersStatus | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()
        self._logouts = 0

    def login_url(self) -> str:
        creds = self._credentials()
        session = self._session_factory(
            client_id=creds.client_id,
            secret_key=creds.secret_key,
            redirect_uri=creds.redirect_uri,
            response_type="code",
            state=_SESSION_STATE,
            grant_type="authorization_code",
        )
        return session.generate_authcode()

    def exchange(self, auth_code: str) -> FyersStatus:
        """Trade an auth code for a token, save it, and point the app at it.

        Raises MissingCredentialsError, TokenRejectedError, or TokenStoreError (save failed).
        """
        creds = self._credentials()
        session = self._session_factory(
            client_id=creds.client_id,
            secret_key=creds.secret_key,
            redirect_uri=creds.redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        with self._lock:
            logouts_at_start = self._logouts
        session.set_token(auth_code)
        reply = session.generate_token()
        logger.info("[Fyers Auth] Token exchange reply status: %s", reply.get("s", "unknown"))

        if reply.get("s") != "ok" or not reply.get("access_token"):
            # Only the reply's status code is logged; its message may echo request data.
            logger.warning("[Fyers Auth] Fyers rejected the auth code (code %s)", reply.get("code"))
            raise TokenRejectedError(AUTH_CODE_REJECTED)

        # Save and logout are serialised: a logout that ran while Fyers was answering wins,
        # so this login cannot write the token back after the user logged out.
        with self._lock:
            if self._logouts != logouts_at_start:
                logger.info("[Fyers Auth] Logout during login; the new token was discarded")
                raise LoginCancelledError(LOGIN_CANCELLED)
            saved = self.store.save(reply, client_id=creds.client_id)
        logger.info("[Fyers Auth] Access token saved; expires at %s", saved.expires_at)
        self.invalidate_status()
        self._refresh_client()
        # A logout explicitly disconnected the live feed; logging in lifts that.
        try:
            self._on_login()
        except Exception as error:
            logger.warning(
                "[Fyers Auth] Could not reconnect the live feed: %s", type(error).__name__
            )
        return FyersStatus(connected=True, user_id=saved.user_id)

    def status(self) -> FyersStatus:
        """Whether a usable token is saved (StoredToken.is_usable, the same check FyersClient
        applies), cached for STATUS_CACHE_SECONDS.

        Raises TokenStoreError when the file exists but cannot be read (not cached).
        """
        now = self._clock()
        if self._cached_status is not None and now - self._cached_at < STATUS_CACHE_SECONDS:
            return self._cached_status
        token = self.store.load()
        if token is None or not token.is_usable():
            status = FyersStatus(connected=False, user_id=None)
        else:
            status = FyersStatus(connected=True, user_id=token.user_id or "")
        self._cached_status = status
        self._cached_at = now
        return status

    def invalidate_status(self) -> None:
        self._cached_status = None

    def disconnect(self) -> None:
        """Stop the live feed (best effort), delete the token file, clear the status cache and
        drop the in-memory client so nothing keeps calling Fyers with the old token."""
        try:
            self._on_disconnect()
        except Exception:
            logger.warning("[Fyers] WebSocket disconnect failed", exc_info=True)
        with self._lock:
            self._logouts += 1
            removed = self.store.clear()
        if removed:
            logger.info("[Fyers] Access token removed")
        self.invalidate_status()
        self._refresh_client()

    def _refresh_client(self) -> None:
        try:
            self._on_token_change()
        except Exception:
            logger.warning(
                "[Fyers Auth] Could not refresh the in-memory Fyers client", exc_info=True
            )


fyers_auth = FyersAuthService(token_store)
"""The process-wide login flow over the default token file."""


def get_fyers_auth() -> FyersAuthService:
    """FastAPI dependency; tests override it with a service over a temp file and a fake SDK."""
    return fyers_auth
