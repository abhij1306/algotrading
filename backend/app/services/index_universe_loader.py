"""
Index Universe Loader Service
=============================
Loads index constituent data from CSV files for accurate symbol mappings.

This service reads NSE index constituent lists from CSV files and provides:
- Symbol lists for each index (NIFTY50, NIFTY100, NIFTY200, NIFTY500, etc.)
- Symbol-to-index mapping (which indices a symbol belongs to)
- Company metadata (name, industry, ISIN)

CSV File Format (from NSE):
- Company Name, Industry, Symbol, Series, ISIN Code

Supported Indices:
- NIFTY 50 (ind_nifty50list.csv)
- NIFTY 100 (ind_nifty100list.csv)
- NIFTY 200 (ind_nifty200list.csv)
- NIFTY 500 (ind_nifty500list.csv)
- NIFTY BANK (ind_niftybanklist.csv)
- NIFTY IT (ind_niftyitlist.csv)
- NIFTY AUTO (ind_niftyautolist.csv)
- NIFTY FINANCE (ind_niftyfinancelist.csv)
- NIFTY REALTY (ind_niftyrealtylist.csv)
- NIFTY MIDCAP 150 (ind_niftymidcap150list.csv)
- NIFTY NEXT 50 (ind_niftynext50list.csv)
- NIFTY SMALLCAP 500 (ind_NiftySmallcap500_list.csv)
- NIFTY TOTAL MARKET (ind_niftytotalmarket_list.csv)
- NIFTY PRIVATE BANK (ind_nifty_privatebanklist.csv)
"""

import csv
import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy import to avoid circular dependencies
_symbol_master = None


def _get_symbol_master():
    """Lazy load the symbol master to avoid circular imports."""
    global _symbol_master
    if _symbol_master is None:
        from .symbol_master import symbol_master

        _symbol_master = symbol_master
    return _symbol_master


# Use project-relative path - GitHub compatible
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
NSE_DATA_PATH = PROJECT_ROOT / "data_system" / "03_universe" / "constituents"

# Set data path - will log warning if not found
DATA_PATH = NSE_DATA_PATH

# Index file mapping: index_id -> filename
INDEX_FILES = {
    # Broad Market Indices
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY500": "ind_nifty500list.csv",
    "NIFTYNEXT50": "ind_niftynext50list.csv",
    "NIFTYTOTALMARKET": "ind_niftytotalmarket_list.csv",
    "NIFTYSMALLCAP500": "ind_NiftySmallcap500_list.csv",
    # Sector Indices
    "NIFTYBANK": "ind_niftybanklist.csv",
    "NIFTYIT": "ind_niftyitlist.csv",
    "NIFTYAUTO": "ind_niftyautolist.csv",
    "NIFTYFINANCE": "ind_niftyfinancelist.csv",
    "NIFTYREALTY": "ind_niftyrealtylist.csv",
    "NIFTYPHARMA": "ind_niftypharmalist.csv",
    "NIFTYMETAL": "ind_niftymetallist.csv",
    "NIFTYFMCG": "ind_niftyfmcglist.csv",
    "NIFTYOILGAS": "ind_niftyoilgaslist.csv",
    "NIFTYHEALTHCARE": "ind_niftyhealthcarelist.csv",
    "NIFTYMEDIA": "ind_niftymedialist.csv",
    "NIFTYCONSUMERDURABLES": "ind_niftyconsumerdurableslist.csv",
    "NIFTYCHEMICALS": "ind_niftyChemicals_list.csv",
    "NIFTYPSUBANK": "ind_niftypsubanklist.csv",
    # Mid & Small Cap Indices
    "NIFTYMIDCAP150": "ind_niftymidcap150list.csv",
    "NIFTYMIDCAP100": "ind_niftymidcap100list.csv",
    "NIFTYMIDCAP50": "ind_niftymidcap50list.csv",
    "NIFTYMIDCAPSELECT": "ind_niftymidcapselect_list.csv",
    "NIFTYSMALLCAP250": "ind_niftysmallcap250list.csv",
    # Multi-factor Indices
    "NIFTYLARGEMIDCAP250": "ind_niftylargemidcap250list.csv",
    "NIFTYMICROCAP250": "ind_niftymicrocap250_list.csv",
    "NIFTYMIDSMALLCAP400": "ind_niftymidsmallcap400list.csv",
    # Speciality Indices
    "NIFTYPRIVATEBANK": "ind_nifty_privatebanklist.csv",
    "NIFTYMIDSMALLFINANCIALSERVICES": "ind_niftymidsmallfinancailservice_list.csv",
    "NIFTYMIDSMALLHEALTHCARE": "ind_niftymidsmallhealthcare_list.csv",
    "NIFTYMIDSMALLITANDTELECOM": "ind_niftymidsmallitAndtelecom_list.csv",
}


