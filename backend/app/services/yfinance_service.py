"""
YFinance Service
Fallback service for fetching market data when Fyers is unavailable.
"""
import yfinance as yf
import pandas as pd
from typing import List, Dict, Optional
import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)

def retry_on_failure(max_retries=3, delay=1):
    """
    Create a decorator that retries a wrapped function on exception.
    
    Parameters:
        max_retries (int): Maximum number of attempts before giving up (must be >= 1).
        delay (float): Seconds to wait between retry attempts.
    
    Returns:
        function: A decorator which, when applied to a callable, returns a wrapped callable that returns the original callable's result on success or `None` if all retry attempts fail. On the final failure the error is logged.
    """
    def decorator(func):
        """
        Wraps a function so it is retried on exception up to the configured retry count.
        
        Parameters:
            func (Callable): The function to be wrapped.
        
        Returns:
            Callable: A wrapper that calls `func`, retrying up to `max_retries` times with `delay` seconds between attempts; on repeated failure it logs an error and returns `None`.
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed after {max_retries} attempts: {e}")
                        return None
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

class YFinanceService:
    """
    Yahoo Finance data service for post-market quotes
    """

    @staticmethod
    @retry_on_failure(max_retries=2, delay=0.5)
    def get_quotes(symbols: List[str]) -> Dict[str, dict]:
        """
        Fetches latest daily quote data for the given NSE symbols from Yahoo Finance.
        
        Parameters:
            symbols (List[str]): List of DB_FORMAT tickers (e.g., ['SBIN', 'RELIANCE']). Special inputs are mapped to Yahoo symbols: 'NIFTY50' -> '^NSEI', 'BANKNIFTY' -> '^NSEBANK', all other tickers are suffixed with '.NS'.
        
        Returns:
            Dict[str, dict]: Mapping from each input symbol to a quote dictionary with keys `price`, `change_pct`, `volume`, `open`, `high`, `low`, and `source`. If data is unavailable or processing for a symbol fails, that symbol maps to `None`. An empty dict is returned if the bulk fetch fails.
        """
        quotes = {}

        # If symbols list is too long, yfinance might be slow.
        # But for overview/top gainers (50-100 symbols), it should be okay.
        yf_symbols = [f"{s}.NS" if s not in ["NIFTY50", "BANKNIFTY"] else ("^NSEI" if s == "NIFTY50" else "^NSEBANK") for s in symbols]

        try:
            # Fetch data in bulk
            data = yf.download(yf_symbols, period="5d", interval="1d", progress=False, group_by='ticker')

            for i, symbol in enumerate(symbols):
                yf_sym = yf_symbols[i]
                try:
                    ticker_data = data[yf_sym] if len(yf_symbols) > 1 else data
                    ticker_data = ticker_data.dropna(how='all')

                    if ticker_data.empty:
                        quotes[symbol] = None
                        continue

                    latest = ticker_data.iloc[-1]
                    prev_close = ticker_data.iloc[-2]['Close'] if len(ticker_data) >= 2 else latest['Open']

                    current_price = latest['Close']
                    if pd.isna(current_price): current_price = latest['Open']

                    change = current_price - prev_close
                    change_pct = (change / prev_close) * 100 if prev_close != 0 else 0

                    quotes[symbol] = {
                        'price': float(current_price),
                        'change_pct': float(change_pct),
                        'volume': int(latest.get('Volume', 0)),
                        'open': float(latest.get('Open', 0)),
                        'high': float(latest.get('High', 0)),
                        'low': float(latest.get('Low', 0)),
                        'source': 'yfinance'
                    }
                except Exception as e:
                    logger.warning(f"Failed to process {symbol} from yfinance: {e}")
                    quotes[symbol] = None

        except Exception as e:
            logger.error(f"Failed to bulk fetch from yfinance: {e}")
            return {}

        return quotes

    @staticmethod
    def get_quote(symbol: str) -> Optional[dict]:
        """
        Retrieve the latest quote data for a single symbol.
        
        Parameters:
            symbol (str): Ticker to query. Accepts NSE identifiers (e.g., "RELIANCE"), special indices like "NIFTY50" or "BANKNIFTY", or other supported ticker formats.
        
        Returns:
            dict: Quote data containing keys such as `price`, `change_pct`, `volume`, `open`, `high`, `low`, and `source` for the requested symbol, or `None` if the quote is unavailable.
        """
        quotes = YFinanceService.get_quotes([symbol])
        return quotes.get(symbol)

# Singleton instance
yfinance_service = YFinanceService()