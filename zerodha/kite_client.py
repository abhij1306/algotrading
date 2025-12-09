"""
Zerodha Kite Connect Client for Historical Data
Provides access to historical options data including expired contracts
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("Please install kiteconnect: pip install kiteconnect")
    raise

# Configuration
API_KEY_FILE = "zerodha/api_credentials.json"
ACCESS_TOKEN_FILE = "zerodha/access_token.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_api_credentials() -> Dict[str, str]:
    """Load API key and secret from credentials file"""
    if not os.path.exists(API_KEY_FILE):
        raise Exception(
            f"API credentials file not found: {API_KEY_FILE}\n"
            "Please create it with your Kite Connect API key and secret:\n"
            '{\n'
            '  "api_key": "your_api_key",\n'
            '  "api_secret": "your_api_secret"\n'
            '}'
        )
    
    with open(API_KEY_FILE, 'r') as f:
        return json.load(f)


def save_access_token(access_token: str, api_key: str):
    """Save access token to file"""
    os.makedirs(os.path.dirname(ACCESS_TOKEN_FILE), exist_ok=True)
    
    data = {
        "access_token": access_token,
        "api_key": api_key,
        "timestamp": datetime.now().isoformat()
    }
    
    with open(ACCESS_TOKEN_FILE, 'w') as f:
        json.dump(data, f, indent=4)
    
    logger.info(f"Access token saved to {ACCESS_TOKEN_FILE}")


def load_access_token() -> Dict[str, str]:
    """Load access token from file"""
    if not os.path.exists(ACCESS_TOKEN_FILE):
        raise Exception(
            f"Access token file not found: {ACCESS_TOKEN_FILE}\n"
            "Please run zerodha_login.py first to generate access token"
        )
    
    with open(ACCESS_TOKEN_FILE, 'r') as f:
        return json.load(f)


def get_kite_client() -> KiteConnect:
    """Get authenticated Kite Connect client"""
    token_data = load_access_token()
    
    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    
    return kite


def get_historical_data(
    kite: KiteConnect,
    instrument_token: int,
    from_date: datetime,
    to_date: datetime,
    interval: str = "minute"
) -> List[Dict[str, Any]]:
    """
    Fetch historical data from Zerodha
    
    Args:
        kite: KiteConnect client
        instrument_token: Instrument token for the symbol
        from_date: Start date
        to_date: End date
        interval: Candle interval (minute, 3minute, 5minute, day, etc.)
    
    Returns:
        List of candle data
    """
    try:
        data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )
        
        logger.info(f"Fetched {len(data)} candles for token {instrument_token}")
        return data
        
    except Exception as e:
        logger.error(f"Failed to fetch historical data: {e}")
        raise


def search_instruments(kite: KiteConnect, query: str, exchange: str = "NFO") -> List[Dict]:
    """
    Search for instruments by name
    
    Args:
        kite: KiteConnect client
        query: Search query (e.g., "NIFTY")
        exchange: Exchange (NFO for options, NSE for equity)
    
    Returns:
        List of matching instruments
    """
    try:
        # Get all instruments
        instruments = kite.instruments(exchange)
        
        # Filter by query
        matches = [
            inst for inst in instruments
            if query.upper() in inst['tradingsymbol'].upper()
        ]
        
        return matches
        
    except Exception as e:
        logger.error(f"Failed to search instruments: {e}")
        raise


def get_nifty_option_symbol(
    expiry_date: datetime,
    strike: int,
    option_type: str
) -> str:
    """
    Generate Zerodha option symbol
    
    Format: NIFTY{YY}{MON}{DD}{STRIKE}{CE/PE}
    Example: NIFTY25NOV06 25700CE
    
    Args:
        expiry_date: Expiry date
        strike: Strike price
        option_type: CE or PE
    
    Returns:
        Zerodha option symbol
    """
    year = str(expiry_date.year)[-2:]
    month = expiry_date.strftime("%b").upper()
    day = f"{expiry_date.day:02d}"
    
    # Zerodha format: NIFTY25NOV0625700CE
    symbol = f"NIFTY{year}{month}{day}{strike}{option_type}"
    
    return symbol


if __name__ == "__main__":
    print("Zerodha Kite Connect Client")
    print("="*50)
    print("\nTo use this client:")
    print("1. Create zerodha/api_credentials.json with your API key and secret")
    print("2. Run zerodha_login.py to generate access token")
    print("3. Use get_kite_client() to get authenticated client")