@dataclass
class IndexConstituent:
    """Represents a single constituent in an index"""

    symbol: str
    company_name: str
    industry: str
    series: str
    isin: str
    index_id: str


@dataclass
class IndexUniverse:
    """Represents an index with all its constituents"""

    index_id: str
    description: str
    symbols: list[str] = field(default_factory=list)
    constituents: list[IndexConstituent] = field(default_factory=list)
    last_updated: date = field(default_factory=date.today)

    def __len__(self) -> int:
        return len(self.symbols)


class IndexUniverseLoader:
    """
    Loads and manages index constituent data from CSV files.

    Usage:
        from app.services.index_universe_loader import index_universe_loader

        # Get all symbols in NIFTY 50
        nifty50_symbols = index_universe_loader.get_index_symbols("NIFTY50")

        # Get all indices a symbol belongs to
        indices = index_universe_loader.get_symbol_indices("SBIN")

        # Check if symbol is in an index
        is_nifty50 = index_universe_loader.is_symbol_in_index("SBIN", "NIFTY50")

        # Get constituent details
        constituent = index_universe_loader.get_constituent("SBIN", "NIFTY50")
    """

    def __init__(self, data_path=None):
        self.data_path = Path(data_path) if data_path else DATA_PATH
        self._indices: dict[str, IndexUniverse] = {}
        self._symbol_to_indices: dict[str, set[str]] = {}
        self._loaded = False
        # ADDED: Lock for thread-safe lazy loading
        self._load_lock = threading.Lock()

        # Warn if data path doesn't exist
        if not self.data_path.exists():
            logger.warning(f"Index universe data path does not exist: {self.data_path}")
            logger.warning("Please copy NSE CSV files to this directory.")

    def load_all(self) -> None:
        """Load all index data from CSV files (thread-safe)"""
        # Double-check pattern: first check without lock, then acquire lock
        if self._loaded:
            return

        with self._load_lock:
            # Double-check after acquiring lock
            if self._loaded:
                return

            logger.info(f"Loading index universe data from {self.data_path}")

            for index_id, filename in INDEX_FILES.items():
                filepath = self.data_path / filename
                if filepath.exists():
                    try:
                        self._load_index_csv(index_id, filepath)
                    except Exception as e:
                        logger.error(f"Failed to load {filename}: {e}")
                else:
                    logger.warning(f"Index file not found: {filepath}")

            self._loaded = True
            logger.info(
                f"Loaded {len(self._indices)} indices with {len(self._symbol_to_indices)} unique symbols"
            )

    def _load_index_csv(self, index_id: str, filepath: Path) -> None:
        """Load a single index CSV file"""
        constituents = []
        symbols = []

        with open(filepath, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                symbol = row.get("Symbol", "").strip().upper()
                if not symbol:
                    continue

                constituent = IndexConstituent(
                    symbol=symbol,
                    company_name=row.get("Company Name", "").strip(),
                    industry=row.get("Industry", "").strip(),
                    series=row.get("Series", "EQ").strip(),
                    isin=row.get("ISIN Code", "").strip(),
                    index_id=index_id,
                )
                constituents.append(constituent)
                symbols.append(symbol)

                # Update symbol-to-indices mapping
                if symbol not in self._symbol_to_indices:
                    self._symbol_to_indices[symbol] = set()
                self._symbol_to_indices[symbol].add(index_id)

        # Create IndexUniverse
        description = f"NSE {index_id.replace('NIFTY', 'NIFTY ')} Index"
        self._indices[index_id] = IndexUniverse(
            index_id=index_id, description=description, symbols=symbols, constituents=constituents
        )

        logger.info(f"Loaded {index_id}: {len(symbols)} symbols")

    def get_index_symbols(self, index_id: str) -> list[str]:
        """
        Get all symbols in an index.

        Args:
            index_id: Index identifier (e.g., "NIFTY50", "NIFTYBANK")

        Returns:
            List of symbols in DB format (e.g., ["SBIN", "HDFCBANK"])
        """
        if not self._loaded:
            self.load_all()

        universe = self._indices.get(index_id)
        return universe.symbols if universe else []

    def get_index_universe(self, index_id: str) -> IndexUniverse | None:
        """Get full IndexUniverse object for an index"""
        if not self._loaded:
            self.load_all()
        return self._indices.get(index_id)

    def get_symbol_indices(self, symbol: str) -> list[str]:
        """
        Get all indices a symbol belongs to.

        Args:
            symbol: Symbol in any format (e.g., "SBIN" or "NSE:SBIN-EQ")

        Returns:
            List of index IDs (e.g., ["NIFTY50", "NIFTY100", "NIFTYBANK"])
        """
        if not self._loaded:
            self.load_all()

        # Normalize symbol to DB format using symbol_master
        clean_symbol = _get_symbol_master().to_db(symbol)

        return list(self._symbol_to_indices.get(clean_symbol, set()))

    def is_symbol_in_index(self, symbol: str, index_id: str) -> bool:
        """Check if a symbol is in a specific index"""
        if not self._loaded:
            self.load_all()

        # Normalize symbol to DB format using symbol_master
        clean_symbol = _get_symbol_master().to_db(symbol)

        return index_id in self._symbol_to_indices.get(clean_symbol, set())

    def get_constituent(self, symbol: str, index_id: str) -> IndexConstituent | None:
        """Get constituent details for a symbol in a specific index"""
        if not self._loaded:
            self.load_all()

        universe = self._indices.get(index_id)
        if not universe:
            return None

        # Normalize symbol to DB format using symbol_master
        clean_symbol = _get_symbol_master().to_db(symbol)

        for constituent in universe.constituents:
            if constituent.symbol == clean_symbol:
                return constituent
        return None

    def get_all_symbols(self) -> set[str]:
        """Get all unique symbols across all indices"""
        if not self._loaded:
            self.load_all()
        return set(self._symbol_to_indices.keys())

    def get_available_indices(self) -> list[str]:
        """Get list of all loaded index IDs"""
        if not self._loaded:
            self.load_all()
        return list(self._indices.keys())

    def get_index_description(self, index_id: str) -> str:
        """Get human-readable description for an index"""
        if not self._loaded:
            self.load_all()
        universe = self._indices.get(index_id)
        return universe.description if universe else ""

    def get_symbols_by_date(self, index_id: str, _target_date: date) -> list[str]:
        """
        Get symbols for an index as of a specific date.

        Note: Currently returns current constituents. Historical changes
        would require parsing IndexInclExcl.xls for rebalance history.

        Args:
            index_id: Index identifier
            target_date: Target date (currently ignored, returns current constituents)

        Returns:
            List of symbols
        """
        # TODO(#9): Implement historical constituent lookup using IndexInclExcl.xls
        return self.get_index_symbols(index_id)

    def refresh(self) -> None:
        """Force reload of all index data"""
        self._loaded = False
        self._indices.clear()
        self._symbol_to_indices.clear()
        self.load_all()


# Singleton instance
index_universe_loader = IndexUniverseLoader()
