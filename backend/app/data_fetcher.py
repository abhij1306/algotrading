"""
Data fetching module for Fyers API
"""
import pandas as pd
import sys
import os
from typing import Optional, Dict
from .config import config

# Add AlgoTrading root directory to path to import Fyers module
# Get the path: backend/app/data_fetcher.py -> go up 3 levels to AlgoTrading/
algotrading_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# Remove if already exists and insert at position 0 to ensure it's searched first
if algotrading_root in sys.path:
    sys.path.remove(algotrading_root)
sys.path.insert(0, algotrading_root)
print(f"[DATA_FETCHER] AlgoTrading root at position 0: {algotrading_root}")

def fetch_fyers_historical(symbol: str, days: int = 365) -> Optional[pd.DataFrame]:
    """
    Fetch historical data from Fyers API
    
    Args:
        symbol: Stock symbol (without NSE: prefix)
        days: Number of days of history (default: 365)
        
    Returns:
        DataFrame with OHLCV data or None if error
    """
    if not config.HAS_FYERS:
        return None
    
    try:
        from fyers import fyers_client
        from datetime import datetime, timedelta
        
        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Format for Fyers
        fyers_symbol = f"NSE:{symbol}-EQ"
        range_from = start_date.strftime("%Y-%m-%d")
        range_to = end_date.strftime("%Y-%m-%d")
        
        # Get historical data
        response = fyers_client.get_historical_data(
            symbol=fyers_symbol,
            timeframe="1D",
            range_from=range_from,
            range_to=range_to
        )
        
        # CRITICAL: Check for token expiration errors
        if isinstance(response, dict):
            error_code = response.get('code')
            
            # Token expired or authentication failed
            if error_code in [401, 403, -17]:
                print(f"❌ Fyers token expired (code: {error_code})")
                print(f"   Please re-authenticate: cd fyers && python fyers_login.py")
                return None
            
            # Other API errors
            if response.get('s') != 'ok':
                error_msg = response.get('message', 'Unknown error')
                print(f"⚠️  Fyers API error for {symbol}: {error_msg}")
                return None
        
        if 'candles' not in response:
            return None
        
        # Convert to DataFrame
        candles = response['candles']
        df = pd.DataFrame(candles, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='s')
        df = df.set_index('date')
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        
        return df
        
    except Exception as e:
        print(f"❌ Exception fetching Fyers data for {symbol}: {str(e)}")
        return None

