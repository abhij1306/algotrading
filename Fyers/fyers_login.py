import os
import json
import webbrowser
from dotenv import load_dotenv
from fyers_apiv3 import fyersModel

# --- Configuration Setup ---
# Loads CLIENT_ID, SECRET_KEY, REDIRECT_URI from config/keys.env
load_dotenv("config/keys.env")

# Load credentials
CLIENT_ID = os.getenv("CLIENT_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
REDIRECT_URI = os.getenv("REDIRECT_URI")
CONFIG_DIR = "config"
ACCESS_TOKEN_FILE = os.path.join(CONFIG_DIR, "access_token.json")


def generate_access_token():
    """
    Handles the three-step OAuth flow to generate and save the access token.
    """
    if not all([CLIENT_ID, SECRET_KEY, REDIRECT_URI]):
        print("❌ Error: One or more environment variables are missing.")
        return None

    try:
        # Step 1: Initialize the Session Model
        # NOTE: grant_type="authorization_code" is crucial for the token exchange.
        session = fyersModel.SessionModel(
            client_id=CLIENT_ID,
            secret_key=SECRET_KEY,
            redirect_uri=REDIRECT_URI,
            response_type="code",
            state="fyers_api_state",
            grant_type="authorization_code"
        )

        # Step 2: Generate Auth Code URL and open browser
        auth_url = session.generate_authcode()
        print(f"Login URL: {auth_url}")
        webbrowser.open(auth_url)
        

        # Step 3: Get the Auth Code manually
        print("\n--- Manual Step Required ---")
        auth_code = input("After login, paste the 'auth_code' from the URL here: ")
        print("----------------------------\n")

        # Step 4: Exchange auth_code for access_token
        session.set_token(auth_code)
        token_response = session.generate_token()

        print("Token Response:", token_response)

        # Step 5: Save the token if successful
        if token_response.get("s") == "ok" and "access_token" in token_response:
            
            # CRITICAL: Add the client_id to the dictionary for use by FyersModel and WebSocket
            token_response["client_id"] = CLIENT_ID
            
            # Ensure config directory exists
            os.makedirs(CONFIG_DIR, exist_ok=True)
            
            with open(ACCESS_TOKEN_FILE, "w") as f:
                json.dump(token_response, f, indent=4)

            print(f"\nAccess Token Saved Successfully to {ACCESS_TOKEN_FILE}")
            return token_response["access_token"]
        else:
            print("\nFailed to generate access token.")
            print(f"   Error: {token_response.get('message', 'Unknown error')}")
            return None

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
        return None


if __name__ == "__main__":
    generate_access_token()