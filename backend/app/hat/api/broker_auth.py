"""The daily broker login as four routes per broker (K2, owner default Q27): `status`, `url`,
`token` and `disconnect`, built by `auth_router` for Fyers (`api/fyers_auth.py`) and Kite
(`api/kite_auth.py`) over that broker's login service (`FyersAuthService`, or the
`KiteAuthService` built on it).

A thin HTTP mapping: `url` builds the broker's login link, `token` trades the code the owner pastes
(Fyers' `auth_code`, Kite's `request_token`) for the day's token and saves it, `status` says whether
a usable token is saved and whether this broker is the active order broker (`HAT_BROKER`; Fyers is
logged in for market data either way), and `disconnect` removes the token. Every answer is a fixed
message: exception text, the token, the secret and the code never reach the client or the log, and
a failed exchange writes no token.

The two POST routes of each broker are HAT's only writes (HAT-DEC-T3). Each depends on
`guard.frontend_write`, which refuses a request that did not come from the HAT page (HAT-DEC-BD5).
The service itself refuses a login for a user other than the broker's active account (`token`
answers 403), so no page can plant a token for another account. With no active account of the
broker yet (before onboarding, which needs the first token) any user is accepted.
"""

import logging
from collections.abc import Callable
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.hat.api.guard import frontend_write
from app.hat.execution.fyers_auth import (
    AccountsUnreadableError,
    FyersAuthError,
    FyersAuthService,
    MissingCredentialsError,
    TokenRejectedError,
    WrongAccountError,
)
from app.hat.execution.fyers_tokens import TokenStoreError
from app.hat.settings import active_broker

__all__ = [
    "AuthCodeRequest",
    "BrokerStatusResponse",
    "LoginUrlResponse",
    "MessageResponse",
    "RouteNames",
    "auth_router",
]

logger = logging.getLogger(__name__)


class AuthCodeRequest(BaseModel):
    auth_code: str  # the code the broker's redirect carries (Kite's request_token)


class BrokerStatusResponse(BaseModel):
    connected: bool
    user_id: str | None = None
    error: str | None = None
    active: bool = False  # this broker is the active order broker (`HAT_BROKER`)


class LoginUrlResponse(BaseModel):
    url: str


class MessageResponse(BaseModel):
    status: str
    message: str


class RouteNames(NamedTuple):
    """The route names (and so the OpenAPI operation ids) of one broker's four routes."""

    status: str
    url: str
    exchange: str
    disconnect: str


def _is_active(broker: str) -> bool:
    try:
        return active_broker() == broker
    except Exception:  # an unknown HAT_BROKER: HAT refuses to start; no broker is active
        return False


def _missing(label: str) -> str:
    return f"{label} credentials not found in environment variables. Please check .env file."


def _exchange_failure(error: Exception, label: str) -> HTTPException:
    """The HTTP answer of a failed login exchange: the service's own message for a refusal,
    a fixed one otherwise (never the broker's text)."""
    if isinstance(error, MissingCredentialsError):
        return HTTPException(status_code=500, detail=_missing(label))
    if isinstance(error, TokenRejectedError):
        return HTTPException(status_code=400, detail=error.message)
    if isinstance(error, WrongAccountError):
        return HTTPException(status_code=403, detail=error.message)
    if isinstance(error, AccountsUnreadableError):
        # the broker accounts could not be read: fail closed, nothing was saved
        return HTTPException(status_code=503, detail=error.message)
    if isinstance(error, FyersAuthError):
        return HTTPException(status_code=500, detail=error.message)
    if isinstance(error, TokenStoreError):
        logger.error("%s token could not be stored", label, exc_info=error)
        return HTTPException(status_code=500, detail=f"The {label} token could not be saved")
    logger.error("%s token exchange failed", label, exc_info=error)
    return HTTPException(status_code=500, detail="Token exchange failed")


def auth_router(
    *,
    broker: str,
    label: str,
    service: Callable[[], FyersAuthService],
    names: RouteNames,
) -> APIRouter:
    """The four login routes of `broker` (named `label` in messages) under
    `/api/hat/auth/<broker>`, over the login service `service` returns."""
    router = APIRouter(prefix=f"/api/hat/auth/{broker}", tags=["HAT auth"])
    auth = Annotated[FyersAuthService, Depends(service)]
    status_error = f"Could not read {label} login status"
    missing = _missing(label)
    url_error = "Failed to generate authentication URL"
    disconnect_error = f"Failed to disconnect from {label}"

    @router.get("/status", name=names.status)
    def status(service: auth) -> BrokerStatusResponse:
        """Whether a usable token is saved (cached for 30 s), and whether this broker is the
        active order broker."""
        active = _is_active(broker)
        try:
            saved = service.status()
        except Exception:
            logger.exception("%s login status unreadable", label)
            return BrokerStatusResponse(connected=False, error=status_error, active=active)
        return BrokerStatusResponse(connected=saved.connected, user_id=saved.user_id, active=active)

    @router.get(
        "/url",
        name=names.url,
        responses={500: {"description": "Credentials missing or the link could not be built"}},
    )
    def login_url(service: auth) -> LoginUrlResponse:
        """The broker's login link, built from the app credentials in the environment."""
        try:
            return LoginUrlResponse(url=service.login_url())
        except MissingCredentialsError:
            raise HTTPException(status_code=500, detail=missing) from None
        except Exception:
            logger.exception("%s login url failed", label)
            raise HTTPException(status_code=500, detail=url_error) from None

    @router.post(
        "/token",
        name=names.exchange,
        dependencies=[Depends(frontend_write)],
        responses={
            400: {"description": "The broker rejected the code"},
            403: {"description": "Not from the HAT page, or not the HAT broker account's login"},
            503: {"description": "The broker accounts could not be read"},
            500: {"description": "Credentials missing, the exchange failed or no token was saved"},
        },
    )
    def exchange(request: AuthCodeRequest, service: auth) -> MessageResponse:
        """Exchange the pasted code for an access token and save it. Any failure writes no
        token."""
        try:
            service.exchange(request.auth_code)
        except Exception as error:
            raise _exchange_failure(error, label) from None
        return MessageResponse(status="success", message="Token generated and saved successfully")

    @router.post(
        "/disconnect",
        name=names.disconnect,
        dependencies=[Depends(frontend_write)],
        responses={
            403: {"description": "Not from the HAT page"},
            500: {"description": "The token could not be removed"},
        },
    )
    def disconnect(service: auth) -> MessageResponse:
        """Remove the saved token and drop the session."""
        try:
            service.disconnect()
        except Exception:
            logger.exception("%s disconnect failed", label)
            raise HTTPException(status_code=500, detail=disconnect_error) from None
        return MessageResponse(status="success", message=f"Disconnected from {label}")

    return router
