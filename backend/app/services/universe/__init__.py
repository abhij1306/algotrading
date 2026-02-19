"""
Universe Service
==============
Unified service for managing index universes and constituents.
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional


class UniverseMode(Enum):
    """Universe mode - historical or live"""
    HISTORICAL = "historical"
    LIVE = "live"


@dataclass
class UniverseConstituent:
    """Represents a single constituent in a universe"""
    symbol: str
    company_name: str
    industry: str
    weight: float | None = None
    index_code: str | None = None


class UniverseServiceImpl:
    """
    Implementation of Universe Service.
    Uses index_universe_loader for data.
    """

    def get_constituents(self, index_code: str, as_of_date: date | None = None) -> list[UniverseConstituent]:
        """Get all constituents for an index."""
        from ..index_universe_loader import index_universe_loader

        constituents = []
        loader = index_universe_loader

        # Get constituents from loader
        universe = loader.get_index_universe(index_code)
        if universe and universe.constituents:
            for c in universe.constituents:
                constituents.append(UniverseConstituent(
                    symbol=c.symbol,
                    company_name=c.company_name,
                    industry=c.industry,
                    index_code=index_code
                ))

        return constituents

    def get_symbols(self, index_code: str, as_of_date: date | None = None) -> list[str]:
        """Get all symbols for an index."""
        from ..index_universe_loader import index_universe_loader
        return index_universe_loader.get_index_symbols(index_code)

    def is_constituent(self, symbol: str, index_code: str, as_of_date: date | None = None) -> bool:
        """Check if a symbol is a constituent of an index."""
        from ..index_universe_loader import index_universe_loader
        return index_universe_loader.is_symbol_in_index(symbol, index_code)

    def list_available_indices(self) -> list[dict]:
        """List all available indices with their metadata."""
        from ..index_universe_loader import index_universe_loader

        indices = []
        available = index_universe_loader.get_available_indices()

        for index_id in available:
            description = index_universe_loader.get_index_description(index_id)
            universe = index_universe_loader.get_index_universe(index_id)
            count = len(universe.symbols) if universe else 0

            indices.append({
                'index_code': index_id,
                'name': description,
                'description': f'{description} - {count} stocks',
                'count': count
            })

        return indices


class UniverseService:
    """
    Service for managing index universes.

    Provides unified interface for:
    - Loading index constituents from database
    - Historical universe composition
    - Live mode data
    """

    def __init__(self):
        self._impl = UniverseServiceImpl()

    def get_constituents(self, index_code: str, as_of_date: date | None = None) -> list[UniverseConstituent]:
        """Get all constituents for an index."""
        return self._impl.get_constituents(index_code, as_of_date)

    def get_symbols(self, index_code: str, as_of_date: date | None = None) -> list[str]:
        """Get all symbols for an index."""
        return self._impl.get_symbols(index_code, as_of_date)

    def is_constituent(self, symbol: str, index_code: str, as_of_date: date | None = None) -> bool:
        """Check if a symbol is a constituent of an index."""
        return self._impl.is_constituent(symbol, index_code, as_of_date)

    def list_available_indices(self) -> list[dict]:
        """List all available indices with their metadata."""
        return self._impl.list_available_indices()


# Singleton instance
_universe_service = None


def get_universe_service() -> UniverseService:
    """Get the universe service singleton."""
    global _universe_service
    if _universe_service is None:
        _universe_service = UniverseService()
    return _universe_service


__all__ = [
    'UniverseService',
    'UniverseMode',
    'UniverseConstituent',
    'get_universe_service'
]
