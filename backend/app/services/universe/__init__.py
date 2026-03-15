"""
Universe Service
==============
Unified service for managing index universes and constituents.
"""

import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import IndexConstituentHistory, IndexUniverseDefinition, UniverseSnapshot


class UniverseMode(Enum):
    """Universe mode - historical or live"""

    HISTORICAL = "historical"
    LIVE = "live"


@dataclass
class UniverseConstituent:
    """Represents a single constituent in a universe"""

    symbol: str
    company_name: str
    industry: str = ""
    weight: float | None = None
    index_code: str | None = None
    isin: str | None = None
    sector: str | None = None


@dataclass
class UniverseResult:
    """Result container for universe lookup"""

    index_code: str
    lookup_date: date
    constituents: list[UniverseConstituent]
    source: str
    is_historical: bool


class UniverseServiceImpl:
    """
    Implementation of Universe Service.
    Uses index_universe_loader for data.
    """

    @staticmethod
    def _load_db_definition(index_code: str, db: Session) -> IndexUniverseDefinition | None:
        return (
            db.query(IndexUniverseDefinition)
            .filter(IndexUniverseDefinition.index_code == index_code)
            .first()
        )

    def _load_db_constituents(self, index_code: str, lookup_date: date) -> list[UniverseConstituent]:
        db: Session = SessionLocal()
        try:
            definition = self._load_db_definition(index_code, db)
            if definition is None:
                return []

            rows = (
                db.query(IndexConstituentHistory)
                .filter(
                    IndexConstituentHistory.universe_id == definition.id,
                    IndexConstituentHistory.effective_from <= lookup_date,
                    (
                        IndexConstituentHistory.effective_to.is_(None)
                        | (IndexConstituentHistory.effective_to >= lookup_date)
                    ),
                )
                .order_by(IndexConstituentHistory.symbol.asc())
                .all()
            )

            return [
                UniverseConstituent(
                    symbol=row.symbol,
                    company_name=row.company_name or row.symbol,
                    industry=row.industry or "",
                    weight=row.weight,
                    index_code=index_code,
                    isin=row.isin,
                )
                for row in rows
            ]
        finally:
            db.close()

    def _load_db_symbols(self, index_code: str, lookup_date: date) -> list[str]:
        db: Session = SessionLocal()
        try:
            definition = self._load_db_definition(index_code, db)
            if definition is None:
                return []

            snapshot = (
                db.query(UniverseSnapshot)
                .filter(
                    UniverseSnapshot.universe_id == definition.id,
                    UniverseSnapshot.snapshot_date == lookup_date,
                )
                .first()
            )
            if snapshot and snapshot.symbols:
                try:
                    return sorted(set(json.loads(snapshot.symbols)))
                except Exception:
                    pass

            rows = (
                db.query(IndexConstituentHistory.symbol)
                .filter(
                    IndexConstituentHistory.universe_id == definition.id,
                    IndexConstituentHistory.effective_from <= lookup_date,
                    (
                        IndexConstituentHistory.effective_to.is_(None)
                        | (IndexConstituentHistory.effective_to >= lookup_date)
                    ),
                )
                .order_by(IndexConstituentHistory.symbol.asc())
                .all()
            )
            return [row[0] for row in rows]
        finally:
            db.close()

    def _list_db_indices(self) -> list[dict]:
        db: Session = SessionLocal()
        try:
            definitions = db.query(IndexUniverseDefinition).order_by(IndexUniverseDefinition.index_code).all()
            indices: list[dict[str, Any]] = []
            for definition in definitions:
                rows = (
                    db.query(IndexConstituentHistory.symbol)
                    .filter(
                        IndexConstituentHistory.universe_id == definition.id,
                        IndexConstituentHistory.effective_to.is_(None),
                    )
                    .count()
                )
                indices.append(
                    {
                        "index_code": definition.index_code,
                        "name": definition.index_name,
                        "description": f"{definition.index_name} - {rows} stocks",
                        "count": rows,
                    }
                )
            return indices
        finally:
            db.close()

    def get_constituents(
        self,
        index_code: str,
        target_date: date | None = None,
        mode: UniverseMode = UniverseMode.LIVE,
    ) -> UniverseResult:
        """Get all constituents for an index."""
        if mode == UniverseMode.HISTORICAL:
            raise NotImplementedError(
                "Historical universe lookup is disabled until date-effective membership data "
                "is wired to an authoritative source."
            )
        from ..index_universe_loader import index_universe_loader

        lookup_date = target_date or date.today()
        constituents = self._load_db_constituents(index_code, lookup_date)
        if constituents:
            return UniverseResult(
                index_code=index_code,
                lookup_date=lookup_date,
                constituents=constituents,
                source="database.index_constituents_history",
                is_historical=(mode == UniverseMode.HISTORICAL),
            )

        # Get constituents from loader
        loader = index_universe_loader
        universe = loader.get_index_universe(index_code)
        if universe and universe.constituents:
            constituents = []
            for c in universe.constituents:
                constituents.append(
                    UniverseConstituent(
                        symbol=c.symbol,
                        company_name=c.company_name,
                        industry=getattr(c, "industry", "") or "",
                        index_code=index_code,
                        weight=getattr(c, "weight", None),
                        isin=getattr(c, "isin", None),
                        sector=getattr(c, "sector", None),
                    )
                )

        return UniverseResult(
            index_code=index_code,
            lookup_date=lookup_date,
            constituents=constituents,
            source="index_universe_loader",
            is_historical=(mode == UniverseMode.HISTORICAL),
        )

    def get_symbols(
        self,
        index_code: str,
        _target_date: date | None = None,
        _mode: UniverseMode = UniverseMode.LIVE,
    ) -> list[str]:
        """Get all symbols for an index."""
        if _mode == UniverseMode.HISTORICAL:
            raise NotImplementedError(
                "Historical universe lookup is disabled until date-effective membership data "
                "is wired to an authoritative source."
            )
        from ..index_universe_loader import index_universe_loader

        lookup_date = _target_date or date.today()
        db_symbols = self._load_db_symbols(index_code, lookup_date)
        if db_symbols:
            return db_symbols
        return index_universe_loader.get_index_symbols(index_code)

    def is_constituent(
        self, symbol: str, index_code: str, _as_of_date: date | None = None
    ) -> bool:
        """Check if a symbol is a constituent of an index."""
        from ..index_universe_loader import index_universe_loader

        return index_universe_loader.is_symbol_in_index(symbol, index_code)

    def list_available_indices(self) -> list[dict]:
        """List all available indices with their metadata."""
        indices = self._list_db_indices()
        if indices:
            return indices

        from ..index_universe_loader import index_universe_loader

        fallback: list[dict[str, Any]] = []
        for index_id in index_universe_loader.get_available_indices():
            description = index_universe_loader.get_index_description(index_id)
            universe = index_universe_loader.get_index_universe(index_id)
            count = len(universe.symbols) if universe else 0
            fallback.append(
                {
                    "index_code": index_id,
                    "name": description,
                    "description": f"{description} - {count} stocks",
                    "count": count,
                }
            )
        return fallback

    def get_universe_changes(
        self, _index_code: str, _start_date: date, _end_date: date
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Get changes to an index between two dates.

        Returns additions, removals, and weight changes.
        Note: This is a placeholder implementation. Full implementation would require
        historical constituent data tracking.
        """
        raise NotImplementedError(
            "Universe change history is disabled until date-effective constituent history is "
            "wired to an authoritative source."
        )

    def get_custom_universe(self, universe_id: int) -> UniverseResult:
        """
        Get a custom universe by its ID.

        Args:
            universe_id: The database ID of the custom universe

        Returns:
            UniverseResult with the custom universe constituents
        """
        from sqlalchemy.orm import Session

        from ..database import SessionLocal
        from ..models.universe import CustomUniverseMember

        db: Session = SessionLocal()
        try:
            members = (
                db.query(CustomUniverseMember)
                .filter(CustomUniverseMember.universe_id == universe_id)
                .all()
            )

            constituents = [
                UniverseConstituent(
                    symbol=m.symbol,
                    company_name=m.symbol,  # Use symbol as name if not available
                    industry="",
                    index_code=None,
                )
                for m in members
            ]

            return UniverseResult(
                index_code=f"custom_{universe_id}",
                lookup_date=date.today(),
                constituents=constituents,
                source="custom_universe",
                is_historical=False,
            )
        finally:
            db.close()


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

    def get_constituents(
        self,
        index_code: str,
        target_date: date | None = None,
        mode: UniverseMode = UniverseMode.LIVE,
    ) -> UniverseResult:
        """Get all constituents for an index."""
        return self._impl.get_constituents(index_code, target_date, mode)

    def get_symbols(
        self,
        index_code: str,
        target_date: date | None = None,
        mode: UniverseMode = UniverseMode.LIVE,
    ) -> list[str]:
        """Get all symbols for an index."""
        return self._impl.get_symbols(index_code, target_date, mode)

    def is_constituent(self, symbol: str, index_code: str, as_of_date: date | None = None) -> bool:
        """Check if a symbol is a constituent of an index."""
        return self._impl.is_constituent(symbol, index_code, as_of_date)

    def list_available_indices(self) -> list[dict]:
        """List all available indices with their metadata."""
        return self._impl.list_available_indices()

    def get_universe_changes(
        self, index_code: str, start_date: date, end_date: date
    ) -> dict[str, list[dict[str, Any]]]:
        """Get changes to an index between two dates."""
        return self._impl.get_universe_changes(index_code, start_date, end_date)

    def get_custom_universe(self, universe_id: int) -> UniverseResult:
        """Get a custom universe by its ID."""
        return self._impl.get_custom_universe(universe_id)


# Singleton instance
_universe_service = None


def get_universe_service() -> UniverseService:
    """Get the universe service singleton."""
    global _universe_service
    if _universe_service is None:
        _universe_service = UniverseService()
    return _universe_service


__all__ = [
    "UniverseService",
    "UniverseMode",
    "UniverseConstituent",
    "UniverseResult",
    "get_universe_service",
]
