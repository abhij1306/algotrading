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

SCOPE:
This module only converts and validates symbol formats. It has no knowledge of
index membership: use ``index_universe_loader`` (which depends on this module,
never the other way round) for constituents, membership and company metadata.

HAT owns the conversion itself (``app.hat.data.symbols.SymbolMaster``: ``to_db``, ``to_fyers``
and the patterns, aliases and index map they use). This legacy class inherits it and adds only
what the legacy app uses: ``get_info`` and its cache, the ISIN
format. K5 deletes it with the legacy features.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum

from app.hat.data.symbols import INDEX_SYMBOLS as FYERS_INDEX_TICKERS
from app.hat.data.symbols import SymbolMaster as HatSymbolMaster

logger = logging.getLogger(__name__)


class SymbolFormat(Enum):
    """Supported symbol formats"""

    DB_FORMAT = "DB"  # SBIN
    FYERS_FORMAT = "FYERS"  # NSE:SBIN-EQ
    DISPLAY_FORMAT = "DISPLAY"  # SBIN (same as DB)
    ISIN_FORMAT = "ISIN"  # INE062A01015


@dataclass
class SymbolInfo:
    """Complete symbol information"""

    ticker: str  # Base ticker (e.g., "SBIN")
    exchange: str = "NSE"  # Exchange (default NSE)
    series: str = "EQ"  # Series (default EQ - equity, INDEX - index)
    company_name: str = ""  # Full company name
    sector: str = ""  # Sector classification
    isin: str = ""  # ISIN identifier
    lot_size: int = 1  # Standard lot size for orders
    tick_size: float = 0.05  # Minimum price movement

    @property
    def db_format(self) -> str:
        """Returns: SBIN"""
        return self.ticker

    @property
    def fyers_format(self) -> str:
        """Returns: NSE:SBIN-EQ or NSE:NIFTY50-INDEX"""
        # Special handling for indices in Fyers format
        if self.series == "INDEX":
            # Check if there's a special Fyers mapping for this index
            fyers_ticker = FYERS_INDEX_TICKERS.get(self.ticker, self.ticker)
            return f"{self.exchange}:{fyers_ticker}-INDEX"
        return f"{self.exchange}:{self.ticker}-{self.series}"

    @property
    def display_format(self) -> str:
        """Returns: SBIN"""
        return self.ticker


class SymbolMaster(HatSymbolMaster):
    """
    Symbol Master Service

    Unified symbol mapping and validation across different brokers and formats.
    Inspired by OpenAlgo's unified API layer.
    """

    ISIN_PATTERN = re.compile(r"^[A-Z]{2}\d{10}[A-Z0-9]{2}$", re.ASCII)

    # Exchange prefixes
    EXCHANGES = ["NSE", "BSE", "MCX", "NFO", "BFO", "CDS"]

    # Known index symbols (tradeable indices)
    INDEX_SYMBOLS = {
        "NIFTY50": "NIFTY 50 Index",
        "SENSEX": "S&P BSE SENSEX",
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
        "INDIAVIX": "India VIX",
        # Sector indices (Fyers format uses these directly)
        "NIFTYBANK": "NIFTY Bank Sector Index",
        "NIFTYPHARMA": "NIFTY Pharma Sector Index",
        "NIFTYMETAL": "NIFTY Metal Sector Index",
    }

    # Lot size and tick size overrides (OpenAlgo practice)
    # Most symbols default to 1 lot and 0.05 tick
    SYMBOL_METADATA = {
        "NIFTY50": {"lot_size": 50, "tick_size": 0.05},
        "BANKNIFTY": {"lot_size": 15, "tick_size": 0.05},
        "FINNIFTY": {"lot_size": 40, "tick_size": 0.05},
        "MIDCPNIFTY": {"lot_size": 75, "tick_size": 0.05},
    }

    def __init__(self) -> None:
        self._cache: dict[str, SymbolInfo] = {}
        self._load_symbol_master()

    def _load_symbol_master(self) -> None:
        """
        Pre-populate the cache with the known index symbols.
        This runs once at startup.
        """
        # Pre-populate cache with known index symbols
        for symbol, description in self.INDEX_SYMBOLS.items():
            metadata = self.SYMBOL_METADATA.get(symbol, {})
            self._cache[symbol] = SymbolInfo(
                ticker=symbol,
                exchange="BSE" if symbol == "SENSEX" else "NSE",
                series="INDEX",
                company_name=description,
                sector="Index",
                lot_size=metadata.get("lot_size", 1),
                tick_size=metadata.get("tick_size", 0.05),
            )

    def to_provider(self, symbol: str, provider: str = "fyers", exchange: str = "NSE") -> str:
        """
        Convert symbol to a specific provider's format.

        Args:
            symbol: Symbol in any format
            provider: Provider name (e.g., "fyers", "zerodha")
            exchange: Target exchange (default: NSE)

        Returns:
            Formatted symbol for the provider
        """
        info = self.get_info(symbol)

        if provider.lower() == "fyers":
            return info.fyers_format

        # Add more providers here as needed (Zerodha, AngelOne, etc.)
        # Fallback to DB format if provider not recognized
        return info.db_format

    def to_display(self, symbol: str) -> str:
        """Convert any format to display format."""
        return self.to_db(symbol)

    def get_info(self, symbol: str) -> SymbolInfo:
        """
        Get format and trading metadata (exchange, series, lot and tick size).

        Index membership, company name and industry for equities live in
        ``index_universe_loader`` (``get_symbol_indices`` / ``get_constituent``).

        Args:
            symbol: Any format

        Returns:
            SymbolInfo for the symbol
        """
        ticker = self.to_db(symbol)

        # Check cache
        if ticker in self._cache:
            return self._cache[ticker]

        # Determine series
        series = "EQ"
        if ticker in self.INDEX_SYMBOLS:
            series = "INDEX"

        # Check metadata overrides
        metadata = self.SYMBOL_METADATA.get(ticker, {})
        lot_size = metadata.get("lot_size", 1)
        tick_size = metadata.get("tick_size", 0.05)

        info = SymbolInfo(
            ticker=ticker,
            series=series,
            lot_size=lot_size,
            tick_size=tick_size,
        )
        self._cache[ticker] = info
        return info

    def is_valid(self, symbol: str, fmt: SymbolFormat = SymbolFormat.DB_FORMAT) -> bool:
        """Validate symbol format"""
        if not symbol:
            return False

        symbol = symbol.strip().upper()
        if fmt == SymbolFormat.DB_FORMAT:
            return self.is_db_format(symbol)
        elif fmt == SymbolFormat.FYERS_FORMAT:
            return self.is_fyers_format(symbol)
        elif fmt == SymbolFormat.ISIN_FORMAT:
            return bool(self.ISIN_PATTERN.match(symbol))
        return False

    def batch_to_fyers(self, symbols: list[str]) -> list[str]:
        """Convert multiple symbols to Fyers format"""
        return [self.to_fyers(s) for s in symbols]

    def batch_to_db(self, symbols: list[str]) -> list[str]:
        """Convert multiple symbols to DB format"""
        return [self.to_db(s) for s in symbols]

    def refresh_cache(self) -> None:
        """Clear cache and reload symbol master."""
        self._cache.clear()
        self._load_symbol_master()


# Singleton instance
symbol_master = SymbolMaster()
