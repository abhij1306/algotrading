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

INDEX UNIVERSE INTEGRATION:
Uses IndexUniverseLoader for accurate index constituent mappings.
"""

from typing import Dict, List
from dataclasses import dataclass, field
from enum import Enum
import re
import logging

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependencies
_index_universe_loader = None

def _get_index_universe_loader():
    """Lazy load the index universe loader to avoid circular imports."""
    global _index_universe_loader
    if _index_universe_loader is None:
        from .index_universe_loader import index_universe_loader
        _index_universe_loader = index_universe_loader
    return _index_universe_loader


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
    indices: List[str] = field(default_factory=list)  # List of indices this symbol belongs to

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
        print(info.indices)  # ["NIFTY50", "NIFTY100", "NIFTYBANK"]
    """

    # Known index symbols (tradeable indices)
    INDEX_SYMBOLS = {
        "NIFTY50": "NIFTY 50 Index",
        "BANKNIFTY": "NIFTY Bank Index",
        "FINNIFTY": "NIFTY Financial Services Index",
        "MIDCPNIFTY": "NIFTY Midcap Select Index",
        "NIFTYNEXT50": "NIFTY Next 50 Index",
        "NIFTY100": "NIFTY 100 Index",
        "NIFTY200": "NIFTY 200 Index",
        "NIFTY500": "NIFTY 500 Index",
        "NIFTYIT": "NIFTY IT Index",
        "NIFTYAUTO": "NIFTY Auto Index",
        "NIFTYREALTY": "NIFTY Realty Index",
    }

    def __init__(self):
        self._cache: Dict[str, SymbolInfo] = {}
        self._load_symbol_master()

    def _load_symbol_master(self):
        """
        Load symbol master from database and index universe.
        This runs once at startup.
        """
        # Pre-populate cache with known index symbols
        for symbol, description in self.INDEX_SYMBOLS.items():
            self._cache[symbol] = SymbolInfo(
                ticker=symbol,
                series="INDEX",
                company_name=description,
                sector="Index"
            )

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
            _exchange, ticker, _series = match.groups()
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
            SymbolInfo with all details including index membership
        """
        ticker = self.to_db(symbol)

        # Check cache
        if ticker in self._cache:
            return self._cache[ticker]

        # Determine series - check if it's a known index
        series = "EQ"
        if ticker in self.INDEX_SYMBOLS:
            series = "INDEX"
        
        # Get index membership and company info from IndexUniverseLoader
        company_name = ""
        sector = ""
        indices = []
        
        try:
            loader = _get_index_universe_loader()
            # Get indices this symbol belongs to
            indices = loader.get_symbol_indices(ticker)
            
            # Try to get company info from any index constituent data
            for index_id in indices:
                constituent = loader.get_constituent(ticker, index_id)
                if constituent:
                    company_name = constituent.company_name
                    sector = constituent.industry
                    break
        except Exception as e:
            logger.debug(f"Could not load index info for {ticker}: {e}")

        info = SymbolInfo(
            ticker=ticker,
            series=series,
            company_name=company_name,
            sector=sector,
            indices=indices
        )
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

    def get_index_symbols(self, index_id: str) -> List[str]:
        """
        Get all symbols in an index.
        
        Args:
            index_id: Index identifier (e.g., "NIFTY50", "NIFTYBANK")
            
        Returns:
            List of symbols in DB format
        """
        try:
            loader = _get_index_universe_loader()
            return loader.get_index_symbols(index_id)
        except Exception as e:
            logger.error(f"Failed to get index symbols for {index_id}: {e}")
            return []

    def is_index_constituent(self, symbol: str, index_id: str) -> bool:
        """
        Check if a symbol is a constituent of a specific index.
        
        Args:
            symbol: Symbol in any format
            index_id: Index identifier (e.g., "NIFTY50")
            
        Returns:
            True if symbol is in the index
        """
        try:
            loader = _get_index_universe_loader()
            return loader.is_symbol_in_index(symbol, index_id)
        except Exception as e:
            logger.debug(f"Failed to check index membership: {e}")
            return False

    def _is_fyers_format(self, symbol: str) -> bool:
        """Check if symbol is in Fyers format (NSE:SBIN-EQ)"""
        return bool(re.match(r'^[A-Z]+:[A-Z0-9_&-]+-[A-Z]+$', symbol))

    def _is_valid_ticker(self, ticker: str) -> bool:
        """Check if ticker is valid (alphanumeric, uppercase, include & and -)"""
        return bool(re.match(r'^[A-Z0-9_&-]+$', ticker)) and len(ticker) <= 20

    def refresh_cache(self) -> None:
        """Clear cache and reload symbol master."""
        self._cache.clear()
        self._load_symbol_master()


# Singleton instance
symbol_master = SymbolMaster()
