"""
Market Data Module - Services Package
Centralized market data services
"""

from .nse_data_reader import NSEDataReader
from .data_fetcher import DataFetcher
from .cache_manager import CacheManager

try:
    from .fyers_direct import get_fyers_quotes, FyersClient
except ImportError:
    # Fyers not available
    get_fyers_quotes = None
    FyersClient = None

__all__ = [
    'NSEDataReader',
    'DataFetcher', 
    'CacheManager',
    'get_fyers_quotes',
    'FyersClient'
]
