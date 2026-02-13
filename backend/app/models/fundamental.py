from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from ..base import Base

class FinancialStatement(Base):
    """Annual/Quarterly financial statements"""
    __tablename__ = "financial_statements"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    # Period info
    period_end = Column(Date, nullable=False, index=True)
    period_type = Column(String(10), nullable=False)  # 'annual', 'quarterly'
    fiscal_year = Column(Integer)
    quarter = Column(Integer)  # 1, 2, 3, 4 (null for annual)

    # Income Statement
    revenue = Column(Float)
    operating_income = Column(Float)
    net_income = Column(Float)
    ebitda = Column(Float)
    eps = Column(Float)  # Earnings per share

    # Balance Sheet
    total_assets = Column(Float)
    total_liabilities = Column(Float)
    shareholders_equity = Column(Float)
    total_debt = Column(Float)
    cash_and_equivalents = Column(Float)

    # Cash Flow
    operating_cash_flow = Column(Float)
    investing_cash_flow = Column(Float)
    financing_cash_flow = Column(Float)
    free_cash_flow = Column(Float)

    # Ratios (calculated or from source)
    pe_ratio = Column(Float)
    pb_ratio = Column(Float)
    debt_to_equity = Column(Float)
    roe = Column(Float)  # Return on equity
    roa = Column(Float)  # Return on assets

    # Metadata
    source = Column(String(50))  # Data source
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="financial_statements")

    # Composite index
    __table_args__ = (
        Index('ix_company_period', 'company_id', 'period_end', 'period_type', unique=True),
    )


class QuarterlyResult(Base):
    """Quarterly results announcements"""
    __tablename__ = "quarterly_results"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)

    # Quarter info
    quarter_end = Column(Date, nullable=False, index=True)
    fiscal_year = Column(Integer)
    quarter = Column(Integer)  # 1, 2, 3, 4

    # Key metrics
    revenue = Column(Float)
    net_profit = Column(Float)
    eps = Column(Float)

    # Growth metrics (YoY)
    revenue_growth_yoy = Column(Float)
    profit_growth_yoy = Column(Float)

    # Announcement info
    announcement_date = Column(Date)
    result_type = Column(String(20))  # 'audited', 'unaudited', 'preliminary'

    # Additional data (JSON for flexibility)
    additional_data = Column(Text)  # Store as JSON string

    # Metadata
    source = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    company = relationship("Company", back_populates="quarterly_results")

    # Composite index
    __table_args__ = (
        Index('ix_company_quarter', 'company_id', 'quarter_end', unique=True),
    )
