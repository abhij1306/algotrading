"""
Fyers API direct integration - bypasses import issues
"""
import os
import json
from fyers_apiv3 import fyersModel

def load_fyers_credentials():
    """Load Fyers credentials from access_token.json"""
    token_file = os.path.join(os.path.dirname(__file__), '..', '..', 'fyers', 'config', 'access_token.json')
    
    if not os.path.exists(token_file):
        return None, None
    
    with open(token_file, 'r') as f:
        data = json.load(f)
        return data.get('client_id'), data.get('access_token')

def get_fyers_quotes(symbols: list):
    """
    Fetch live quotes from Fyers API
    
    Args:
        symbols: List of stock symbols (without NSE: prefix)
        
    Returns:
        Dictionary of symbol -> quote data
    """
    try:
        client_id, access_token = load_fyers_credentials()
        
        if not client_id or not access_token:
            return {}
        
        # Initialize Fyers client
        fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
        
        # Format symbols for Fyers
        fyers_symbols = [f"NSE:{sym}-EQ" for sym in symbols]
        symbols_str = ",".join(fyers_symbols)
        
        # Get quotes
        response = fyers.quotes({"symbols": symbols_str})
        
        if response.get('s') != 'ok' or 'd' not in response:
            return {}
        
        # Parse response
        quotes_dict = {}
        for quote in response['d']:
            symbol = quote['n'].replace('NSE:', '').replace('-EQ', '')
            v = quote.get('v', {})
            
            ltp = v.get('lp', 0)
            prev_close = v.get('prev_close_price', ltp)
            
            # Calculate percentage change
            if prev_close and prev_close > 0:
                change_pct = ((ltp - prev_close) / prev_close) * 100
            else:
                change_pct = 0
            
            quotes_dict[symbol] = {
                'ltp': ltp,
                'volume': v.get('volume', 0),
                'high': v.get('high_price', 0),
                'low': v.get('low_price', 0),
                'open': v.get('open_price', 0),
                'prev_close': prev_close,
                'change_pct': round(change_pct, 2),
            }
        
        return quotes_dict
        
    except Exception as e:
        print(f"Error fetching Fyers quotes: {str(e)}")
        return {}

def get_option_premium(symbol: str, strike: float, option_type: str, expiry_date=None):
    """
    Fetch live option premium from Fyers API
    
    Args:
        symbol: Underlying symbol (e.g., 'NIFTY', 'BANKNIFTY', 'RELIANCE')
        strike: Strike price (e.g., 24000)
        option_type: 'CE' or 'PE'
        expiry_date: datetime object for expiry (if None, uses next Thursday)
        
    Returns:
        Option premium (LTP) or None if not found
    """
    try:
        from datetime import datetime, timedelta
        
        client_id, access_token = load_fyers_credentials()
        
        if not client_id or not access_token:
            return None
        
        # Initialize Fyers client
        fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, log_path="")
        
        # Calculate expiry if not provided (next Thursday)
        if expiry_date is None:
            today = datetime.now()
            days_ahead = (3 - today.weekday()) % 7  # Thursday = 3
            if days_ahead == 0:
                days_ahead = 7
            expiry_date = today + timedelta(days=days_ahead)
        
        # Format expiry as YYMMMDD (e.g., 24DEC19)
        expiry_str = expiry_date.strftime('%y%b%d').upper()
        
        # Construct Fyers option symbol
        # Format: NSE:SYMBOL{YY}{MMM}{DD}{STRIKE}{CE/PE}
        # Example: NSE:NIFTY24DEC1924000CE
        option_symbol = f"NSE:{symbol}{expiry_str}{int(strike)}{option_type}"
        
        print(f"[FYERS] Fetching option: {option_symbol}")
        
        # Get quote for this specific option
        response = fyers.quotes({"symbols": option_symbol})
        
        if response.get('s') != 'ok' or 'd' not in response:
            print(f"[FYERS] Failed to fetch option: {response.get('message', 'Unknown error')}")
            return None
        
        # Parse response
        if len(response['d']) > 0:
            quote = response['d'][0]
            v = quote.get('v', {})
            ltp = v.get('lp', 0)
            
            if ltp > 0:
                print(f"[FYERS] Option premium: ₹{ltp:.2f}")
                return ltp
        
        return None
        
    except Exception as e:
        print(f"[FYERS] Error fetching option premium: {str(e)}")
        import traceback
        traceback.print_exc()
        return None
