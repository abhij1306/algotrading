from sqlalchemy import Column, Integer, String, Date, Float, DateTime, UniqueConstraint, Index, or_
from sqlalchemy.sql import func
from ..base import Base

class IndexMembership(Base):
    """Index membership table for efficient index filtering and historical tracking"""
    __tablename__ = "index_membership"

    id = Column(Integer, primary_key=True)
    index_name = Column(String(50), nullable=False)           # 'NIFTY50', 'NIFTY100', 'BANKNIFTY'
    symbol = Column(String(20), nullable=False)               # 'SBIN', 'RELIANCE', etc.
    start_date = Column(Date, nullable=False)                  # Date stock entered index
    end_date = Column(Date, nullable=True)                    # Date stock exited (NULL = still active)
    weight = Column(Float, nullable=True)                      # Index weight (optional)
    company_name = Column(String(200), nullable=True)          # Full company name
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('index_name', 'symbol', 'start_date', name='uq_index_membership'),
        Index('idx_index_membership_index', 'index_name'),
        Index('idx_index_membership_symbol', 'symbol'),
        Index('idx_index_membership_dates', 'start_date', 'end_date'),
    )

    def __repr__(self):
        return f"<IndexMembership(index={self.index_name}, symbol={self.symbol}, active={self.end_date is None})>"
