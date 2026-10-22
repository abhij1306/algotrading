"""The daily Fyers OAuth login, as four routes under `/api/hat/auth/fyers` (K2, owner default Q27).

A thin HTTP mapping over `FyersAuthService`: `url` builds the Fyers login link, `token` trades the
auth code for a token and saves it, `status` says whether a usable token is saved and `disconnect`
removes it. This is the one login and, through the service, the one token store.

Every answer is a fixed message. Exception text, the token, the secret and the auth code never reach
the client or the log, and a failed exchange writes no token.

The two POST routes are HAT's only writes (HAT-DEC-T3). Each depends on `guard.frontend_write`,
which refuses a request that did not come from the HAT page (HAT-DEC-BD5). `token` also refuses a
login for a Fyers user other than the active Fyers broker account, so no page can plant a token
for another account. With no active Fyers account yet (before onboarding, which needs the first
token) any user is accepted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.hat.api.guard import frontend_write
from app.hat.execution.fyers_auth import (
    FyersAuthError,
    FyersAuthService,
    MissingCredentialsError,
    TokenRejectedError,
    WrongAccountError,
    get_fyers_auth,
)
from app.hat.execution.fyers_tokens import TokenStoreError
from app.hat.models import HatBrokerAccount

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hat/auth/fyers", tags=["HAT auth"])

AuthService = Annotated[FyersAuthService, Depends(get_fyers_auth)]
Db = Annotated[Session, Depends(get_db)]
FYERS = "fyers"
ACCOUNT_ACTIVE = "active"  # `hat_broker_account.status` of an onboarded account

STATUS_ERROR = "Could not read Fyers login status"
MISSING_CREDENTIALS_ERROR = (
    "Fyers credentials not found in environment variables. Please check .env file."
)
URL_ERROR = "Failed to generate authentication URL"
EXCHANGE_ERROR = "Token exchange failed"
STORE_ERROR = "The Fyers token could not be saved"
DISCONNECT_ERROR = "Failed to disconnect from Fyers"


class AuthCodeRequest(BaseModel):
    auth_code: str


class FyersStatusResponse(BaseModel):
    connected: bool
    user_id: str | None = None
    error: str | None = None


class LoginUrlResponse(BaseModel):
    url: str


class MessageResponse(BaseModel):
    status: str
    message: str


@router.get("/status")
def fyers_status(service: AuthService) -> FyersStatusResponse:
    """Whether a usable Fyers token is saved. Cached for 30 s."""
    try:
        status = service.status()
    except Exception:
        logger.exception("Fyers login status unreadable")
        return FyersStatusResponse(connected=False, error=STATUS_ERROR)
    return FyersStatusResponse(connected=status.connected, user_id=status.user_id)


@router.get(
    "/url",
    responses={500: {"description": "Fyers credentials missing or the link could not be built"}},
)
def fyers_login_url(service: AuthService) -> LoginUrlResponse:
    """The Fyers login link, built from the app credentials in the environment."""
    try:
        return LoginUrlResponse(url=service.login_url())
    except MissingCredentialsError:
        raise HTTPException(status_code=500, detail=MISSING_CREDENTIALS_ERROR) from None
    except Exception:
        logger.exception("Fyers login url failed")
        raise HTTPException(status_code=500, detail=URL_ERROR) from None


def _account_check(db: Session) -> Callable[[str | None], bool]:
    """The `accept_user` of a login: the user id must be the `account_ref` of an active Fyers
    broker account, unless there is none yet."""
    refs = set(
        db.scalars(
            select(HatBrokerAccount.account_ref).where(
                HatBrokerAccount.broker == FYERS, HatBrokerAccount.status == ACCOUNT_ACTIVE
            )
        )
    )
    return lambda user_id: not refs or user_id in refs


@router.post(
    "/token",
    dependencies=[Depends(frontend_write)],
    responses={
        400: {"description": "Fyers rejected the auth code"},
        403: {"description": "Not from the HAT page, or not the HAT broker account's login"},
        500: {"description": "Credentials missing, the exchange failed or the token was not saved"},
    },
)
def exchange_auth_code(request: AuthCodeRequest, service: AuthService, db: Db) -> MessageResponse:
    """Exchange an auth code for an access token and save it. Any failure writes no token."""
    accept_user = _account_check(db)
    try:
        service.exchange(request.auth_code, accept_user=accept_user)
    except MissingCredentialsError:
        raise HTTPException(status_code=500, detail=MISSING_CREDENTIALS_ERROR) from None
    except TokenRejectedError as error:
        raise HTTPException(status_code=400, detail=error.message) from None
    except WrongAccountError as error:
        raise HTTPException(status_code=403, detail=error.message) from None
    except FyersAuthError as error:
        raise HTTPException(status_code=500, detail=error.message) from None
    except TokenStoreError:
        logger.exception("Fyers token could not be stored")
        raise HTTPException(status_code=500, detail=STORE_ERROR) from None
    except Exception:
        logger.exception("Fyers token exchange failed")
        raise HTTPException(status_code=500, detail=EXCHANGE_ERROR) from None
    return MessageResponse(status="success", message="Token generated and saved successfully")


@router.post(
    "/disconnect",
    dependencies=[Depends(frontend_write)],
    responses={
        403: {"description": "Not from the HAT page"},
        500: {"description": "The token could not be removed"},
    },
)
def fyers_disconnect(service: AuthService) -> MessageResponse:
    """Remove the saved Fyers token and drop the session."""
    try:
        service.disconnect()
    except Exception:
        logger.exception("Fyers disconnect failed")
        raise HTTPException(status_code=500, detail=DISCONNECT_ERROR) from None
    return MessageResponse(status="success", message="Disconnected from Fyers")
