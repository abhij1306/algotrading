"""Fyers login endpoints: thin HTTP mapping over ``FyersAuthService``.

Every error carries a fixed message; exception text, the token and the secret never reach the
client.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.fyers.auth_flow import (
    FyersAuthService,
    MissingCredentialsError,
    TokenRejectedError,
    get_fyers_auth,
)
from app.utils.errors import handle_api_error

logger = logging.getLogger(__name__)

router = APIRouter()

AuthService = Annotated[FyersAuthService, Depends(get_fyers_auth)]

AUTH_URL_RESPONSES = {
    500: {"description": "Fyers credentials missing or auth URL generation failed"}
}
TOKEN_RESPONSES = {
    400: {"description": "Fyers rejected the auth code"},
    500: {"description": "Fyers credentials missing or token exchange failed"},
}

STATUS_ERROR = "Could not read Fyers login status"
MISSING_CREDENTIALS_ERROR = (
    "Fyers credentials not found in environment variables. Please check .env file."
)


class AuthCodeRequest(BaseModel):
    auth_code: str


@router.get("/fyers/status", response_model=None)
def get_fyers_status(service: AuthService) -> dict[str, Any]:
    """Whether a Fyers access token is saved. Cached for 30s to avoid repeated file I/O."""
    try:
        status = service.status()
    except Exception:
        logger.exception("[Fyers] Error checking status")
        return {"connected": False, "user_id": None, "error": STATUS_ERROR}
    return {"connected": status.connected, "user_id": status.user_id}


@router.post("/fyers/disconnect", response_model=None)
def disconnect_fyers(service: AuthService) -> dict[str, Any]:
    """Disconnect Fyers: stop the WebSocket and remove the access token file."""
    try:
        service.disconnect()
    except Exception as e:
        raise handle_api_error(e, "Failed to disconnect from Fyers") from e
    return {"status": "success", "message": "Disconnected from Fyers"}


@router.get("/fyers/url", response_model=None, responses=AUTH_URL_RESPONSES)
def get_fyers_auth_url(service: AuthService) -> dict[str, Any]:
    """Generate the Fyers login URL from CLIENT_ID, SECRET_KEY and REDIRECT_URI in .env."""
    try:
        url = service.login_url()
    except MissingCredentialsError as e:
        raise HTTPException(status_code=500, detail=e.message) from e
    except Exception as e:
        raise handle_api_error(e, "Failed to generate authentication URL") from e
    logger.info("[Fyers Auth] Generated auth URL successfully")
    return {"url": url}


@router.post("/fyers/token", response_model=None, responses=TOKEN_RESPONSES)
def generate_token(request: AuthCodeRequest, service: AuthService) -> dict[str, Any]:
    """Exchange an auth code for an access token and save it."""
    try:
        service.exchange(request.auth_code)
    except MissingCredentialsError as e:
        raise HTTPException(status_code=500, detail=MISSING_CREDENTIALS_ERROR) from e
    except TokenRejectedError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    except Exception as e:
        raise handle_api_error(e, "Token exchange failed") from e
    return {"status": "success", "message": "Token generated and saved successfully"}
