from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import relationship

from ..base import Base


class Company(Base):
    """Company master data - the core entity all financial data links to"""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(500))
    sector = Column(String(100), index=True)
    industry = Column(String(100))
    is_active = Column(Boolean, default=True, index=True)

    # Market data
    market_cap = Column(Float)  # In crores
    is_fno = Column(Boolean, default=False)

    # Index categorization (broad-based and sector)
    broad_market = Column(String(100), index=True)  # NIFTY50, NIFTY100, NIFTY200, NIFTY500, NIFTYNEXT50, etc.
    sector_index = Column(String(100), index=True)  # NIFTYIT, NIFTYBANK, NIFTYAUTO, NIFTYPHARMA, etc.

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships - these connect to the tables that have ForeignKey to companies.id
    historical_prices = relationship("HistoricalPrice", back_populates="company", lazy="dynamic")
    financial_statements = relationship("FinancialStatement", back_populates="company", lazy="dynamic")
    quarterly_results = relationship("QuarterlyResult", back_populates="company", lazy="dynamic")
    intraday_candles = relationship("IntradayCandle", back_populates="company", lazy="dynamic")

    # Indexes for common queries
    __table_args__ = (
        Index('ix_company_sector_active', 'sector', 'is_active'),
        Index('ix_company_symbol_active', 'symbol', 'is_active'),
        Index('ix_company_broad_market', 'broad_market', 'is_active'),
        Index('ix_company_sector_index', 'sector_index', 'is_active'),
    )

    def __repr__(self):
        return f"<Company(symbol={self.symbol}, name={self.name})>"
