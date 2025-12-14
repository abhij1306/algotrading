#!/usr/bin/env python3
"""
Fyers Client for REST API operations
Handles authentication and REST API calls for the AlgoTrading project.
"""

import os
import json
import logging
from typing import Dict, Any, Optional
from fyers_apiv3 import fyersModel

# Configuration
ACCESS_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "access_token.json")


def load_access_token() -> str:
    """
    Load access token from the token file.
    Returns the access token string.
    """
    if not os.path.exists(ACCESS_TOKEN_FILE):
        raise Exception("Access token file not found. Run fyers_login.py first.")
    
    with open(ACCESS_TOKEN_FILE, "r") as f:
        data = json.load(f)
        return data.get('access_token', '')


def load_client_id() -> str:
    """
    Load client ID from the token file.
    Returns the client ID string.
    """
    if not os.path.exists(ACCESS_TOKEN_FILE):
        raise Exception("Access token file not found. Run fyers_login.py first.")
    
    with open(ACCESS_TOKEN_FILE, "r") as f:
        data = json.load(f)
        return data.get('client_id', '')


def load_fyers() -> fyersModel.FyersModel:
    """
    Load and return an authenticated FyersModel instance.
    """
    try:
        access_token = load_access_token()
        client_id = load_client_id()
        
        if not access_token or not client_id:
            raise Exception("Invalid token file format. Missing access_token or client_id.")
        
        # For REST API, use just the access_token (not client_id:access_token)
        # Note: WebSocket uses client_id:access_token format
        token = access_token
        
        # Initialize FyersModel
        fyers = fyersModel.FyersModel(client_id=client_id, token=token, log_path="")
        
        return fyers
    
    except Exception as e:
        logging.error(f"Failed to load Fyers client: {e}")
        raise


def get_account_info() -> Dict[str, Any]:
    """
    Get account information from Fyers API.
    """
    try:
        fyers = load_fyers()
        response = fyers.get_profile()
        return response
    except Exception as e:
        logging.error(f"Failed to get account info: {e}")
        return {}


def get_funds() -> Dict[str, Any]:
    """
    Get account funds information from Fyers API.
    """
    try:
        fyers = load_fyers()
        response = fyers.funds()
        return response
    except Exception as e:
        logging.error(f"Failed to get funds: {e}")
        return {}


def get_market_status() -> Dict[str, Any]:
    """
    Get market status from Fyers API.
    """
    try:
        fyers = load_fyers()
        response = fyers.market_status()
        return response
    except Exception as e:
        logging.error(f"Failed to get market status: {e}")
        return {}


def get_quotes(symbols: str) -> Dict[str, Any]:
    """
    Get quotes for given symbols.
    symbols: Comma-separated list of symbols (e.g., "NSE:NIFTY50-INDEX,NSE:SBIN-EQ")
    """
    try:
        fyers = load_fyers()
        response = fyers.quotes({"symbols": symbols})
        return response
    except Exception as e:
        logging.error(f"Failed to get quotes for {symbols}: {e}")
        return {}


def get_option_chain(symbol: str) -> Dict[str, Any]:
    """
    Get option chain data for a symbol.
    symbol: Option symbol (e.g., "NSE:NIFTY25CE19500")
    Returns latest price and other details.
    """
    try:
        fyers = load_fyers()
        response = fyers.quotes({"symbols": symbol})
        if response.get('s') == 'ok' and 'd' in response:
            return response['d'][0]
        return {}
    except Exception as e:
        logging.error(f"Failed to get option chain for {symbol}: {e}")
        return {}

def get_historical_data(symbol: str, timeframe: str = "1", range_from: str = None, range_to: str = None) -> Dict[str, Any]:
    """
    Get historical data for a symbol.
    symbol: Symbol to get data for (e.g., "NSE:NIFTY50-INDEX")
    timeframe: Timeframe (e.g., "1" for 1 minute, "5" for 5 minutes, "1D" for daily)
    range_from: Start date in YYYY-MM-DD format
    range_to: End date in YYYY-MM-DD format
    """
    try:
        fyers = load_fyers()
        
        # If range not provided, use last 30 days
        if not range_from or not range_to:
            from datetime import datetime, timedelta
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            range_from = start_date.strftime("%Y-%m-%d")
            range_to = end_date.strftime("%Y-%m-%d")
        
        data = {
            "symbol": symbol,
            "resolution": timeframe,
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1"
        }
        
        response = fyers.history(data)
        return response
    except Exception as e:
        logging.error(f"Failed to get historical data for {symbol}: {e}")
        return {}


def validate_token() -> bool:
    """
    Validate if the current token is still valid.
    Returns True if valid, False otherwise.
    """
    try:
        # Try to get account info - if it fails, token is invalid
        account_info = get_account_info()
        return account_info.get('s') == 'ok'
    except:
        return False


if __name__ == "__main__":
    # Test the client
    try:
        fyers = load_fyers()
        print("Fyers client loaded successfully")
        
        # Test basic API call
        quotes = get_quotes("NSE:NIFTY50-INDEX")
        print(f"Quotes response: {quotes}")
        
    except Exception as e:
        print(f"Error: {e}")
