"""The Fyers login flow: login URL, auth-code exchange, status and disconnect (K2; cut down from
the legacy `FyersAuthService`, HAT-OPS-051).

`FyersAuthService` owns the only `SessionModel` use, persists tokens through `TokenStore`, keeps the
30 s login-status cache, and tells the rest of the process when the token changes through
injected hooks, so this module imports no feed and no HTTP layer:

- `token_change` runs after a token was saved or removed (the default drops the process-wide
  `FyersSession`, so the next call loads the new token);
- `login` runs after a successful login and `disconnect` before a logout removes the token (K3's
  live feed registers here).

Every login is checked against the broker accounts (HAT-DEC-BD5): the service is built with
`account_refs`, the `account_ref` of every active Fyers broker account, and saves a token only
when there is none or the token's Fyers user is one of them. The lookup is part of the service,
not an argument of `exchange`, so no caller (the HTTP route, a
later Telegram or job login) can skip it. It is read after Fyers has answered and just before
the save, so no connection is held while Fyers answers and an account onboarded meanwhile counts.

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
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import HatBrokerAccount
from ..models.strategy import ACCOUNT_ACTIVE, BROKER_FYERS
from .fyers_broker import profile_data
from .fyers_session import reset_fyers_session, sdk_model
from .fyers_tokens import TokenStore, token_store

__all__ = [
    "ACCOUNTS_UNREADABLE",
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
    "ACCOUNT_UNCONFIRMED",
    "WRONG_ACCOUNT",
    "AccountsUnreadableError",
    "WrongAccountError",
    "active_fyers_refs",
    "fyers_account_refs",
    "credentials_from_env",
    "fyers_auth",
    "get_fyers_auth",
]

logger = logging.getLogger(__name__)

STATUS_CACHE_SECONDS = 30.0
PROFILE_TIMEOUT_SECONDS = 5.0
# the profile read after a login runs here, so a hung SDK call cannot hold the login request
_PROFILE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fyers-profile")
_SESSION_STATE = "fyers_api_state"
AUTH_CODE_REJECTED = "Fyers rejected the auth code. Please log in again."
LOGIN_FAILED = "Fyers login failed. Please try again."
LOGIN_CANCELLED = "Login was cancelled by a logout. Please log in again."
WRONG_ACCOUNT = "This Fyers login is not the HAT broker account. Please log in with that account."
ACCOUNT_UNCONFIRMED = "Could not confirm which Fyers account logged in. Please log in again."
ACCOUNTS_UNREADABLE = "The database is unavailable; the Fyers login was not saved"

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


class WrongAccountError(FyersAuthError):
    """The token is for a Fyers user the caller does not accept (HAT-DEC-BD5)."""


class AccountsUnreadableError(FyersAuthError):
    """The broker accounts could not be read, so the login's user cannot be checked."""


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
AccountRefs = Callable[[], Collection[str]]

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


def fyers_account_refs(session: DbSession) -> frozenset[str]:
    """The `account_ref` (the Fyers user id) of every active Fyers broker account."""
    return frozenset(
        session.scalars(
            select(HatBrokerAccount.account_ref).where(
                HatBrokerAccount.broker == BROKER_FYERS,
                HatBrokerAccount.status == ACCOUNT_ACTIVE,
            )
        )
    )


def active_fyers_refs() -> frozenset[str]:
    """`fyers_account_refs` in a session scope of its own, closed before the token is saved."""
    from app.database import db_session_scope  # deferred: importing it connects the engine

    with db_session_scope() as session:
        return fyers_account_refs(session)


def _sdk_session(**kwargs: str) -> Session:
    from .fyers_ipv4 import sdk  # the login exchange leaves over IPv4 too (HAT-BRK-093)

    return sdk().SessionModel(**kwargs)


class FyersAuthService:
    def __init__(
        self,
        store: TokenStore,
        *,
        account_refs: AccountRefs,
        session_factory: SessionFactory = _sdk_session,
        credentials: Callable[[], FyersCredentials] = credentials_from_env,
        on_token_change: Callable[[], None] = reset_fyers_session,
        clock: Callable[[], float] = time.monotonic,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self._account_refs = account_refs
        self._session_factory = session_factory
        self._model_factory = model_factory
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

    def _profile_user_id(self, client_id: str, access_token: str) -> str | None:
        """The Fyers user id (`fy_id`) from the profile of a new token. The token reply carries
        none, so without this the login status has no user id. None when there is no model
        factory or the profile cannot be read in PROFILE_TIMEOUT_SECONDS; the login stands either
        way only while no Fyers broker account is active (`_accepts`). The reply is checked as
        every profile read is (`profile_data`)."""
        if self._model_factory is None:
            return None
        try:
            model = self._model_factory(client_id=client_id, token=access_token, log_path="")
            reply = _PROFILE_POOL.submit(model.get_profile).result(timeout=PROFILE_TIMEOUT_SECONDS)
            profile = profile_data(reply)
        except Exception as error:
            logger.warning("Fyers profile after login failed: %s", type(error).__name__)
            return None
        user_id = profile.get("fy_id")
        if not isinstance(user_id, str) or not user_id:
            logger.warning("Fyers profile after login has no user id")
            return None
        return user_id

    def _accepts(self, user_id: str | None) -> bool:
        """Whether the token's Fyers user (None: its profile could not be read) may log in: any
        user while no Fyers broker account is active (onboarding needs the first token), else
        only an active account's `account_ref`. Raises AccountsUnreadableError when the accounts
        cannot be read."""
        try:
            refs = self._account_refs()
        except Exception as error:
            logger.warning("Fyers login: broker accounts unreadable: %s", type(error).__name__)
            raise AccountsUnreadableError(ACCOUNTS_UNREADABLE) from None
        return not refs or user_id in refs

    def exchange(self, auth_code: str) -> FyersStatus:
        """Trade an auth code for a token, check its Fyers user against the broker accounts,
        save it, and point the process at it.

        A user the accounts refuse raises WrongAccountError, with ACCOUNT_UNCONFIRMED when the
        user id is unknown and WRONG_ACCOUNT otherwise.

        Raises MissingCredentialsError, TokenRejectedError, WrongAccountError,
        AccountsUnreadableError, FyersAuthError (the SDK failed) or TokenStoreError (save failed);
        in each case no token was written.
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

        user_id = self._profile_user_id(creds.client_id, access_token)
        if not self._accepts(user_id):
            if user_id is None:
                logger.warning("Fyers login refused: its account could not be confirmed")
                raise WrongAccountError(ACCOUNT_UNCONFIRMED)
            logger.warning("Fyers login refused: the token is not for the HAT broker account")
            raise WrongAccountError(WRONG_ACCOUNT)
        if user_id is not None:
            reply = {**reply, "fy_id": user_id}

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
            status = FyersStatus(connected=True, user_id=token.user_id)
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


fyers_auth = FyersAuthService(token_store, account_refs=active_fyers_refs, model_factory=sdk_model)
"""The process-wide login flow over the default token file."""


def get_fyers_auth() -> FyersAuthService:
    """The process-wide login flow; the HAT auth route depends on it, tests override it."""
    return fyers_auth
