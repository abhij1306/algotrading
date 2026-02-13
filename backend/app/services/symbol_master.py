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
        """
        Return the canonical database representation of the symbol.
        
        Returns:
            The ticker string used as the DB format (e.g., "SBIN").
        """
        return self.ticker

    @property
    def fyers_format(self) -> str:
        """
        Get the symbol in FYERS format (EXCHANGE:TICKER-SERIES).
        
        Returns:
            fyers (str): Symbol formatted as "EXCHANGE:TICKER-SERIES", for example "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX".
        """
        return f"{self.exchange}:{self.ticker}-{self.series}"

    @property
    def display_format(self) -> str:
        """
        Display representation of the symbol.
        
        Returns:
            str: Ticker string used for display (e.g., "SBIN").
        """
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
        """
        Initialize the SymbolMaster internal state.
        
        Creates an empty cache for SymbolInfo objects, sets a small predefined list of common index tickers, and invokes the loader to populate any initial symbol data.
        """
        self._cache: Dict[str, SymbolInfo] = {}
        # Simple predefined list for common indices
        self._indices = ["NIFTY50", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNEXT50"]
        self._load_symbol_master()

    def _load_symbol_master(self):
        """
        Initialize symbol master data at application startup.
        
        This method is a startup hook intended to load symbol master information (e.g., from a database or index universe). In the current implementation it performs no action because symbol information is populated lazily by get_info.
        """
        # In a real implementation, this would query the DB.
        # For now, we use a lazy loading approach in get_info.
        pass

    def to_fyers(self, symbol: str) -> str:
        """
        Convert a symbol to FYERS format (e.g., "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX").
        
        Parameters:
            symbol (str): Input symbol in DB format (e.g., "SBIN") or already in FYERS format (e.g., "NSE:SBIN-EQ").
        
        Returns:
            str: Symbol in FYERS format.
        
        Raises:
            ValueError: If the input ticker is not a valid ticker.
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
        Convert a symbol string into the DB format (ticker only).
        
        Accepts inputs such as "SBIN", "NSE:SBIN-EQ", or "NSE:NIFTY50-INDEX" and returns the base ticker in uppercase. Handles tickers containing characters like "&" or "-" (e.g., "M&M", "BAJAJ-AUTO").
        
        Parameters:
            symbol (str): Input symbol in any supported form.
        
        Returns:
            str: The ticker portion in DB format (e.g., "SBIN" or "NIFTY50").
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
        Convert a symbol to display format (ticker-only).
        
        Accepts symbols in DB format (e.g., "SBIN") or FYERS format (e.g., "NSE:SBIN-EQ") and returns the ticker in uppercase.
        
        Returns:
            display (str): Ticker in display format (ticker-only, uppercase).
        """
        return self.to_db(symbol)

    def get_info(self, symbol: str) -> SymbolInfo:
        """
        Retrieve the canonical SymbolInfo for a given symbol.
        
        Accepts a symbol in any supported format, normalizes it to the DB ticker, and returns a cached or newly created SymbolInfo. The returned info uses series "INDEX" for known indices or tickers containing "NIFTY", otherwise "EQ".
        
        Parameters:
            symbol (str): Symbol in any supported format (e.g., "SBIN", "NSE:SBIN-EQ").
        
        Returns:
            SymbolInfo: Symbol information containing ticker, exchange, series, company_name, and sector.
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
        """
        Check whether a symbol conforms to the specified symbol format.
        
        Parameters:
            symbol (str): The symbol string to validate (e.g., "SBIN" or "NSE:SBIN-EQ").
            format (SymbolFormat): The expected symbol format to validate against.
        
        Returns:
            `true` if the symbol matches the provided format, `false` otherwise. Returns `false` if an internal error occurs during validation.
        """
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
        """
        Convert a list of symbols to FYERS format.
        
        Parameters:
            symbols (List[str]): Input symbols in any supported format (e.g., "SBIN", "NSE:SBIN-EQ").
        
        Returns:
            List[str]: Symbols converted to FYERS format, in the same order as the input.
        """
        return [self.to_fyers(s) for s in symbols]

    def to_fyers_list(self, symbols: List[str]) -> List[str]:
        """
        Convert a list of symbols to FYERS format.
        
        Parameters:
            symbols (List[str]): Symbols to convert; each may be in any supported input format.
        
        Returns:
            List[str]: Symbols converted to FYERS format (e.g., "NSE:SBIN-EQ").
        """
        return self.batch_to_fyers(symbols)

    def batch_to_db(self, symbols: List[str]) -> List[str]:
        """
        Convert a sequence of symbols into DB format (ticker-only).
        
        Parameters:
            symbols (List[str]): Symbols in any supported format (for example "SBIN" or "NSE:SBIN-EQ").
        
        Returns:
            List[str]: List of symbols normalized to DB format (ticker only, uppercase).
        """
        return [self.to_db(s) for s in symbols]

    def _is_fyers_format(self, symbol: str) -> bool:
        """
        Determine whether a symbol matches the FYERS pattern EXCHANGE:TICKER-SERIES (e.g., NSE:SBIN-EQ).
        
        Returns:
            True if the symbol matches the FYERS pattern, False otherwise.
        """
        return bool(re.match(r'^[A-Z]+:[A-Z0-9_&-]+-[A-Z]+$', symbol))

    def _is_valid_ticker(self, ticker: str) -> bool:
        """
        Validate a ticker string for allowed characters and maximum length.
        
        Parameters:
            ticker (str): Ticker to validate; expected uppercase and may include letters, digits, underscore (`_`), ampersand (`&`), and hyphen (`-`).
        
        Returns:
            bool: `true` if `ticker` contains only the allowed characters and is at most 20 characters long, `false` otherwise.
        """
        return bool(re.match(r'^[A-Z0-9_&-]+$', ticker)) and len(ticker) <= 20

# Singleton instance
symbol_master = SymbolMaster()