from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text

from ..base import Base


class DataUpdateLog(Base):
    """Track data updates for each company"""
    __tablename__ = "data_update_logs"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    # Update info
    data_type = Column(String(50), nullable=False)
    last_update = Column(DateTime, nullable=False)
    records_updated = Column(Integer)
    status = Column(String(20))
    error_message = Column(Text)

    # Date range
    start_date = Column(Date)
    end_date = Column(Date)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_company_datatype', 'company_id', 'data_type'),
    )

class AllocatorDecision(Base):
    """Audit trail of all allocator weight changes"""
    __tablename__ = "allocator_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    strategy_id = Column(String(50), nullable=False)
    old_weight = Column(Float, nullable=False)
    new_weight = Column(Float, nullable=False)
    delta = Column(Float, nullable=False)
    reason = Column(Text, nullable=False)
    recovery_condition = Column(Text)
    severity = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)
