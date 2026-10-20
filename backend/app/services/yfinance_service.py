"""
YFinance Service
Fallback service for fetching market data when Fyers is unavailable.
"""

import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def retry_on_failure(
    max_retries: int = 3, delay: float = 1
) -> Callable[[Callable[P, R]], Callable[P, R | None]]:
    """Decorator to retry on failure"""

    def decorator(func: Callable[P, R]) -> Callable[P, R | None]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.exception(f"Failed after {max_retries} attempts: {e}")
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
        yf_symbols = [YAHOO_INDEX_SYMBOLS.get(s, f"{s}.NS") for s in symbols]

        try:
            # Fetch data in bulk
            data = yf.download(
                yf_symbols, period="5d", interval="1d", progress=False, group_by="ticker"
            )

            for symbol, yf_sym in zip(symbols, yf_symbols, strict=True):
                try:
                    ticker_data = data[yf_sym] if len(yf_symbols) > 1 else data
                    quotes[symbol] = _quote_from_bars(ticker_data.dropna(how="all"))
                except Exception as e:
                    logger.warning(f"Failed to process {symbol} from yfinance: {e}")
                    quotes[symbol] = None

        except Exception as e:
            logger.exception(f"Failed to bulk fetch from yfinance: {e}")
            return {}

        return quotes

    @staticmethod
    def get_quote(symbol: str) -> dict | None:
        """Get quote for single symbol"""
        quotes = YFinanceService.get_quotes([symbol])
        return quotes.get(symbol)


# DB symbols of indices and their Yahoo tickers; equities use "<symbol>.NS"
YAHOO_INDEX_SYMBOLS = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}


def _quote_from_bars(ticker_data: pd.DataFrame) -> dict | None:
    """Latest price and change vs. the previous close (or today's open), or None if empty."""
    if ticker_data.empty:
        return None

    latest = ticker_data.iloc[-1]
    prev_close = ticker_data.iloc[-2]["Close"] if len(ticker_data) >= 2 else latest["Open"]

    current_price = latest["Close"]
    if pd.isna(current_price):
        current_price = latest["Open"]

    change = current_price - prev_close
    change_pct = (change / prev_close) * 100 if prev_close != 0 else 0

    return {
        "price": float(current_price),
        "change_pct": float(change_pct),
        "volume": int(latest.get("Volume", 0)),
        "open": float(latest.get("Open", 0)),
        "high": float(latest.get("High", 0)),
        "low": float(latest.get("Low", 0)),
        "source": "yfinance",
    }


# Singleton instance
yfinance_service = YFinanceService()