def fetch_historical_data(symbol: str, days: int = 365) -> Optional[pd.DataFrame]:
    """
    Fetch historical data - uses database for persistence
    
    Args:
        symbol: Stock symbol
        days: Number of days to fetch (default: 365)
        
    Returns:
        DataFrame with OHLCV data
    """
    from .database import SessionLocal
    from .data_repository import DataRepository
    from datetime import datetime, date, timedelta
    
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        # Check if we have data in database
        latest_date = repo.get_latest_price_date(symbol)
        today = date.today()
        
        # Determine if we need to fetch data
        need_full_fetch = False
        need_update = False
        
        if latest_date is None:
            # No data in database - fetch full history
            need_full_fetch = True
        elif latest_date < today:
            # Data exists but needs update
            need_update = True
        
        # Fetch from database if we have recent data
        if not need_full_fetch:
            df = repo.get_historical_prices(symbol, days=days)
            
            if not df.empty and need_update:
                # Fetch only missing days
                if config.HAS_FYERS:
                    try:
                        from fyers import fyers_client
                        
                        fyers_symbol = f"NSE:{symbol}-EQ"
                        start_date = latest_date + timedelta(days=1)
                        
                        response = fyers_client.get_historical_data(
                            symbol=fyers_symbol,
                            timeframe="1D",
                            range_from=start_date.strftime("%Y-%m-%d"),
                            range_to=today.strftime("%Y-%m-%d")
                        )
                        
                        if response.get('s') == 'ok' and 'candles' in response and response['candles']:
                            # Convert to DataFrame
                            candles = response['candles']
                            new_df = pd.DataFrame(candles, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                            new_df['date'] = pd.to_datetime(new_df['timestamp'], unit='s')
                            new_df = new_df.set_index('date')
                            new_df = new_df[['Open', 'High', 'Low', 'Close', 'Volume']]
                            
                            # Save to database
                            repo.save_historical_prices(symbol, new_df, source='fyers')
                            
                            # Append to existing data
                            df = pd.concat([df, new_df])
                            print(f"Updated {symbol} with {len(new_df)} new candles")
                    except Exception as e:
                        print(f"Failed to update {symbol}: {e}")
            
            # If we don't have enough data in DB (e.g. requested 400 but only have 100), we might need a full fetch
            # For now, simple check: if we got data, return it. proper handling would check start date.
            if not df.empty:
                return df
        
        # Full fetch needed
        
        # Try Fyers first
        if config.HAS_FYERS:
            hist = fetch_fyers_historical(symbol, days=days)
            if hist is not None and not hist.empty:
                # Save to database
                repo.save_historical_prices(symbol, hist, source='fyers')
                return hist
        
        # No fallback - return None if Fyers fails
        print(f"Failed to fetch {symbol} from Fyers")
        
        # FINAL FALLBACK: NSE Data Reader (Parquet/CSV)
        try:
            from .nse_data_reader import NSEDataReader
            reader = NSEDataReader()
            end_str = datetime.now().strftime("%Y-%m-%d")
            start_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            
            nse_df = reader.get_historical_data(symbol, start_str, end_str)
            if nse_df is not None and not nse_df.empty:
                print(f"[DATA] Recovered {symbol} from Local NSE Data")
                # Save to DB so we don't need to read file next time
                repo.save_historical_prices(symbol, nse_df, source='nse_local')
                return nse_df
        except Exception as e:
             print(f"Local NSE fallback failed for {symbol}: {e}")

        return None
        
    finally:
        db.close()

def fetch_fyers_quotes(symbols: list) -> Dict:
    """
    Fetch real-time quotes from Fyers API
    
    Args:
        symbols: List of stock symbols (without NSE: prefix)
        
    Returns:
        Dictionary of symbol -> quote data
    """
    if not config.HAS_FYERS:
        return {}
    
    try:
        # Import fyers client (sys.path already set at module level)
        from fyers import fyers_client
        
        # Format symbols for Fyers (NSE:SYMBOL-EQ)
        fyers_symbols = [f"NSE:{sym}-EQ" for sym in symbols]
        symbols_str = ",".join(fyers_symbols)
        
        # Get quotes
        response = fyers_client.get_quotes(symbols_str)
        
        if response.get('s') != 'ok' or 'd' not in response:
            print(f"Fyers API error: {response}")
            return {}
        
        # Parse response into dict
        quotes_dict = {}
        for quote in response['d']:
            # Extract symbol name (remove NSE: and -EQ)
            symbol = quote['n'].replace('NSE:', '').replace('-EQ', '')
            v = quote.get('v', {})
            quotes_dict[symbol] = {
                'ltp': v.get('lp', 0),  # Last price
                'volume': v.get('volume', 0),
                'high': v.get('high_price', 0),
                'low': v.get('low_price', 0),
                'open': v.get('open_price', 0),
                'prev_close': v.get('prev_close_price', 0),
            }
        
        return quotes_dict
        
    except Exception as e:
        print(f"Error fetching Fyers quotes: {str(e)}")
        import traceback
        traceback.print_exc()
        return {}

def fetch_fyers_preopen(symbol: str) -> Optional[dict]:
    """
    Fetch pre-open data from Fyers API
    
    Args:
        symbol: Stock symbol
        
    Returns:
        Dictionary with pre-open data or None
    """
    if not config.HAS_FYERS:
        return None
    
    # TODO: Implement Fyers pre-open API integration
    # Fyers doesn't have a direct pre-open API
    # We can use market depth or quotes during pre-open hours
    return None

def get_enhanced_quote(symbol: str, hist_data: pd.DataFrame) -> Dict:
    """
    Get enhanced quote combining historical + Fyers real-time
    
    Args:
        symbol: Stock symbol
        hist_data: Historical data from database
        
    Returns:
        Dictionary with latest price info
    """
    result = {
        'symbol': symbol,
        'source': 'database'
    }
    
    if hist_data is not None and not hist_data.empty:
        latest = hist_data.iloc[-1]
        result['close'] = float(latest['close'])
        result['volume'] = int(latest['volume'])
        result['high'] = float(latest['high'])
        result['low'] = float(latest['low'])
    
    # Try to get real-time quote from Fyers
    if config.HAS_FYERS:
        try:
            fyers_quotes = fetch_fyers_quotes([symbol])
            if symbol in fyers_quotes:
                fyers_data = fyers_quotes[symbol]
                result['close'] = fyers_data['ltp']  # Use real-time price
                result['volume'] = fyers_data['volume']
                result['high'] = fyers_data['high']
                result['low'] = fyers_data['low']
                result['source'] = 'fyers'
        except:
            pass
    
    return result
