"""
Symbol History Model
===================
Track symbol lifecycle changes (mergers, name changes, de-listings, etc.)
"""
from datetime import UTC, date, datetime
from enum import Enum

from sqlalchemy import Column, Date, DateTime, Integer, String, Text
from sqlalchemy import Enum as SQLEnum

from ..base import Base


class SymbolChangeType(str, Enum):
    """Types of symbol changes"""
    MERGER = "merger"  # Company merged with another
    ACQUISITION = "acquisition"  # Company was acquired
    NAME_CHANGE = "name_change"  # Company renamed
    SYMBOL_CHANGE = "symbol_change"  # Symbol changed (e.g., RPL -> RELIANCE)
    SPLIT = "split"  # Stock split
    DE_LISTING = "de_listing"  # Removed from exchange
    LISTING = "listing"  # New listing
    FPO = "fpo"  # Follow-on public offer
    RIGHTS_ISSUE = "rights_issue"  # Rights issue


class SymbolHistory(Base):
    """
    Track historical symbol changes for resolution.

    This enables accurate historical backtesting by mapping
    old symbols to their current equivalents.
    """
    __tablename__ = "symbol_history"

    id = Column(Integer, primary_key=True, index=True)

    # Original/old symbol
    old_symbol = Column(String(20), nullable=False, index=True)

    # New/current symbol (null if de-listed)
    new_symbol = Column(String(20), nullable=True, index=True)

    # Company name at time of change
    old_company_name = Column(String(200))
    new_company_name = Column(String(200))

    # Type of change
    change_type = Column(SQLEnum(SymbolChangeType), nullable=False)

    # Effective dates
    effective_from = Column(Date, nullable=False, index=True)
    effective_to = Column(Date, nullable=True)  # Null means ongoing

    # Optional: ISIN for cross-referencing
    isin = Column(String(12), nullable=True)

    # Ratio for mergers/splits (e.g., 1:1, 1:5)
    conversion_ratio = Column(String(50))

    # Notes/details
    notes = Column(Text)

    # Metadata
    source = Column(String(50))  # e.g., 'nse_bse', 'manual'
    created_at = Column(DateTime, default=datetime.now(UTC))
    updated_at = Column(DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))

    def __repr__(self):
        return f"<SymbolHistory {self.old_symbol} -> {self.new_symbol} ({self.change_type.value})>"

    def is_active_on(self, check_date: date) -> bool:
        """Check if this symbol mapping was active on a given date"""
        if self.effective_from > check_date:
            return False
        if self.effective_to is not None and self.effective_to < check_date:
            return False
        return True

    def resolve_symbol(self, target_date: date) -> str:
        """
        Resolve the symbol for a given date.

        Returns the symbol that was valid on the target date.
        """
        if not self.is_active_on(target_date):
            raise ValueError(f"Symbol {self.old_symbol} was not active on {target_date}")

        # If the new_symbol became effective before or on target_date, use it
        if self.new_symbol and self.effective_from <= target_date:
            return self.new_symbol

        return self.old_symbol
