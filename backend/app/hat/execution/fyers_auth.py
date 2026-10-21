"""The Fyers login flow: login URL, auth-code exchange, status and disconnect (K2; cut down from
the legacy `FyersAuthService`, HAT-OPS-051).

`FyersAuthService` owns the only `SessionModel` use, persists tokens through `TokenStore`, keeps the
30 s login-status cache, and tells the rest of the process when the token changes through
injected hooks, so this module imports no feed and no HTTP layer:

- `token_change` runs after a token was saved or removed (the default drops the process-wide
  `FyersSession`, so the next call loads the new token);
- `login` runs after a successful login and `disconnect` before a logout removes the token (K3's
  live feed registers here).

A hook that raises is logged by its type and never blocks the login or the logout.

Fail closed: a refused code, a missing field, an SDK error, a reply that is not a mapping and a
token that cannot be saved all end in a fixed-message error and no token is written. The token,
the secret, the auth code and SDK exception text are never logged or raised.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .fyers_session import reset_fyers_session
from .fyers_tokens import TokenStore, token_store

__all__ = [
    "AUTH_CODE_REJECTED",
    "LOGIN_CANCELLED",
    "LOGIN_FAILED",
    "STATUS_CACHE_SECONDS",
    "FyersAuthError",
    "FyersAuthService",
    "FyersCredentials",
    "FyersStatus",
    "LoginCancelledError",
    "MissingCredentialsError",
    "TokenRejectedError",
    "credentials_from_env",
    "fyers_auth",
    "get_fyers_auth",
]

logger = logging.getLogger(__name__)

STATUS_CACHE_SECONDS = 30.0
_SESSION_STATE = "fyers_api_state"
AUTH_CODE_REJECTED = "Fyers rejected the auth code. Please log in again."
LOGIN_FAILED = "Fyers login failed. Please try again."
LOGIN_CANCELLED = "Login was cancelled by a logout. Please log in again."

Event = Literal["token_change", "login", "disconnect"]


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

    def __repr__(self) -> str:  # the secret never reaches a log or a traceback
        return f"FyersCredentials(client_id={self.client_id!r})"


@dataclass(frozen=True)
class FyersStatus:
    connected: bool
    user_id: str | None


class Session(Protocol):
    """The part of `fyersModel.SessionModel` the login flow uses."""

    def generate_authcode(self) -> str: ...
    def set_token(self, auth_code: str) -> None: ...
    def generate_token(self) -> Mapping[str, Any]: ...


SessionFactory = Callable[..., Session]

# Each credential: the name the login flow has always read first, then the names `.env.example`
# also documents (FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_CLIENT_ID).
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


class FyersAuthService:
    def __init__(
        self,
        store: TokenStore,
        *,
        session_factory: SessionFactory = _sdk_session,
        credentials: Callable[[], FyersCredentials] = credentials_from_env,
        on_token_change: Callable[[], None] = reset_fyers_session,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._session_factory = session_factory
        self._credentials = credentials
        self._clock = clock
        self._hooks: dict[Event, list[Callable[[], None]]] = {
            "token_change": [on_token_change],
            "login": [],
            "disconnect": [],
        }
        self._cached_status: FyersStatus | None = None
        self._cached_at = 0.0
        self._lock = threading.Lock()
        self._logouts = 0

    def add_hook(self, event: Event, hook: Callable[[], None]) -> None:
        """Run `hook` on `event` from now on (`token_change`, `login` or `disconnect`); a hook added
        twice runs once."""
        if hook not in self._hooks[event]:
            self._hooks[event].append(hook)

    def _run_hooks(self, event: Event) -> None:
        for hook in [*self._hooks[event]]:
            try:
                hook()
            except Exception as error:  # a hook never blocks the login or the logout
                logger.warning("Fyers %s hook failed: %s", event, type(error).__name__)

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

    def _generate_token(self, creds: FyersCredentials, auth_code: str) -> Mapping[str, Any]:
        """The token reply for `auth_code`, or a fixed-message error: an SDK failure is not
        quoted (its text can echo the request)."""
        try:
            session = self._session_factory(
                client_id=creds.client_id,
                secret_key=creds.secret_key,
                redirect_uri=creds.redirect_uri,
                response_type="code",
                grant_type="authorization_code",
            )
            session.set_token(auth_code)
            reply = session.generate_token()
        except Exception as error:
            logger.warning("Fyers token exchange failed: %s", type(error).__name__)
            raise FyersAuthError(LOGIN_FAILED) from None
        if not isinstance(reply, Mapping):
            logger.warning("Fyers token exchange returned no mapping")
            raise TokenRejectedError(AUTH_CODE_REJECTED)
        return reply

    def exchange(self, auth_code: str) -> FyersStatus:
        """Trade an auth code for a token, save it, and point the process at it.

        Raises MissingCredentialsError, TokenRejectedError, FyersAuthError (the SDK failed) or
        TokenStoreError (save failed); in each case no token was written.
        """
        creds = self._credentials()
        with self._lock:
            logouts_at_start = self._logouts
        reply = self._generate_token(creds, auth_code)
        logger.info("Fyers token exchange reply status: %s", reply.get("s", "unknown"))

        access_token = reply.get("access_token")
        if reply.get("s") != "ok" or not isinstance(access_token, str) or not access_token:
            # Only the reply's status code is logged; its message may echo request data.
            logger.warning("Fyers rejected the auth code (code %s)", reply.get("code"))
            raise TokenRejectedError(AUTH_CODE_REJECTED)

        # Save and logout are serialised: a logout that ran while Fyers was answering wins, so
        # this login cannot write the token back after the user logged out.
        with self._lock:
            if self._logouts != logouts_at_start:
                logger.info("Logout during login; the new token was discarded")
                raise LoginCancelledError(LOGIN_CANCELLED)
            saved = self.store.save(reply, client_id=creds.client_id)
        logger.info("Fyers access token saved; expires at %s", saved.expires_at)
        self.invalidate_status()
        self._run_hooks("token_change")
        self._run_hooks("login")
        return FyersStatus(connected=True, user_id=saved.user_id)

    def status(self) -> FyersStatus:
        """Whether a usable token is saved (`StoredToken.is_usable`, the same check the session
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
        """Run the disconnect hooks (best effort), delete the token file, clear the status cache
        and drop the session so nothing keeps calling Fyers with the old token."""
        with self._lock:
            self._logouts += 1  # from here a login still waiting for Fyers is cancelled
        self._run_hooks("disconnect")
        with self._lock:
            removed = self.store.clear()
        if removed:
            logger.info("Fyers access token removed")
        self.invalidate_status()
        self._run_hooks("token_change")


fyers_auth = FyersAuthService(token_store)
"""The process-wide login flow over the default token file."""


def get_fyers_auth() -> FyersAuthService:
    """The process-wide login flow; the HAT auth route depends on it, tests override it."""
    return fyers_auth
