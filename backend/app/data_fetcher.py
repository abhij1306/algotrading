"""
Data fetching module for live Fyers quotes only.
Historical price ingestion from Fyers is deprecated in canonical mode.
"""

import time

import pandas as pd

from .config import config
from .services.fyers_client import get_fyers_client
from .services.symbol_master import symbol_master

# Simple cache for quotes to avoid 429 errors
_quotes_cache = {}
_CACHE_TTL = 5  # 5 seconds TTL for live quotes (increased from 2 for better rate limiting)

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
    Get enhanced quote combining stored history + Fyers real-time quote.

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
