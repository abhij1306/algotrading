"""
Symbol Lifecycle Service
=======================
Service for resolving symbols across historical time periods.
Handles mergers, name changes, de-listings, and symbol changes.
"""
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..database import get_db_session
from ..models.symbol_history import SymbolChangeType, SymbolHistory


class SymbolLifecycleService:
    """
    Service for resolving symbols to their historical equivalents.

    This enables accurate historical backtesting by mapping
    old symbols to their current equivalents and vice versa.
    """

    def __init__(self):
        self._cache: dict[tuple, str | None] = {}

    def resolve_symbol(
        self,
        symbol: str,
        target_date: date,
        session: Session | None = None
    ) -> str:
        """
        Resolve a symbol to what it was called on a specific date.

        Args:
            symbol: The current or historical symbol
            target_date: The date to resolve to
            session: Optional DB session (creates one if not provided)

        Returns:
            The symbol that was valid on the target date
        """
        cache_key = (symbol, target_date.isoformat())
        if cache_key in self._cache:
            return self._cache[cache_key]

        close_session = False
        if session is None:
            session = get_db_session()
            close_session = True

        try:
            # First check: is this symbol active on target_date?
            # Look for any change that affects this symbol on target_date

            # Case 1: Symbol was renamed/changed TO this symbol
            new_symbol_record = session.query(SymbolHistory).filter(
                and_(
                    SymbolHistory.new_symbol == symbol,
                    or_(
                            SymbolHistory.effective_to.is_(None),
                        SymbolHistory.effective_to >= target_date
                    )
                )
            ).first()

            if new_symbol_record:
                # If target_date is before the effective_from date, return old symbol (pre-change)
                if target_date < new_symbol_record.effective_from:
                    result = new_symbol_record.old_symbol
                else:
                    # target_date >= effective_from, return the new symbol (post-change)
                    result = symbol
                self._cache[cache_key] = result
                return result

            # Case 2: Symbol was changed FROM this symbol to something else
            old_symbol_record = session.query(SymbolHistory).filter(
                and_(
                    SymbolHistory.old_symbol == symbol,
                    SymbolHistory.effective_from <= target_date,
                    or_(
                        SymbolHistory.effective_to.is_(None),
                        SymbolHistory.effective_to >= target_date
                    )
                )
            ).first()

            if old_symbol_record and old_symbol_record.new_symbol:
                result = old_symbol_record.new_symbol
                self._cache[cache_key] = result
                return result

            # No change - symbol was the same
            self._cache[cache_key] = symbol
            return symbol

        finally:
            if close_session:
                session.close()

    def get_symbol_history(
        self,
        symbol: str,
        session: Session | None = None
    ) -> list[SymbolHistory]:
        """
        Get full history of changes for a symbol.

        Args:
            symbol: The symbol to look up
            session: Optional DB session

        Returns:
            List of SymbolHistory records (ordered by effective_from)
        """
        close_session = False
        if session is None:
            session = get_db_session()
            close_session = True

        try:
            records = session.query(SymbolHistory).filter(
                or_(
                    SymbolHistory.old_symbol == symbol,
                    SymbolHistory.new_symbol == symbol
                )
            ).order_by(SymbolHistory.effective_from).all()

            return records
        finally:
            if close_session:
                session.close()

    def get_current_symbol(
        self,
        symbol: str,
        session: Session | None = None
    ) -> str:
        """
        Get the current symbol that a historical symbol maps to.

        Args:
            symbol: Historical symbol
            session: Optional DB session

        Returns:
            Current symbol (falling back to input symbol if no mapping)
        """
        return self.resolve_symbol(symbol, date.today(), session)

    def was_listed_on(
        self,
        symbol: str,
        check_date: date,
        session: Session | None = None
    ) -> bool:
        """
        Check if a symbol was listed on a specific date.

        Args:
            symbol: Symbol to check
            check_date: Date to check
            session: Optional DB session

        Returns:
            True if symbol was listed/active on the date
        """
        close_session = False
        if session is None:
            session = get_db_session()
            close_session = True

        try:
            # Check if there's any record that overlaps with check_date
            record = session.query(SymbolHistory).filter(
                or_(
                    and_(
                        SymbolHistory.old_symbol == symbol,
                        or_(
                            SymbolHistory.effective_to.is_(None),
                            SymbolHistory.effective_to >= check_date
                        )
                    ),
                    and_(
                        SymbolHistory.new_symbol == symbol,
                        SymbolHistory.effective_from <= check_date
                    )
                )
            ).first()

            # If no record exists, assume it was listed (current symbol)
            if record is None:
                return True

            # Check if the symbol wasn't listed yet on check_date
            if record.list_date is not None and record.list_date > check_date:
                return False

            # Check if the symbol was de-listed before or on check_date
            if record.de_list_date is not None and record.de_list_date <= check_date:
                return False

            return True
        finally:
            if close_session:
                session.close()

    def add_symbol_change(
        self,
        old_symbol: str,
        new_symbol: str | None,
        change_type: SymbolChangeType,
        effective_from: date,
        effective_to: date | None = None,
        old_company_name: str | None = None,
        new_company_name: str | None = None,
        conversion_ratio: str | None = None,
        notes: str | None = None,
        source: str = "manual",
        session: Session | None = None
    ) -> SymbolHistory:
        """
        Add a new symbol change record.

        Args:
            old_symbol: The old symbol
            new_symbol: The new symbol (None if de-listed)
            change_type: Type of change
            effective_from: Date the change became effective
            effective_to: Date the change ended (None for ongoing)
            old_company_name: Old company name
            new_company_name: New company name
            conversion_ratio: Ratio for mergers/splits
            notes: Additional notes
            source: Source of the information
            session: Optional DB session

        Returns:
            Created SymbolHistory record
        """
        close_session = False
        if session is None:
            session = get_db_session()
            close_session = True

        try:
            history = SymbolHistory(
                old_symbol=old_symbol,
                new_symbol=new_symbol,
                change_type=change_type,
                effective_from=effective_from,
                effective_to=effective_to,
                old_company_name=old_company_name,
                new_company_name=new_company_name,
                conversion_ratio=conversion_ratio,
                notes=notes,
                source=source
            )

            session.add(history)
            session.commit()
            session.refresh(history)

            # Clear cache
            self._cache.clear()

            return history
        finally:
            if close_session:
                session.close()

    def get_active_symbols(
        self,
        target_date: date,
        session: Session | None = None
    ) -> list[str]:
        """
        Get all symbols that were active on a given date.

        Args:
            target_date: Date to check
            session: Optional DB session

        Returns:
            List of active symbols
        """
        close_session = False
        if session is None:
            session = get_db_session()
            close_session = True

        try:
            # Get all records active on target_date
            records = session.query(SymbolHistory).filter(
                and_(
                    SymbolHistory.effective_from <= target_date,
                    or_(
                        SymbolHistory.effective_to.is_(None),
                        SymbolHistory.effective_to >= target_date
                    )
                )
            ).all()

            symbols = set()
            for record in records:
                if record.effective_from <= target_date:
                    if record.new_symbol and record.effective_from <= target_date:
                        symbols.add(record.new_symbol)
                    else:
                        symbols.add(record.old_symbol)

            return sorted(list(symbols))
        finally:
            if close_session:
                session.close()


# Singleton instance
_symbol_lifecycle_service: SymbolLifecycleService | None = None


def get_symbol_lifecycle_service() -> SymbolLifecycleService:
    """Get the singleton SymbolLifecycleService instance"""
    global _symbol_lifecycle_service
    if _symbol_lifecycle_service is None:
        _symbol_lifecycle_service = SymbolLifecycleService()
    return _symbol_lifecycle_service
