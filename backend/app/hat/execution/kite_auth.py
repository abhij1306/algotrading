"""The Kite (Zerodha) login flow: login URL, request-token exchange, status and disconnect (as
`fyers_auth` is for Fyers, HAT-OPS-051).

`KiteAuthService` is a `FyersAuthService` with Kite's link and exchange: the status cache, the
logout that cancels a login still waiting for the broker, the hooks and the account check
(HAT-DEC-BD5) are the same code. Every login is checked against the active zerodha broker
accounts: a token is saved only when there is none or the token's Kite user (the session reply's
`user_id`) is one of their `account_ref`s.

The owner opens the login link, logs in at Kite, and Kite redirects to `KITE_REDIRECT_URI` with a
`request_token` in the address; the owner pastes it and `exchange` trades it, with the API secret,
for the day's access token. Credentials come from `KITE_API_KEY`, `KITE_API_SECRET` and
`KITE_REDIRECT_URI` only.

Fail closed: a refused request token, a reply without a token or a user, an SDK error and a
token that cannot be saved all end in a fixed-message error and no token is written. The token,
the secret, the request token and SDK exception text are never logged or raised.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..models import HatBrokerAccount
from ..models.strategy import ACCOUNT_ACTIVE, BROKER_ZERODHA
from .fyers_auth import (
    AccountRefs,
    FyersAuthError,
    FyersAuthService,
    FyersStatus,
    MissingCredentialsError,
    TokenRejectedError,
    WrongAccountError,
)
from .fyers_tokens import TokenStore
from .kite_session import kite_model, reset_kite_session
from .kite_tokens import KITE_USER_KEY, kite_token_store

__all__ = [
    "KITE_LOGIN_FAILED",
    "REQUEST_CODE_REJECTED",
    "KiteAuthService",
    "KiteCredentials",
    "active_zerodha_refs",
    "get_kite_auth",
    "kite_credentials_from_env",
    "kite_auth",
    "zerodha_account_refs",
]

logger = logging.getLogger(__name__)

REQUEST_CODE_REJECTED = "Kite rejected the request token. Please log in again."
KITE_LOGIN_FAILED = "Kite login failed. Please try again."
KITE_WRONG_ACCOUNT = (
    "This Kite login is not the HAT broker account. Please log in with that account."
)
# the HTTP statuses Kite answers a bad, used or expired request token with (InputException 400,
# TokenException 403): a refusal, not an outage
_REFUSED_STATUSES = frozenset({400, 403})

_CREDENTIAL_ENV = {
    "api_key": "KITE_API_KEY",
    "api_secret": "KITE_API_SECRET",
    "redirect_uri": "KITE_REDIRECT_URI",
}


@dataclass(frozen=True)
class KiteCredentials:
    api_key: str
    api_secret: str
    redirect_uri: str

    def __repr__(self) -> str:  # the secret never reaches a log or a traceback
        return f"KiteCredentials(api_key={self.api_key!r})"


def kite_credentials_from_env() -> KiteCredentials:
    """The Kite app credentials from the environment (loaded from .env at startup);
    MissingCredentialsError naming the first one missing."""
    values: dict[str, str] = {}
    for field_name, name in _CREDENTIAL_ENV.items():
        value = (os.getenv(name) or "").strip()
        if not value:
            raise MissingCredentialsError(
                f"{name} not found in environment variables. Please add it to .env file."
            )
        values[field_name] = value
    return KiteCredentials(**values)


def zerodha_account_refs(session: DbSession) -> frozenset[str]:
    """The `account_ref` (the Kite user id) of every active zerodha broker account."""
    return frozenset(
        session.scalars(
            select(HatBrokerAccount.account_ref).where(
                HatBrokerAccount.broker == BROKER_ZERODHA,
                HatBrokerAccount.status == ACCOUNT_ACTIVE,
            )
        )
    )


def active_zerodha_refs() -> frozenset[str]:
    """`zerodha_account_refs` in a session scope of its own, closed before the token is saved."""
    from app.database import db_session_scope  # deferred: importing it connects the engine

    with db_session_scope() as session:
        return zerodha_account_refs(session)


def session_reply_fields(reply: object) -> tuple[str, str]:
    """(access token, Kite user id) of a `generate_session` reply; TokenRejectedError when it is
    not a mapping or either is missing or empty."""
    if not isinstance(reply, Mapping):
        raise TokenRejectedError(REQUEST_CODE_REJECTED)
    token, user_id = reply.get("access_token"), reply.get(KITE_USER_KEY)
    if not isinstance(token, str) or not token or not isinstance(user_id, str) or not user_id:
        raise TokenRejectedError(REQUEST_CODE_REJECTED)
    return token, user_id


class KiteAuthService(FyersAuthService):
    def __init__(
        self,
        store: TokenStore,
        *,
        account_refs: AccountRefs,
        credentials: Callable[[], KiteCredentials] = kite_credentials_from_env,
        model_factory: Callable[..., Any] = kite_model,
        on_token_change: Callable[[], None] = reset_kite_session,
    ) -> None:
        super().__init__(store, account_refs=account_refs, on_token_change=on_token_change)
        self._kite_credentials = credentials
        self._kite_model = model_factory

    def login_url(self) -> str:
        """Kite's login link for the app (`https://kite.zerodha.com/connect/login?api_key=..`),
        built by the SDK without a network call."""
        creds = self._kite_credentials()
        return str(self._kite_model(api_key=creds.api_key).login_url())

    def _session_reply(self, creds: KiteCredentials, request_token: str) -> object:
        """The `generate_session` reply for `request_token`, or a fixed-message error: Kite's
        refusal (HTTP 400 or 403) is TokenRejectedError, anything else a failed login; the SDK's
        text is not quoted (it can echo the request)."""
        try:
            model = self._kite_model(api_key=creds.api_key)
            return model.generate_session(request_token, api_secret=creds.api_secret)
        except Exception as error:
            logger.warning("Kite token exchange failed: %s", type(error).__name__)
            if getattr(error, "code", None) in _REFUSED_STATUSES:
                raise TokenRejectedError(REQUEST_CODE_REJECTED) from None
            raise FyersAuthError(KITE_LOGIN_FAILED) from None

    def exchange(self, auth_code: str) -> FyersStatus:
        """Trade a Kite request token (`auth_code`, as the route names every broker's code) for
        the day's access token, check its Kite user against the zerodha broker accounts, save it,
        and point the process at it. Raises as `FyersAuthService.exchange` does; in each case no
        token was written."""
        creds = self._kite_credentials()
        logouts_at_start = self._logouts_now()
        access_token, user_id = session_reply_fields(self._session_reply(creds, auth_code))
        if not self._accepts(user_id):
            logger.warning("Kite login refused: the token is not for the HAT broker account")
            raise WrongAccountError(KITE_WRONG_ACCOUNT)
        # only what HAT reads back is kept: never the refresh or public token, nor the profile
        kept = {"access_token": access_token, KITE_USER_KEY: user_id}
        return self._save_login(kept, creds.api_key, logouts_at_start)


kite_auth = KiteAuthService(kite_token_store, account_refs=active_zerodha_refs)
"""The process-wide Kite login flow over the default token file."""


def get_kite_auth() -> KiteAuthService:
    """The process-wide Kite login flow; the zerodha auth route depends on it, tests override
    it."""
    return kite_auth
