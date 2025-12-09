#!/usr/bin/env python3
import sys
import os
sys.path.append(os.getcwd())

from fyers.fyers_client import load_access_token, load_client_id
from fyers_apiv3 import fyersModel

# Load tokens
access_token = load_access_token()
client_id = load_client_id()

print(f"Client ID: {client_id}")
print(f"Access Token (first 50 chars): {access_token[:50]}...")
print(f"Access Token (last 20 chars): ...{access_token[-20:]}")

# Try different token formats
print("\n--- Testing Token Format 1: client_id:access_token ---")
token1 = f"{client_id}:{access_token}"
print(f"Token format: {token1[:50]}...")
fyers1 = fyersModel.FyersModel(client_id=client_id, token=token1, log_path="")
response1 = fyers1.quotes({"symbols": "NSE:NIFTY50-INDEX"})
print(f"Response: {response1}")

print("\n--- Testing Token Format 2: just access_token ---")
fyers2 = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
response2 = fyers2.quotes({"symbols": "NSE:NIFTY50-INDEX"})
print(f"Response: {response2}")
