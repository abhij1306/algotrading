"""
Data fetching module for Fyers API
"""

import time

import pandas as pd

from .config import config
from .services.fyers_client import get_fyers_client
from .services.symbol_master import symbol_master


def fetch_fyers_historical(symbol: str, days: int = 365) -> pd.DataFrame | None:
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
        fyers_client = get_fyers_client()
        from datetime import datetime, timedelta

        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Format for Fyers
        fyers_symbol = symbol_master.to_fyers(symbol)
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
                print(f"[ERROR] Fyers token expired (code: {error_code})")
                print("   Please re-authenticate: cd fyers && python fyers_login.py")
                return None

            # Other API errors
            if response.get('s') != 'ok':
                error_msg = response.get('message', 'Unknown error')
                print(f"[WARN] Fyers API error for {symbol}: {error_msg}")
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
        print(f"[ERROR] Exception fetching Fyers data for {symbol}: {str(e)}")
        return None

def fetch_historical_data(symbol: str, days: int = 365) -> pd.DataFrame | None:
    """
    Fetch historical data - uses database for persistence

    Args:
        symbol: Stock symbol
        days: Number of days to fetch (default: 365)

    Returns:
        DataFrame with OHLCV data
    """
    from datetime import date, datetime, timedelta

    from .data_repository import DataRepository
    from .database import SessionLocal

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
            # No data in database - fetch full history using Fyers
            # Use a larger window for the first backfill so technicals have enough lookback
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
                        fyers_client = get_fyers_client()

                        fyers_symbol = symbol_master.to_fyers(symbol)
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

        # Full fetch needed (initial backfill)
        # Try Fyers first using a generous lookback window so indicators like EMA/RSI have context
        if config.HAS_FYERS:
            full_days = max(days, 365)
            hist = fetch_fyers_historical(symbol, days=full_days)
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

# Simple cache for quotes to avoid 429 errors
_quotes_cache = {}
_CACHE_TTL = 2  # 2 seconds TTL for live quotes

def fetch_fyers_quotes(symbols: list) -> dict:
    """
    Fetch real-time quotes from Fyers API

    Args:
        symbols: List of stock symbols (without NSE: prefix)

    Returns:
        Dictionary of symbol -> quote data
    """
    if not config.HAS_FYERS:
        print("[fetch_fyers_quotes] Fyers not configured")
        return {}

    now = time.time()
    result = {}
    symbols_to_fetch = []

    # Check cache first
    for symbol in symbols:
        if symbol in _quotes_cache:
            cache_time, data = _quotes_cache[symbol]
            if now - cache_time < _CACHE_TTL:
                result[symbol] = data
                continue
        symbols_to_fetch.append(symbol)

    if not symbols_to_fetch:
        print(f"[fetch_fyers_quotes] All {len(symbols)} symbols from cache")
        return result

    try:
        fyers_client = get_fyers_client()

        if not fyers_client:
            print("[fetch_fyers_quotes] Fyers client not available")
            return result

        # Format symbols for Fyers using Symbol Master
        fyers_symbols = symbol_master.batch_to_fyers(symbols_to_fetch)
        print(f"[fetch_fyers_quotes] Fetching {len(fyers_symbols)} symbols: {fyers_symbols[:3]}...")

        # Get quotes
        response = fyers_client.get_quotes(fyers_symbols)

        if response.get('s') != 'ok':
            print(f"[fetch_fyers_quotes] Fyers API error: {response}")
            return result

        if 'd' not in response:
            print(f"[fetch_fyers_quotes] No data in response: {response}")
            return result

        # Parse response into dict
        quote_count = 0
        for quote in response['d']:
            # Extract symbol name using Symbol Master
            symbol = symbol_master.to_db(quote['n'])
            v = quote.get('v', {})
            quote_data = {
                'ltp': v.get('lp', 0),  # Last price
                'volume': v.get('volume', 0),
                'high': v.get('high_price', 0),
                'low': v.get('low_price', 0),
                'open': v.get('open_price', 0),
                'prev_close': v.get('prev_close_price', 0),
            }
            result[symbol] = quote_data
            quote_count += 1

            # Update cache
            _quotes_cache[symbol] = (now, quote_data)

        print(f"[fetch_fyers_quotes] Successfully fetched {quote_count} quotes")
        return result

    except Exception as e:
        print(f"[fetch_fyers_quotes] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return {}

def fetch_fyers_preopen(symbol: str) -> dict | None:
    """
    Fetch pre-open data from Fyers API

    Args:
        symbol: Stock symbol

    Returns:
        Dictionary with pre-open data or None
    """
    if not config.HAS_FYERS:
        return None

    try:
        fyers_client = get_fyers_client()
        fyers_symbol = symbol_master.to_fyers(symbol)

        # We use standard quotes as they reflect the equilibrium price during pre-open
        quotes = fyers_client.get_parsed_quotes([fyers_symbol])

        # get_parsed_quotes normalizes keys to bare symbols (strips NSE:/BSE: and -EQ/-INDEX)
        # So we check for the bare symbol first, then fallback to fyers_symbol for safety
        if symbol in quotes:
            q = quotes[symbol]
            return {
                'symbol': symbol,
                'price': q.get('ltp', 0),
                'volume': q.get('volume', 0),
                'timestamp': pd.Timestamp.now(),
                'source': 'fyers_preopen'
            }
        elif fyers_symbol in quotes:
            # Fallback: check for full fyers_symbol key if normalization didn't happen
            q = quotes[fyers_symbol]
            return {
                'symbol': symbol,
                'price': q.get('ltp', 0),
                'volume': q.get('volume', 0),
                'timestamp': pd.Timestamp.now(),
                'source': 'fyers_preopen'
            }
        else:
            # Verbose log if neither key format is present
            print(f"Warning: No quote data found for {symbol} (fyers_symbol={fyers_symbol}). Available keys: {list(quotes.keys())}")

    except Exception as e:
        print(f"Error fetching pre-open data for {symbol}: {e}")

    return None

def get_enhanced_quote(symbol: str, hist_data: pd.DataFrame) -> dict:
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
        except Exception:
            pass

    return result
