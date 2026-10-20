"""The Fyers login flow: login URL, auth-code exchange, status and disconnect.

``FyersAuthService`` owns the only ``SessionModel`` use, persists tokens through ``TokenStore``,
keeps the 30s login-status cache, and tells the rest of the app when the token changes (by
default it resets the in-memory Fyers client so the next call loads the new token).

Errors carry fixed messages only; the token, the secret and SDK exception text are never logged
or raised.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .token_store import TokenStore, token_store

logger = logging.getLogger(__name__)

STATUS_CACHE_SECONDS = 30.0
_SESSION_STATE = "fyers_api_state"
AUTH_CODE_REJECTED = "Fyers rejected the auth code. Please log in again."


class FyersAuthError(Exception):
    """A login-flow failure with a fixed, client-safe message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MissingCredentialsError(FyersAuthError):
    """CLIENT_ID, SECRET_KEY or REDIRECT_URI is not set."""


class TokenRejectedError(FyersAuthError):
    """Fyers did not return a token for the auth code."""


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

_CREDENTIAL_ENV = ("CLIENT_ID", "SECRET_KEY", "REDIRECT_URI")


def credentials_from_env() -> FyersCredentials:
    """Read the app credentials from the environment (loaded from .env at startup)."""
    values = {name: os.getenv(name) or "" for name in _CREDENTIAL_ENV}
    for name in _CREDENTIAL_ENV:
        if not values[name]:
            raise MissingCredentialsError(
                f"{name} not found in environment variables. Please add it to .env file."
            )
    return FyersCredentials(
        client_id=values["CLIENT_ID"],
        secret_key=values["SECRET_KEY"],
        redirect_uri=values["REDIRECT_URI"],
    )


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


class FyersAuthService:
    def __init__(
        self,
        store: TokenStore,
        *,
        session_factory: SessionFactory = _sdk_session,
        credentials: Callable[[], FyersCredentials] = credentials_from_env,
        on_token_change: Callable[[], None] = _reset_fyers_client,
        on_disconnect: Callable[[], None] = _disconnect_live_feed,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._session_factory = session_factory
        self._credentials = credentials
        self._on_token_change = on_token_change
        self._on_disconnect = on_disconnect
        self._clock = clock
        self._cached_status: FyersStatus | None = None
        self._cached_at = 0.0

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
        session.set_token(auth_code)
        reply = session.generate_token()
        logger.info("[Fyers Auth] Token exchange reply status: %s", reply.get("s", "unknown"))

        if reply.get("s") != "ok" or not reply.get("access_token"):
            # Only the reply's status code is logged; its message may echo request data.
            logger.warning("[Fyers Auth] Fyers rejected the auth code (code %s)", reply.get("code"))
            raise TokenRejectedError(AUTH_CODE_REJECTED)

        saved = self.store.save(reply, client_id=creds.client_id)
        logger.info("[Fyers Auth] Access token saved; expires at %s", saved.expires_at)
        self.invalidate_status()
        self._refresh_client()
        return FyersStatus(connected=True, user_id=saved.user_id)

    def status(self) -> FyersStatus:
        """Whether an unexpired token is saved, cached for STATUS_CACHE_SECONDS.

        Raises TokenStoreError when the file exists but cannot be read (not cached).
        """
        now = self._clock()
        if self._cached_status is not None and now - self._cached_at < STATUS_CACHE_SECONDS:
            return self._cached_status
        token = self.store.load()
        if token is None or token.is_expired():
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
        if self.store.clear():
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
