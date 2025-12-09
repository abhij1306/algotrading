# fyers/data_feed.py
import os
import json
import time
from fyers_apiv3.FyersWebsocket import data_ws

# --- Configuration ---
ACCESS_TOKEN_FILE = "fyers/config/access_token.json"
SYMBOL_LIST = ["NSE:NIFTY50-INDEX", "NSE:SBIN-EQ"]


def load_websocket_token():
    """
    Loads the access token in the required WebSocket format: client_id:access_token.
    """
    if not os.path.exists(ACCESS_TOKEN_FILE):
        raise Exception("Access token file not found. Run fyers_login.py first.")
    with open(ACCESS_TOKEN_FILE, "r") as f:
        data = json.load(f)
        # We need the full client_id:access_token string for the WebSocket connection
        return f"{data.get('client_id')}:{data.get('access_token')}"


# --- WebSocket Handler Functions ---

def on_message(message):
    """Callback function to process incoming tick data."""
    print(f"Tick Received: {message}")

def on_error(message):
    """Callback function to handle WebSocket errors."""
    print(f"WebSocket Error: {message}")

def on_close(message):
    """Callback function when the connection closes."""
    print(f"Connection closed: {message}")


def start_websocket():
    """Initializes and connects the Fyers Data WebSocket."""
    try:
        access_token_with_app_id = load_websocket_token()
        
        # Define on_open inside to capture 'ws' instance
        def on_open():
            """
            Callback function executed when the WebSocket connection is established.
            """
            print("Connection Established. Subscribing to symbols...")
            
            # Use the 'ws' object from the outer scope
            ws.subscribe(
                data_type="symbolData",
                symbols=SYMBOL_LIST
            )
            print(f"Subscribed to: {', '.join(SYMBOL_LIST)}")

        # Initialize the FyersDataSocket
        ws = data_ws.FyersDataSocket(
            access_token=access_token_with_app_id,
            log_path="", 
            write_to_file=False,
            reconnect=True,
            # Assign the callback handlers
            on_connect=on_open,
            on_close=on_close,
            on_error=on_error,
            on_message=on_message 
        )

        # Establish connection
        print("Starting WebSocket connection...")
        ws.connect()
        
        
        # Keep the main thread alive so the background WebSocket thread can continue
        print("Listening for ticks. Press Ctrl+C to exit.")
        while True:
            time.sleep(10)

    except Exception as e:
        print(f"\nFatal error in websocket connection: {e}")


if __name__ == "__main__":
    start_websocket()