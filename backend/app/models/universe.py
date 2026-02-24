"""
Enhanced Index Membership Model for Historical Tracking
====================================================
Supports accurate historical index composition snapshots for backtesting.
"""

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..base import Base


class IndexUniverseDefinition(Base):
    """
    Master table for index universe definitions.
    Stores metadata about each index tracked by the system.
    """

    __tablename__ = "index_universe_definitions"

    id = Column(Integer, primary_key=True)
    index_code = Column(
        String(20), unique=True, nullable=False, index=True
    )  # 'NIFTY50', 'BANKNIFTY', etc.
    index_name = Column(String(100), nullable=False)
    exchange = Column(String(10), default="NSE")  # NSE, BSE
    is_custom = Column(Boolean, default=False)  # True for user-defined universes
    description = Column(Text)

    # Download tracking
    last_download_date = Column(Date)
    last_weightage_file_date = Column(Date)

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Removed duplicate index - index=True on column already creates an index

    def __repr__(self):
        return f"<IndexUniverseDefinition(code={self.index_code}, name={self.index_name})>"


class IndexConstituentHistory(Base):
    """
    Historical tracking of index constituents with effective dates.
    This is the core table for historical backtesting accuracy.

    Key design:
    - Each symbol's inclusion in an index is tracked with start/end dates
    - Multiple entries per symbol possible (if re-added after removal)
    - Weightage changes tracked separately
    """

    __tablename__ = "index_constituents_history"

    id = Column(Integer, primary_key=True)
    universe_id = Column(Integer, nullable=False)  # FK to IndexUniverseDefinition

    # Symbol identification
    symbol = Column(String(20), nullable=False, index=True)  # DB format: SBIN, RELIANCE
    fyers_symbol = Column(String(50))  # Fyers format: NSE:SBIN-EQ
    isin = Column(String(20))  # Securities Identification Number

    # Date tracking - core for historical accuracy
    effective_from = Column(Date, nullable=False)  # When symbol entered
    effective_to = Column(Date, nullable=True)  # When symbol exited (NULL = still in)

    # Weightage data
    weight = Column(Float)  # Index weight percentage
    market_cap_weight = Column(Float)  # Market cap based weight

    # Company info (snapshot)
    company_name = Column(String(200))
    industry = Column(String(100))

    # Source tracking
    source_file = Column(String(200))  # Which CSV file this came from
    import_date = Column(Date, nullable=False)  # When this data was imported

    # Metadata
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("universe_id", "symbol", "effective_from", name="uq_constituent_history"),
        Index("idx_history_universe_date", "universe_id", "effective_from"),
        Index("idx_history_symbol_dates", "symbol", "effective_from", "effective_to"),
        Index("idx_history_lookup", "universe_id", "effective_from", "effective_to"),
    )

    def __repr__(self):
        return f"<IndexConstituentHistory(symbol={self.symbol}, period={self.effective_from} to {self.effective_to})>"


class IndexWeightageChange(Base):
    """
    Tracks weightage changes for index constituents over time.
    Useful for understanding portfolio rebalancing impacts.
    """

    __tablename__ = "index_weightage_changes"

    id = Column(Integer, primary_key=True)
    universe_id = Column(Integer, nullable=False)
    symbol = Column(String(20), nullable=False, index=True)

    # Change tracking
    change_date = Column(Date, nullable=False, index=True)
    previous_weight = Column(Float)
    new_weight = Column(Float)
    weight_change = Column(Float)  # new_weight - previous_weight

    # Source
    source_file = Column(String(200))
    import_date = Column(Date, nullable=False)

    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (Index("idx_weightage_symbol_date", "symbol", "change_date"),)

    def __repr__(self):
        return f"<IndexWeightageChange(symbol={self.symbol}, date={self.change_date}, change={self.weight_change}%)>"


class CustomUniverse(Base):
    """
    User-defined custom universes for screening and backtesting.
    """

    __tablename__ = "custom_universes"

    id = Column(Integer, primary_key=True)
    universe_code = Column(String(20), unique=True, nullable=False, index=True)
    universe_name = Column(String(100), nullable=False)
    description = Column(Text)

    # Criteria (JSON for flexibility)
    # Example: {"indices": ["NIFTY50"], "filters": {"min_market_cap": 10000000000}}
    criteria = Column(Text)  # JSON string

    created_by = Column(String(50))  # User ID or 'system'
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<CustomUniverse(code={self.universe_code}, name={self.universe_name})>"


class CustomUniverseMember(Base):
    """
    Members of custom universes.
    """

    __tablename__ = "custom_universe_members"

    id = Column(Integer, primary_key=True)
    universe_id = Column(Integer, ForeignKey("index_universe_definitions.id"), nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    weight = Column(Float)

    added_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("universe_id", "symbol", name="uq_custom_universe_member"),
        Index("idx_custom_universe_symbol", "universe_id", "symbol"),
    )


class UniverseSnapshot(Base):
    """
    Pre-computed universe snapshots for fast historical lookups.
    Generated monthly or on-demand.
    """

    __tablename__ = "universe_snapshots"

    id = Column(Integer, primary_key=True)
    universe_id = Column(Integer, ForeignKey("index_universe_definitions.id"), nullable=False)
    snapshot_date = Column(Date, nullable=False, index=True)  # The date this snapshot represents

    # Symbols in this snapshot (stored as JSON for fast retrieval)
    symbols = Column(Text)  # JSON array: ["SBIN", "RELIANCE", ...]

    # Metadata
    generated_at = Column(DateTime, server_default=func.now())
    source_data_date = Column(Date)  # Which weightage file this was generated from

    __table_args__ = (
        UniqueConstraint("universe_id", "snapshot_date", name="uq_universe_snapshot"),
        Index("idx_snapshot_lookup", "universe_id", "snapshot_date"),
    )

    def __repr__(self):
        return f"<UniverseSnapshot(universe={self.universe_id}, date={self.snapshot_date})>"
