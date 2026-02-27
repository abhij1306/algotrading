"""
Historical Universe Manager Service
=================================
Manages index universe compositions for both historical backtesting and live screening.
Supports accurate historical snapshots using timestamped constituent changes.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..models.universe import (
    CustomUniverse,
    CustomUniverseMember,
    IndexConstituentHistory,
    IndexUniverseDefinition,
    UniverseSnapshot,
)
from ..services.symbol_lifecycle import SymbolLifecycleService
from ..services.symbol_master import symbol_master

logger = logging.getLogger(__name__)

# NSE Index weightage file URLs and patterns
NSE_INDEX_FILES = {
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY200": "ind_nifty200list.csv",
    "NIFTY500": "ind_nifty500list.csv",
    "NIFTYBANK": "ind_niftybanklist.csv",
    "NIFTYIT": "ind_niftyitlist.csv",
    "NIFTYFMCG": "ind_niftyfmcglist.csv",
    "NIFTYPHARMA": "ind_niftypharmalist.csv",
    "NIFTYAUTO": "ind_niftyautolist.csv",
    "NIFTYMETAL": "ind_niftymetallist.csv",
    "NIFTYENERGY": "ind_niftyenergylist.csv",
    "NIFTYREALTY": "ind_niftyrealtylist.csv",
}

# Default data path for weightage files
DEFAULT_DATA_PATH = Path(__file__).parent.parent.parent.parent / "data" / "index_weightage"


@dataclass
class UniverseConstituent:
    """Represents a single constituent in an index universe"""

    symbol: str
    fyers_symbol: str
    weight: float | None
    company_name: str | None
    industry: str | None
    isin: str | None


@dataclass
class LocalUniverseSnapshotDTO:
    """Snapshot of universe composition at a specific date"""

    universe_code: str
    snapshot_date: date
    constituents: list[UniverseConstituent]
    total_weight: float


class HistoricalUniverseManager:
    """
    Manages index universe compositions for historical analysis.

    Key features:
    - Historical composition lookup for any date
    - Monthly snapshot generation
    - Custom universe support
    - Integration with existing symbol mapper
    """

    def __init__(self, data_path: Path | None = None):
        self.data_path = data_path or DEFAULT_DATA_PATH
        self.symbol_master = symbol_master
        self.symbol_lifecycle = SymbolLifecycleService()
        self._cache: dict[str, dict[date, list[str]]] = {}  # universe -> date -> symbols

    def resolve_symbol(self, symbol: str, target_date: date, db: Session) -> str:
        """
        Resolve a symbol to its historical name for a specific date.

        This enables accurate historical backtesting by mapping current symbols
        to their historical equivalents (e.g., RELIANCE -> RPL before 2007 merger).

        Args:
            symbol: Current symbol (e.g., 'RELIANCE')
            target_date: Date to resolve to
            db: Database session

        Returns:
            Historical symbol name valid at target_date
        """
        return self.symbol_lifecycle.resolve_symbol(symbol, target_date, db)

    def get_universe_definition(
        self, db: Session, universe_code: str
    ) -> IndexUniverseDefinition | None:
        """Get universe definition by code"""
        return (
            db.query(IndexUniverseDefinition)
            .filter(IndexUniverseDefinition.index_code == universe_code)
            .first()
        )

    def list_universes(
        self, db: Session, include_custom: bool = True
    ) -> list[IndexUniverseDefinition]:
        """List all available universes"""
        query = db.query(IndexUniverseDefinition)
        if not include_custom:
            query = query.filter(IndexUniverseDefinition.is_custom.is_(False))
        return query.all()

    def get_constituents_at_date(
        self, db: Session, universe_code: str, target_date: date
    ) -> list[UniverseConstituent]:
        """
        Get all constituents of a universe at a specific date.

        This is the core method for historical backtesting accuracy.
        It queries the IndexConstituentsHistory table to find all symbols
        that were part of the index at the given date.

        Args:
            db: Database session
            universe_code: Index code (e.g., 'NIFTY50')
            target_date: Date for which to get composition

        Returns:
            List of UniverseConstituent objects
        """
        # Get universe ID
        universe = self.get_universe_definition(db, universe_code)
        if not universe:
            logger.warning(f"Universe {universe_code} not found")
            return []

        # Query constituents that were active at target_date
        # A symbol is active if: effective_from <= target_date AND (effective_to is NULL OR effective_to >= target_date)
        results = (
            db.query(IndexConstituentHistory)
            .filter(
                and_(
                    IndexConstituentHistory.universe_id == universe.id,
                    IndexConstituentHistory.effective_from <= target_date,
                    or_(
                        IndexConstituentHistory.effective_to.is_(None),
                        IndexConstituentHistory.effective_to >= target_date,
                    ),
                )
            )
            .all()
        )

        constituents = []
        for r in results:
            constituents.append(
                UniverseConstituent(
                    symbol=r.symbol,
                    fyers_symbol=r.fyers_symbol or self.symbol_master.to_fyers(r.symbol),
                    weight=r.weight,
                    company_name=r.company_name,
                    industry=r.industry,
                    isin=r.isin,
                )
            )

        logger.debug(f"Found {len(constituents)} constituents for {universe_code} at {target_date}")
        return constituents

    def get_symbols_at_date(
        self, db: Session, universe_code: str, target_date: date, resolve_historical: bool = True
    ) -> list[str]:
        """
        Get list of symbols in universe at a specific date.

        Args:
            db: Database session
            universe_code: Index code (e.g., 'NIFTY50')
            target_date: Date to get symbols for
            resolve_historical: If True, resolve symbols to historical names
                              (e.g., 'RELIANCE' -> 'RPL' for dates before 2007)
        """
        constituents = self.get_constituents_at_date(db, universe_code, target_date)
        symbols = [c.symbol for c in constituents]

        if resolve_historical:
            # Resolve each symbol to its historical name for the target date
            resolved_symbols = []
            for symbol in symbols:
                resolved = self.resolve_symbol(symbol, target_date, db)
                resolved_symbols.append(resolved)
            return resolved_symbols

        return symbols

    def get_universe_symbols(
        self, db: Session, universe_code: str, target_date: date, resolve_historical: bool = True
    ) -> list[str]:
        """
        Get symbols for a universe at a specific date.

        This is an alias for get_symbols_at_date for backwards compatibility.

        Args:
            db: Database session
            universe_code: Index code (e.g., 'NIFTY50')
            target_date: Date to get symbols for
            resolve_historical: If True, resolve symbols to historical names
        """
        return self.get_symbols_at_date(db, universe_code, target_date, resolve_historical)

    def get_constituents_with_weightage(
        self, db: Session, universe_code: str, target_date: date
    ) -> dict[str, float]:
        """Get symbol -> weightage mapping for a universe at a date"""
        constituents = self.get_constituents_at_date(db, universe_code, target_date)
        return {c.symbol: c.weight for c in constituents if c.weight is not None}

    def get_date_range_constituents(
        self, db: Session, universe_code: str, start_date: date, end_date: date
    ) -> dict[date, list[str]]:
        """
        Get constituent lists for each trading day in a date range.
        Useful for running historical backtests.

        Optimized to fetch all IndexConstituentsHistory rows for the universe
        in the entire date window once, then compute daily membership in memory.
        """
        # Get universe definition first
        universe = self.get_universe_definition(db, universe_code)
        if not universe:
            return {}

        # Fetch all constituent history for the universe in the date window in one query
        history_records = (
            db.query(IndexConstituentHistory)
            .filter(
                IndexConstituentHistory.universe_id == universe.id,
                IndexConstituentHistory.effective_from <= end_date,
                or_(
                    IndexConstituentHistory.effective_to.is_(None),
                    IndexConstituentHistory.effective_to >= start_date,
                ),
            )
            .all()
        )

        # Generate all trading dates in range
        dates = []
        current = start_date
        while current <= end_date:
            # Skip weekends
            if current.weekday() < 5:
                dates.append(current)
            current += timedelta(days=1)

        # Build result by computing membership in memory for each date
        result = {}
        for d in dates:
            # Find all symbols that were valid on this date
            symbols = []
            for record in history_records:
                if record.effective_from <= d:
                    if record.effective_to is None or record.effective_to >= d:
                        symbols.append(record.symbol)
            result[d] = symbols

        return result

    def create_universe_snapshot(
        self, db: Session, universe_code: str, snapshot_date: date
    ) -> bool:
        """
        Pre-compute and cache a universe snapshot for fast retrieval.
        Call this after importing new weightage files.
        """
        universe = self.get_universe_definition(db, universe_code)
        if not universe:
            return False

        constituents = self.get_constituents_at_date(db, universe_code, snapshot_date)
        symbols = [c.symbol for c in constituents]

        # Check if snapshot exists
        existing = (
            db.query(UniverseSnapshot)
            .filter(
                and_(
                    UniverseSnapshot.universe_id == universe.id,
                    UniverseSnapshot.snapshot_date == snapshot_date,
                )
            )
            .first()
        )

        if existing:
            existing.symbols = json.dumps(symbols)
            existing.generated_at = datetime.now()
        else:
            snapshot = UniverseSnapshot(
                universe_id=universe.id, snapshot_date=snapshot_date, symbols=json.dumps(symbols)
            )
            db.add(snapshot)

        db.flush()  # Persist snapshot, caller must commit
        logger.info(
            f"Created snapshot for {universe_code} at {snapshot_date}: {len(symbols)} symbols"
        )
        return True

    def get_cached_snapshot(
        self, db: Session, universe_code: str, target_date: date
    ) -> list[str] | None:
        """
        Get pre-cached snapshot if available.
        Falls back to dynamic query if not cached.
        """
        universe = self.get_universe_definition(db, universe_code)
        if not universe:
            return None

        # Find nearest snapshot
        snapshot = (
            db.query(UniverseSnapshot)
            .filter(
                and_(
                    UniverseSnapshot.universe_id == universe.id,
                    UniverseSnapshot.snapshot_date <= target_date,
                )
            )
            .order_by(UniverseSnapshot.snapshot_date.desc())
            .first()
        )

        if snapshot:
            return json.loads(snapshot.symbols)

        # Fallback to dynamic query
        return self.get_symbols_at_date(db, universe_code, target_date)


class UniverseManager(HistoricalUniverseManager):
    """
    Extended universe manager that combines:
    - Historical lookups (from HistoricalUniverseManager)
    - Custom universe management
    - Live screening capabilities
    """

    def __init__(self, data_path: Path | None = None):
        super().__init__(data_path)

    def get_custom_universe_symbols(
        self, db: Session, universe_code: str, _target_date: date | None = None
    ) -> list[str]:
        """
        Get symbols from a custom user-defined universe.
        """
        custom = (
            db.query(CustomUniverse)
            .filter(
                CustomUniverse.universe_code == universe_code, CustomUniverse.is_active.is_(True)
            )
            .first()
        )

        if not custom:
            return []

        members = (
            db.query(CustomUniverseMember)
            .filter(CustomUniverseMember.universe_id == custom.id)
            .all()
        )

        return [m.symbol for m in members]

    def create_custom_universe(
        self,
        db: Session,
        universe_code: str,
        universe_name: str,
        symbols: list[str],
        description: str = "",
        created_by: str = "system",
    ) -> CustomUniverse:
        """Create a new custom universe with symbols"""
        # Check for existing universe with same universe_code
        existing = (
            db.query(CustomUniverse).filter(CustomUniverse.universe_code == universe_code).first()
        )

        if existing:
            raise ValueError(
                f"Custom universe with code '{universe_code}' already exists (ID: {existing.id})"
            )

        custom = CustomUniverse(
            universe_code=universe_code,
            universe_name=universe_name,
            description=description,
            created_by=created_by,
        )
        db.add(custom)
        db.flush()

        # Add members
        for symbol in symbols:
            member = CustomUniverseMember(universe_id=custom.id, symbol=symbol)
            db.add(member)

        db.flush()  # Persist member entries, let caller commit
        logger.info(f"Created custom universe {universe_code} with {len(symbols)} symbols")
        return custom

    def get_available_universes(self, db: Session) -> list[dict]:
        """
        Get list of all available universes with metadata.
        """
        universes = []

        # Standard indices
        for code, _definition in NSE_INDEX_FILES.items():
            universes.append(
                {
                    "code": code,
                    "name": code.replace("NIFTY", "Nifty "),
                    "type": "standard",
                    "source": "NSE",
                }
            )

        # Custom universes
        custom = db.query(CustomUniverse).filter(CustomUniverse.is_active.is_(True)).all()

        for c in custom:
            count = (
                db.query(CustomUniverseMember)
                .filter(CustomUniverseMember.universe_id == c.id)
                .count()
            )

            universes.append(
                {
                    "code": c.universe_code,
                    "name": c.universe_name,
                    "type": "custom",
                    "source": "user",
                    "member_count": count,
                }
            )

        return universes


# Singleton instance
_universe_manager: UniverseManager | None = None


def get_universe_manager() -> UniverseManager:
    """Get singleton universe manager instance"""
    global _universe_manager
    if _universe_manager is None:
        _universe_manager = UniverseManager()
    return _universe_manager
