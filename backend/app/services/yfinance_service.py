"""
YFinance Service
Fallback service for fetching market data when Fyers is unavailable.
"""

import logging
import time
from functools import wraps

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def retry_on_failure(max_retries=3, delay=1):
    """Decorator to retry on failure"""

    def decorator(func):
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
    def get_quotes(symbols: list[str]) -> dict[str, dict]:
        """
        Get current quotes for symbols
        Args:
            symbols: List of DB_FORMAT symbols (e.g., ['SBIN', 'RELIANCE'])
        Returns:
            Dict mapping symbol to quote data
        """
        quotes = {}

        # If symbols list is too long, yfinance might be slow.
        # But for overview/top gainers (50-100 symbols), it should be okay.
        yf_symbols = [
            f"{s}.NS"
            if s not in ["NIFTY50", "BANKNIFTY"]
            else ("^NSEI" if s == "NIFTY50" else "^NSEBANK")
            for s in symbols
        ]

        try:
            # Fetch data in bulk
            data = yf.download(
                yf_symbols, period="5d", interval="1d", progress=False, group_by="ticker"
            )

            for i, symbol in enumerate(symbols):
                yf_sym = yf_symbols[i]
                try:
                    ticker_data = data[yf_sym] if len(yf_symbols) > 1 else data
                    ticker_data = ticker_data.dropna(how="all")

                    if ticker_data.empty:
                        quotes[symbol] = None
                        continue

                    latest = ticker_data.iloc[-1]
                    prev_close = (
                        ticker_data.iloc[-2]["Close"] if len(ticker_data) >= 2 else latest["Open"]
                    )

                    current_price = latest["Close"]
                    if pd.isna(current_price):
                        current_price = latest["Open"]

                    change = current_price - prev_close
                    change_pct = (change / prev_close) * 100 if prev_close != 0 else 0

                    quotes[symbol] = {
                        "price": float(current_price),
                        "change_pct": float(change_pct),
                        "volume": int(latest.get("Volume", 0)),
                        "open": float(latest.get("Open", 0)),
                        "high": float(latest.get("High", 0)),
                        "low": float(latest.get("Low", 0)),
                        "source": "yfinance",
                    }
                except Exception as e:
                    logger.warning(f"Failed to process {symbol} from yfinance: {e}")
                    quotes[symbol] = None

        except Exception as e:
            logger.error(f"Failed to bulk fetch from yfinance: {e}")
            return {}

        return quotes

    @staticmethod
    def get_quote(symbol: str) -> dict | None:
        """Get quote for single symbol"""
        quotes = YFinanceService.get_quotes([symbol])
        return quotes.get(symbol)


# Singleton instance
yfinance_service = YFinanceService()
