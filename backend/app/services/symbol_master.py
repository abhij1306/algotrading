"""
Symbol Master Service
=====================
Single source of truth for symbol format conversions and validations.

CANONICAL FORMATS:
- DB_FORMAT: Just ticker (e.g., "SBIN", "NIFTY50")
- FYERS_FORMAT: Exchange:Ticker-Series (e.g., "NSE:SBIN-EQ", "NSE:NIFTY50-INDEX")
- DISPLAY_FORMAT: Just ticker (e.g., "SBIN") - same as DB_FORMAT

WHY THIS EXISTS:
Prevents symbol format hell by centralizing all conversions.
No more ad-hoc conversions scattered across the codebase.
"""

from typing import Dict, Optional, List
from dataclasses import dataclass
from enum import Enum
import re
import logging

logger = logging.getLogger(__name__)

class SymbolFormat(Enum):
    """Supported symbol formats"""
    DB_FORMAT = "DB"           # SBIN
    FYERS_FORMAT = "FYERS"     # NSE:SBIN-EQ
    DISPLAY_FORMAT = "DISPLAY" # SBIN (same as DB)

@dataclass
class SymbolInfo:
    """Complete symbol information"""
    ticker: str              # Base ticker (e.g., "SBIN")
    exchange: str = "NSE"    # Exchange (default NSE)
    series: str = "EQ"       # Series (default EQ - equity, INDEX - index)
    company_name: str = ""   # Full company name
    sector: str = ""         # Sector classification

    @property
    def db_format(self) -> str:
        """Returns: SBIN"""
        return self.ticker

    @property
    def fyers_format(self) -> str:
        """Returns: NSE:SBIN-EQ or NSE:NIFTY50-INDEX"""
        return f"{self.exchange}:{self.ticker}-{self.series}"

    @property
    def display_format(self) -> str:
        """Returns: SBIN"""
        return self.ticker

class SymbolMaster:
    """
    Symbol Master Service

    Usage:
        from app.services.symbol_master import symbol_master

        # Convert formats
        fyers_symbol = symbol_master.to_fyers("SBIN")
        db_symbol = symbol_master.to_db("NSE:SBIN-EQ")

        # Validate
        is_valid = symbol_master.is_valid("SBIN", SymbolFormat.DB_FORMAT)

        # Get info
        info = symbol_master.get_info("SBIN")
        print(info.fyers_format)  # NSE:SBIN-EQ
    """

    def __init__(self):
        self._cache: Dict[str, SymbolInfo] = {}
        # Simple predefined list for common indices
        self._indices = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNEXT50"]
        self._load_symbol_master()

    def _load_symbol_master(self):
        """
        Load symbol master from database and index universe.
        This runs once at startup.
        """
        # In a real implementation, this would query the DB.
        # For now, we use a lazy loading approach in get_info.
        pass

    def to_fyers(self, symbol: str) -> str:
        """
        Convert any format to Fyers format.

        Args:
            symbol: Can be "SBIN" or "NSE:SBIN-EQ" (idempotent)

        Returns:
            "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX"

        Raises:
            ValueError: If symbol is invalid
        """
        # If already in Fyers format, return as-is
        if self._is_fyers_format(symbol):
            return symbol

        ticker = symbol.strip().upper()

        # Validate ticker format
        if not self._is_valid_ticker(ticker):
            raise ValueError(f"Invalid ticker format: {ticker}")

        # Get info and return Fyers format
        info = self.get_info(ticker)
        return info.fyers_format

    def to_db(self, symbol: str) -> str:
        """
        Convert any format to DB format.

        Args:
            symbol: Can be "SBIN" or "NSE:SBIN-EQ" (idempotent)

        Returns:
            "SBIN"
        """
        # If already in DB format (no colon, and no dash except for tickers like BAJAJ-AUTO), return as-is
        if ':' not in symbol and not symbol.endswith(('-EQ', '-INDEX')):
            return symbol.strip().upper()

        # Parse Fyers format: NSE:SBIN-EQ → SBIN or NSE:NIFTY50-INDEX -> NIFTY50
        # Support special characters in tickers like M&M or BAJAJ-AUTO
        match = re.match(r'([A-Z]+):([A-Z0-9_&-]+)-(EQ|INDEX|BE|BZ|SM|ST)', symbol)
        if match:
            exchange, ticker, series = match.groups()
            return ticker

        # Fallback for some weird formats if any
        ticker = symbol.replace('NSE:', '').replace('-EQ', '').replace('-INDEX', '')
        return ticker.strip().upper()

    def to_display(self, symbol: str) -> str:
        """
        Convert any format to display format.
        Currently same as DB format.
        """
        return self.to_db(symbol)

    def get_info(self, symbol: str) -> SymbolInfo:
        """
        Get complete symbol information.

        Args:
            symbol: Any format

        Returns:
            SymbolInfo with all details
        """
        ticker = self.to_db(symbol)

        # Check cache
        if ticker in self._cache:
            return self._cache[ticker]

        # Determine series
        series = "EQ"
        if ticker in self._indices or "NIFTY" in ticker:
            series = "INDEX"

        info = SymbolInfo(ticker=ticker, series=series)
        self._cache[ticker] = info
        return info

    def is_valid(self, symbol: str, format: SymbolFormat) -> bool:
        """Validate symbol format"""
        try:
            if format == SymbolFormat.DB_FORMAT:
                return self._is_valid_ticker(symbol)
            elif format == SymbolFormat.FYERS_FORMAT:
                return self._is_fyers_format(symbol)
            else:
                return self._is_valid_ticker(symbol)
        except:
            return False

    def batch_to_fyers(self, symbols: List[str]) -> List[str]:
        """Convert multiple symbols to Fyers format"""
        return [self.to_fyers(s) for s in symbols]

    def to_fyers_list(self, symbols: List[str]) -> List[str]:
        """Alias for batch_to_fyers (compatibility)"""
        return self.batch_to_fyers(symbols)

    def batch_to_db(self, symbols: List[str]) -> List[str]:
        """Convert multiple symbols to DB format"""
        return [self.to_db(s) for s in symbols]

    def _is_fyers_format(self, symbol: str) -> bool:
        """Check if symbol is in Fyers format (NSE:SBIN-EQ)"""
        return bool(re.match(r'^[A-Z]+:[A-Z0-9_&-]+-[A-Z]+$', symbol))

    def _is_valid_ticker(self, ticker: str) -> bool:
        """Check if ticker is valid (alphanumeric, uppercase, include & and -)"""
        return bool(re.match(r'^[A-Z0-9_&-]+$', ticker)) and len(ticker) <= 20

# Singleton instance
symbol_master = SymbolMaster()
