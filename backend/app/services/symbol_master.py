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

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

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
    indices: list[str] = field(default_factory=list)  # List of indices this symbol belongs to

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
            from .symbol_master import SymbolMaster

            fyers_ticker = SymbolMaster.FYERS_INDEX_MAPPING.get(self.ticker, self.ticker)
            return f"{self.exchange}:{fyers_ticker}-INDEX"
        return f"{self.exchange}:{self.ticker}-{self.series}"

    @property
    def display_format(self) -> str:
        """Returns: SBIN"""
        return self.ticker


class SymbolMaster:
    """
    Symbol Master Service

    Unified symbol mapping and validation across different brokers and formats.
    Inspired by OpenAlgo's unified API layer.
    """

    # Regex patterns (ported from SymbolMapper)
    FYERS_PATTERN = re.compile(
        r"^([A-Z]+):([A-Z0-9&\.]+)(-(EQ|BL|GS|DR|V|DL|IL|INDEX))?$", re.IGNORECASE
    )
    DB_PATTERN = re.compile(r"^[A-Z0-9&\.\-]{1,20}$")
    ISIN_PATTERN = re.compile(r"^[A-Z]{2}[0-9]{10}[A-Z0-9]{2}$")

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

    # Display/input aliases normalized to DB format
    SYMBOL_ALIASES = {
        "NIFTY": "NIFTY50",
        "NIFTY 50": "NIFTY50",
        "NIFTY50": "NIFTY50",
        "NIFTY BANK": "BANKNIFTY",
        "BANK NIFTY": "BANKNIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "NIFTYBANK": "BANKNIFTY",
        "NIFTY IT": "NIFTYIT",
        "INDIA VIX": "INDIAVIX",
        "S&P BSE SENSEX": "SENSEX",
        "SENSEX": "SENSEX",
    }

    # Fyers index symbol mapping (DB format -> Fyers ticker)
    # Some indices have different names in Fyers
    FYERS_INDEX_MAPPING = {
        "NIFTY50": "NIFTY50",
        "SENSEX": "SENSEX",
        "BANKNIFTY": "NIFTYBANK",  # Fyers uses NIFTYBANK not BANKNIFTY
        "FINNIFTY": "FINNIFTY",
        "MIDCPNIFTY": "MIDCPNIFTY",
        "NIFTYNEXT50": "NIFTYNEXT50",
        "NIFTY100": "NIFTY100",
        "NIFTY200": "NIFTY200",
        "NIFTY500": "NIFTY500",
        "NIFTYIT": "NIFTYIT",
        "NIFTYAUTO": "NIFTYAUTO",
        "NIFTYREALTY": "NIFTYREALTY",
        "INDIAVIX": "INDIAVIX",
        # Sector indices (same in Fyers)
        "NIFTYBANK": "NIFTYBANK",
        "NIFTYPHARMA": "NIFTYPHARMA",
        "NIFTYMETAL": "NIFTYMETAL",
    }

    # Lot size and tick size overrides (OpenAlgo practice)
    # Most symbols default to 1 lot and 0.05 tick
    SYMBOL_METADATA = {
        "NIFTY50": {"lot_size": 50, "tick_size": 0.05},
        "BANKNIFTY": {"lot_size": 15, "tick_size": 0.05},
        "FINNIFTY": {"lot_size": 40, "tick_size": 0.05},
        "MIDCPNIFTY": {"lot_size": 75, "tick_size": 0.05},
    }

    def __init__(self):
        self._cache: dict[str, SymbolInfo] = {}
        self._load_symbol_master()

    def _load_symbol_master(self):
        """
        Load symbol master from database and index universe.
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

    def to_fyers_option(self, underlying: str, expiry: date, strike: float, opt_type: str) -> str:
        """
        Convert to Fyers option symbol format.

        Args:
            underlying: Symbol ticker (e.g., NIFTY, SBIN)
            expiry: Expiry date
            strike: Strike price
            opt_type: CE or PE

        Returns:
            Fyers symbol string
        """
        underlying = underlying.upper()
        opt_type = opt_type.upper()
        strike = int(strike)

        yy = str(expiry.year)[-2:]
        mmm = expiry.strftime("%b").upper()

        # Determine if Monthly or Weekly format should be used.
        # Fyers convention:
        # Monthly contracts (last Thurs of month) use MMM format.
        # Weekly contracts use M/O/N/D + DD format.

        # However, determining "last Thursday" accurately requires a holiday calendar.
        # For this implementation, we will try to infer or default to Weekly format
        # unless it is explicitly a Month-end expiry.
        # Ideally, we should have an explicit flag.
        # For now, we will assume:
        # If the day > 24, it MIGHT be monthly.
        # Let's rely on a helper or just support the Weekly format for everything?
        # No, Fyers enforces the format.

        # SIMPLE HEURISTIC for Phase 1:
        # We will use the WEEKLY format for everything except if we detect it's likely a monthly.
        # Actually, let's implement both and let the user/service decide?
        # No, SymbolMaster should be definitive.

        # Better approach: Check if date equals last Thursday?
        # Let's import a utility if available. If not, implementing a simple last-thursday check.

        def get_last_thursday(year, month):
            import calendar

            c = calendar.monthcalendar(year, month)
            # c is list of weeks, each week is list of 7 days (0 if not in month)
            # Thursday is index 3
            last_thursday = 0
            for week in c:
                if week[3] != 0:
                    last_thursday = week[3]
            return date(year, month, last_thursday)

        last_thurs = get_last_thursday(expiry.year, expiry.month)

        # If expiry matches last thursday, use Monthly format
        # Note: Handling holidays (if last Thurs is holiday, expiry is Wed) is tricky without calendar.
        # But Fyers usually sticks to the contract spec.
        # Let's try matching exact date.

        is_monthly = expiry == last_thurs

        # Handle exceptions/holidays crudely: if expiry is Wed and last Thurs is tomorrow?
        # Keeping it simple: If > 24th and close to month end, assume monthly?
        # Let's Stick to exact Last Thursday match for now.

        if is_monthly:
            # NSE:NIFTY25FEB22500CE
            return f"NSE:{underlying}{yy}{mmm}{strike}{opt_type}"
        else:
            # NSE:NIFTY2520622500CE
            # Month codes: 1-9, O, N, D
            month = expiry.month
            if month == 10:
                m_code = "O"
            elif month == 11:
                m_code = "N"
            elif month == 12:
                m_code = "D"
            else:
                m_code = str(month)

            dd = f"{expiry.day:02d}"
            return f"NSE:{underlying}{yy}{m_code}{dd}{strike}{opt_type}"

    def to_fyers_future(self, underlying: str, expiry: date) -> str:
        """
        Convert to Fyers future symbol format.
        Format: NSE:{TICKER}{YY}{MMM}FUT
        """
        underlying = underlying.upper()
        yy = str(expiry.year)[-2:]
        mmm = expiry.strftime("%b").upper()
        return f"NSE:{underlying}{yy}{mmm}FUT"

    def parse_option_symbol(self, fyers_symbol: str) -> dict:
        """
        Parse a Fyers option symbol into components.
        example: NSE:NIFTY2520622500CE
        """
        try:
            # Remove NSE: prefix
            clean_symbol = fyers_symbol.replace("NSE:", "").replace("BSE:", "")

            # Try Monthly Regex: TAATASTEEL 25 FEB 100 CE
            # REGEX: ^([A-Z0-9]+)(\d{2})([A-Z]{3})(\d+)(CE|PE)$
            m_match = re.match(r"^([A-Z0-9]+)(\d{2})([A-Z]{3})(\d+)(CE|PE)$", clean_symbol)
            if m_match:
                und, yy, mmm, strike, otype = m_match.groups()
                return {
                    "underlying": und,
                    "expiry_str": f"{yy}-{mmm}",
                    "strike": float(strike),
                    "option_type": otype,
                    "format": "MONTHLY",
                }

            # Try Weekly Regex: NIFTY 25 2 06 22500 CE
            # REGEX: ^([A-Z0-9]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$
            w_match = re.match(r"^([A-Z0-9]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$", clean_symbol)
            if w_match:
                und, yy, m, dd, strike, otype = w_match.groups()
                return {
                    "underlying": und,
                    "expiry_str": f"{yy}-{m}-{dd}",  # Raw codes
                    "strike": float(strike),
                    "option_type": otype,
                    "format": "WEEKLY",
                }

            return {}
        except Exception:
            return {}

    def to_fyers(self, symbol: str) -> str:
        """Alias for to_provider(symbol, 'fyers')"""
        if not self.is_valid(symbol, SymbolFormat.DB_FORMAT) and not self.is_valid(
            symbol, SymbolFormat.FYERS_FORMAT
        ):
            raise ValueError(f"Invalid symbol format: {symbol}")
        return self.to_provider(symbol, "fyers")

    def to_db(self, symbol: str) -> str:
        """
        Convert any format to DB format (canonical ticker).

        Examples:
            "NSE:SBIN-EQ" -> "SBIN"
            "NSE:NIFTY50" -> "NIFTY50"
            "SBIN" -> "SBIN"
        """
        if not symbol:
            return ""

        symbol = symbol.strip().upper()
        alias_match = self.SYMBOL_ALIASES.get(symbol)
        if alias_match:
            return alias_match

        # 1. Try Fyers format parsing (robust regex)
        # FYERS_PATTERN = re.compile(r'^([A-Z]+):([A-Z0-9&\.]+)(-(EQ|BL|GS|DR|V|DL|IL|INDEX))?$', re.IGNORECASE)
        match = self.FYERS_PATTERN.match(symbol)
        if match:
            # groups: (exchange, ticker, -series, series)
            ticker = match.group(2)
            return self.SYMBOL_ALIASES.get(ticker, ticker)

        # 2. Try simple Fyers format parsing (fallback)
        if ":" in symbol:
            parts = symbol.split(":")
            if len(parts) > 1:
                ticker_part = parts[1]
                # Remove common suffixes
                for suffix in [
                    "-EQ",
                    "-INDEX",
                    "-BE",
                    "-BZ",
                    "-BL",
                    "-GS",
                    "-DR",
                    "-V",
                    "-DL",
                    "-IL",
                ]:
                    if ticker_part.endswith(suffix):
                        ticker = ticker_part[: -len(suffix)]
                        return self.SYMBOL_ALIASES.get(ticker, ticker)
                return self.SYMBOL_ALIASES.get(ticker_part, ticker_part)

        # 3. ISIN - would need database lookup (not implemented here)

        # 4. Already in DB format?
        if self.DB_PATTERN.match(symbol):
            return self.SYMBOL_ALIASES.get(symbol, symbol)

        return symbol

    def to_display(self, symbol: str) -> str:
        """Convert any format to display format."""
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
        if ticker in self.INDEX_SYMBOLS:
            series = "INDEX"

        # Get index membership and company info from IndexUniverseLoader
        company_name = ""
        sector = ""
        indices = []
        lot_size = 1
        tick_size = 0.05

        # Check metadata overrides
        metadata = self.SYMBOL_METADATA.get(ticker, {})
        lot_size = metadata.get("lot_size", 1)
        tick_size = metadata.get("tick_size", 0.05)

        try:
            loader = _get_index_universe_loader()
            if loader:
                # Get indices this symbol belongs to
                indices = loader.get_symbol_indices(ticker)

                # Try to get company info
                for index_id in indices:
                    constituent = loader.get_constituent(ticker, index_id)
                    if constituent:
                        company_name = constituent.company_name
                        sector = constituent.industry
                        # Series usually EQ for constituents
                        break
        except Exception as e:
            logger.debug(f"Could not load index info for {ticker}: {e}")

        info = SymbolInfo(
            ticker=ticker,
            series=series,
            company_name=company_name,
            sector=sector,
            indices=indices,
            lot_size=lot_size,
            tick_size=tick_size,
        )
        self._cache[ticker] = info
        return info

    def is_valid(self, symbol: str, format: SymbolFormat = SymbolFormat.DB_FORMAT) -> bool:
        """Validate symbol format"""
        if not symbol:
            return False

        symbol = symbol.strip().upper()
        if format == SymbolFormat.DB_FORMAT:
            return bool(self.DB_PATTERN.match(symbol))
        elif format == SymbolFormat.FYERS_FORMAT:
            return bool(self.FYERS_PATTERN.match(symbol))
        elif format == SymbolFormat.ISIN_FORMAT:
            return bool(self.ISIN_PATTERN.match(symbol))
        return False

    def batch_to_fyers(self, symbols: list[str]) -> list[str]:
        """Convert multiple symbols to Fyers format"""
        return [self.to_fyers(s) for s in symbols]

    def to_fyers_list(self, symbols: list[str]) -> list[str]:
        """Alias for batch_to_fyers (compatibility)"""
        return self.batch_to_fyers(symbols)

    def batch_to_db(self, symbols: list[str]) -> list[str]:
        """Convert multiple symbols to DB format"""
        return [self.to_db(s) for s in symbols]

    def get_index_symbols(self, index_id: str) -> list[str]:
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
        Check if a symbol is a constituents of a specific index.

        Args:
            symbol: Symbol in any format
            index_id: Index identifier (e.g., "NIFTY50")

        Returns:
            True if symbol is in the index
        """
        try:
            # FIXED: Normalize symbol to DB format before checking
            normalized_symbol = self.to_db(symbol)
            loader = _get_index_universe_loader()
            return loader.is_symbol_in_index(normalized_symbol, index_id)
        except Exception as e:
            logger.debug(f"Failed to check index membership: {e}")
            return False

    def _is_fyers_format(self, symbol: str) -> bool:
        """Check if symbol is in Fyers format (NSE:SBIN-EQ)"""
        return bool(re.match(r"^[A-Z]+:[A-Z0-9_&-]+-[A-Z]+$", symbol))

    def _is_valid_ticker(self, ticker: str) -> bool:
        """Check if ticker is valid (alphanumeric, uppercase, include & and -)"""
        return bool(re.match(r"^[A-Z0-9_&-]+$", ticker)) and len(ticker) <= 20

    def refresh_cache(self) -> None:
        """Clear cache and reload symbol master."""
        self._cache.clear()
        self._load_symbol_master()


# Singleton instance
symbol_master = SymbolMaster()
