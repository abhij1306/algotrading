from sqlalchemy import Column, String, DateTime, JSON, Text
from datetime import datetime
from ..base import Base

class StrategyConfig(Base):
    """Runtime Configuration for Strategies/Agents"""
    __tablename__ = "strategy_configs"

    key = Column(String(100), primary_key=True, index=True)
    value = Column(JSON, nullable=False)
    description = Column(String(200))
    category = Column(String(50), default="GENERAL", index=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class StrategyContract(Base):
    """Read-only strategy-universe-timeframe contracts"""
    __tablename__ = "strategy_contracts"

    strategy_id = Column(String(50), primary_key=True)
    allowed_universes = Column(JSON, nullable=False)
    timeframe = Column(String(10), nullable=False)
    holding_period = Column(String(20), nullable=False)
    regime = Column(String(20), nullable=False)
    when_loses = Column(Text)
    description = Column(Text)
    parameters = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

    # Lifecycle and Governance
    lifecycle_state = Column(String(20), default="RESEARCH")
    state_since = Column(DateTime, default=datetime.now)
    approved_at = Column(DateTime)
    approved_by = Column(String(50))

class StrategyMetadata(Base):
    """
    Rich metadata for strategies (The 'Label')
    Contains forensic analysis notes, risk profile, and lifecycle status.
    """
    __tablename__ = "strategy_metadata"

    strategy_id = Column(String(50), primary_key=True)
    display_name = Column(String(100))
    description = Column(Text)

    regime_notes = Column(Text)

    # Risk Profile (Cached)
    risk_profile = Column(JSON, default={})

    # Lifecycle
    lifecycle_status = Column(String(20), default='RESEARCH')

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
