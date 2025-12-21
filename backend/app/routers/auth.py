from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os
import json
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

router = APIRouter()

# Constants - Adjust paths to discover files in both dev and packaged modes
def get_base_dir():
    # If running as PyInstaller bundle, sys._MEIPASS is the temp folder with _internal
    # However, we want the folder where the executable or the script resides.
    if getattr(sys, 'frozen', False):
        # We are in an executable. BASE_DIR is usually the folder containing the .exe
        # but let's check one level up too (resources folder)
        exe_dir = os.path.dirname(sys.executable)
        # Try finding fyers folder nearby
        if os.path.exists(os.path.join(exe_dir, "fyers")): return exe_dir
        if os.path.exists(os.path.join(os.path.dirname(exe_dir), "fyers")): return os.path.dirname(exe_dir)
        # Fallback to current working directory
        return os.getcwd()
    else:
        # Development mode
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import sys
BASE_DIR = get_base_dir()
FYERS_DIR = os.path.join(BASE_DIR, "fyers")
CONFIG_DIR = os.path.join(FYERS_DIR, "config")
KEYS_FILE = os.path.join(CONFIG_DIR, "keys.env")
ACCESS_TOKEN_FILE = os.path.join(CONFIG_DIR, "access_token.json")


class AuthCodeRequest(BaseModel):
    auth_code: str

@router.get("/fyers/url")
async def get_fyers_auth_url():
    """
    Generate Fyers Login URL based on keys.env
    """
    try:
        # Load keys explicitly from the file
        if not os.path.exists(KEYS_FILE):
             return {"error": "keys.env not found in fyers/config"}
        
        # Manually load to avoid polluting global env if needed, or use dotenv
        env_vars = {}
        with open(KEYS_FILE) as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    k, v = line.strip().split('=', 1)
                    # Strip quotes and whitespace
                    v = v.strip().strip('"').strip("'")
                    env_vars[k] = v
        
        client_id = env_vars.get("CLIENT_ID")
        secret_key = env_vars.get("SECRET_KEY")
        redirect_uri = env_vars.get("REDIRECT_URI")
        
        if not all([client_id, secret_key, redirect_uri]):
             raise HTTPException(status_code=500, detail="Missing Fyers credentials in keys.env or file unreadable.")

        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            state="fyers_api_state",
            grant_type="authorization_code"
        )
        
        auth_url = session.generate_authcode()
        return {"url": auth_url}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/fyers/token")
async def generate_token(request: AuthCodeRequest):
    """
    Exchange auth code for access token and save to file
    """
    try:
        # Reload keys
        env_vars = {}
        if os.path.exists(KEYS_FILE):
             with open(KEYS_FILE) as f:
                for line in f:
                    if '=' in line and not line.startswith('#'):
                        k, v = line.strip().split('=', 1)
                        # Strip quotes and whitespace
                        v = v.strip().strip('"').strip("'")
                        env_vars[k] = v
                        
        client_id = env_vars.get("CLIENT_ID")
        secret_key = env_vars.get("SECRET_KEY")
        redirect_uri = env_vars.get("REDIRECT_URI")

        session = fyersModel.SessionModel(
            client_id=client_id,
            secret_key=secret_key,
            redirect_uri=redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )

        session.set_token(request.auth_code)
        response = session.generate_token()
        
        if response.get("s") == "ok" and "access_token" in response:
             # Add client_id as per fyers_login.py logic
             response["client_id"] = client_id
             
             os.makedirs(CONFIG_DIR, exist_ok=True)
             with open(ACCESS_TOKEN_FILE, "w") as f:
                 json.dump(response, f, indent=4)
                 
             return {"status": "success", "message": "Token generated and saved"}
        else:
             raise HTTPException(status_code=400, detail=response.get("message", "Token generation failed"))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
