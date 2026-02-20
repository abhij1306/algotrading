import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException
from fyers_apiv3 import fyersModel
from pydantic import BaseModel

router = APIRouter()

# Get base directory for saving access tokens
def get_base_dir():
    """Get the base directory of the project."""
    if getattr(sys, 'frozen', False):
        # Running as executable
        exe_dir = os.path.dirname(sys.executable)
        if os.path.exists(os.path.join(exe_dir, "fyers")):
            return exe_dir
        if os.path.exists(os.path.join(os.path.dirname(exe_dir), "fyers")):
            return os.path.dirname(exe_dir)
        return os.getcwd()
    else:
        # Development mode - go up from backend/app/routers/ to project root
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BASE_DIR = get_base_dir()
CONFIG_DIR = os.path.join(BASE_DIR, "fyers", "config")
ACCESS_TOKEN_FILE = os.path.join(CONFIG_DIR, "access_token.json")


class AuthCodeRequest(BaseModel):
    auth_code: str


# Simple cache for status (avoid repeated file checks)
_status_cache = {"connected": False, "user_id": None, "cached_at": 0}

@router.get("/fyers/status")
def get_fyers_status():
    """
    Check if Fyers is currently connected by verifying access token file exists.
    Cached for 30s to avoid repeated file I/O.
    """

    # Return cached result if less than 30s old
    if time.time() - _status_cache["cached_at"] < 30:
        return {
            "connected": _status_cache["connected"],
            "user_id": _status_cache["user_id"]
        }

    try:
        if os.path.exists(ACCESS_TOKEN_FILE):
            with open(ACCESS_TOKEN_FILE) as f:
                data = json.load(f)

            _status_cache["connected"] = True
            _status_cache["user_id"] = data.get("fy_id", "")
            _status_cache["cached_at"] = time.time()

            return {"connected": True, "user_id": data.get("fy_id", "")}
        else:
            _status_cache["connected"] = False
            _status_cache["user_id"] = None
            _status_cache["cached_at"] = time.time()

            return {"connected": False, "user_id": None}
    except Exception as e:
        print(f"[Fyers] Error checking status: {str(e)}")
        return {"connected": False, "user_id": None, "error": str(e)}


@router.post("/fyers/disconnect")
def disconnect_fyers():
    """
    Disconnect Fyers by removing the access token file and disconnecting WebSocket.
    """
    try:
        # Disconnect WebSocket first
        try:
            from ..services.live_market_service import live_market
            if live_market.ws_service:
                live_market.ws_service.disconnect()
                print("[Fyers] WebSocket disconnected")
        except Exception as ws_e:
            print(f"[Fyers] WebSocket disconnect warning: {ws_e}")

        # Remove token file
        if os.path.exists(ACCESS_TOKEN_FILE):
            os.remove(ACCESS_TOKEN_FILE)
            print("[Fyers] Access token removed")

        # Clear cache
        _status_cache["connected"] = False
        _status_cache["user_id"] = None
        _status_cache["cached_at"] = 0

        return {"status": "success", "message": "Disconnected from Fyers"}
    except Exception as e:
        print(f"[Fyers] Error disconnecting: {str(e)}")
        raise handle_api_error(e, "Failed to disconnect from Fyers")


@router.get("/fyers/url")
def get_fyers_auth_url():
    """
    Generate Fyers Login URL using credentials from environment variables.
    Credentials should be set in .env file: CLIENT_ID, SECRET_KEY, REDIRECT_URI
    """
    try:
        # Read credentials from environment variables (loaded from .env by main.py)
        client_id = os.getenv("CLIENT_ID")
        secret_key = os.getenv("SECRET_KEY")
        redirect_uri = os.getenv("REDIRECT_URI")

        # Validate that all required credentials are present
        if not client_id:
            raise HTTPException(
                status_code=500,
                detail="CLIENT_ID not found in environment variables. Please add it to .env file."
            )
        if not secret_key:
            raise HTTPException(
                status_code=500,
                detail="SECRET_KEY not found in environment variables. Please add it to .env file."
            )
        if not redirect_uri:
            raise HTTPException(
                status_code=500,
                detail="REDIRECT_URI not found in environment variables. Please add it to .env file."
            )

        print("[Fyers Auth] Generating auth URL...")

        # Create Fyers session
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            state="fyers_api_state",
            grant_type="authorization_code"
        )

        auth_url = session.generate_authcode()
        print("[Fyers Auth] Generated auth URL successfully")
        return {"url": auth_url}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[Fyers Auth] Error generating auth URL: {str(e)}")
        raise handle_api_error(e, "Failed to generate authentication URL")

@router.post("/fyers/token")
def generate_token(request: AuthCodeRequest):
    """
    Exchange auth code for access token and save to file.
    Uses credentials from environment variables (.env file).
    """
    try:
        # Read credentials from environment variables
        client_id = os.getenv("CLIENT_ID")
        secret_key = os.getenv("SECRET_KEY")
        redirect_uri = os.getenv("REDIRECT_URI")

        # Validate credentials
        if not all([client_id, secret_key, redirect_uri]):
            raise HTTPException(
                status_code=500,
                detail="Fyers credentials not found in environment variables. Please check .env file."
            )

        print("[Fyers Auth] Exchanging auth code for access token...")

        # Create Fyers session
        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )

        # Exchange auth code for token
        session.set_token(request.auth_code)
        response = session.generate_token()

        print(f"[Fyers Auth] Token generation response: {response.get('s', 'unknown')}")

        # Check if token generation was successful
        if response.get("s") == "ok" and "access_token" in response:
            # Add client_id to the response for later use
            response["client_id"] = client_id

            # Calculate expiry: midnight IST (00:00) next day
            # IST = UTC+5:30
            ist_offset = timedelta(hours=5, minutes=30)
            utc_now = datetime.now(UTC)
            ist_now = utc_now + ist_offset

            # Midnight tonight IST = tomorrow 00:00 IST
            midnight_ist = (ist_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

            # Convert back to UTC for storage
            expires_at_utc = midnight_ist - ist_offset
            response["expires_at"] = expires_at_utc.isoformat()

            # Also store IST midnight for display
            response["expires_at_ist"] = midnight_ist.isoformat() + "+05:30"

            # Ensure config directory exists
            os.makedirs(CONFIG_DIR, exist_ok=True)

            # Save token to file
            with open(ACCESS_TOKEN_FILE, "w") as f:
                json.dump(response, f, indent=4)

            print(f"[Fyers Auth] Access token saved to {ACCESS_TOKEN_FILE}")
            print(f"[Fyers Auth] Token expires at: {expires_at_utc.isoformat()}")
            try:
                # Ensure in-memory singleton does not keep stale token after re-login.
                from ..services.fyers_client import reset_fyers_client
                reset_fyers_client()
            except Exception as refresh_err:
                print(f"[Fyers Auth] Warning: could not reset in-memory Fyers client: {refresh_err}")
            return {"status": "success", "message": "Token generated and saved successfully"}
        else:
            error_msg = response.get("message", "Token generation failed")
            print(f"[Fyers Auth] Token generation failed: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[Fyers Auth] Error during token exchange: {str(e)}")
        raise handle_api_error(e, "Token exchange failed")
