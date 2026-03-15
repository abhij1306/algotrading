import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text

from ..base import Base


class VCPScanResult(Base):
    __tablename__ = "vcp_scan_results"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(String(36), index=True, default=lambda: str(uuid.uuid4()))
    scan_date = Column(Date, nullable=False, index=True)
    scan_timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    universe = Column(String(50), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200))
    sector = Column(String(100))
    grade = Column(String(1), nullable=False)
    rs_rating = Column(Integer, nullable=False)
    stage2_conditions_met = Column(Integer, nullable=False)
    contraction_count = Column(Integer, nullable=False)
    contraction_depths = Column(JSON, nullable=False)
    final_contraction_depth = Column(Float, nullable=False)
    volume_dry_up_pct = Column(Float, nullable=False)
    pivot_high = Column(Float, nullable=False)
    stop_level = Column(Float, nullable=False)
    stop_pct = Column(Float, nullable=False)
    days_in_base = Column(Integer)
    is_breakout = Column(Boolean, default=False, nullable=False)
    breakout_price = Column(Float)
    breakout_volume_mult = Column(Float)
    close_position_in_range = Column(Float)
    overhead_clear = Column(Boolean, nullable=False)
    regime = Column(String(10), nullable=False)
    signal_status = Column(String(30), default="SIGNAL", nullable=False, index=True)
    metadata_json = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class StrategyPosition(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    scan_result_id = Column(Integer, ForeignKey("vcp_scan_results.id"))
    entry_date = Column(Date, nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=False)
    shares = Column(Integer, nullable=False)
    shares_remaining = Column(Integer, nullable=False)
    two_r_price = Column(Float, nullable=False)
    two_r_hit = Column(Boolean, default=False, nullable=False)
    two_r_hit_date = Column(Date)
    two_r_hit_price = Column(Float)
    regime_at_entry = Column(String(10), nullable=False)
    risk_pct_at_entry = Column(Float, nullable=False)
    status = Column(String(30), nullable=False, index=True)
    exit_date = Column(Date)
    exit_price = Column(Float)
    exit_reason = Column(String(30))
    pnl_inr = Column(Float)
    pnl_pct = Column(Float)
    r_multiple = Column(Float)
    is_paper = Column(Boolean, default=True, nullable=False)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class SystemConfig(Base):
    __tablename__ = "system_config"

    key = Column(String(100), primary_key=True, index=True)
    value = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
