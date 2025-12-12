"""
Database models for NSE Trading Screener
Supports historical prices, financial data, and quarterly results
"""
from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, Boolean, Text, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from pathlib import Path

# Database setup
DB_PATH = Path(__file__).parent.parent / 'data' / 'screener.db'
DB_PATH.parent.mkdir(exist_ok=True)

DATABASE_URL = f"sqlite:///{DB_PATH}"
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ==================== MODELS ====================

class Company(Base):
    """Company master table"""
    __tablename__ = "companies"
    
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(200))
    sector = Column(String(100))
    industry = Column(String(100))
    market_cap = Column(Float)
    is_fno = Column(Boolean, default=False)  # F&O eligible
    is_active = Column(Boolean, default=True)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    historical_prices = relationship("HistoricalPrice", back_populates="company", cascade="all, delete-orphan")
    financial_statements = relationship("FinancialStatement", back_populates="company", cascade="all, delete-orphan")
    quarterly_results = relationship("QuarterlyResult", back_populates="company", cascade="all, delete-orphan")


class HistoricalPrice(Base):
    """Daily OHLCV data"""
    __tablename__ = "historical_prices"
    
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    date = Column(Date, nullable=False, index=True)
    
    # OHLCV
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    
    # Additional fields
    adj_close = Column(Float)  # Adjusted close
    trades = Column(Integer)  # Number of trades
    
    # Data source tracking
    source = Column(String(20))  # 'fyers', 'yfinance', etc.
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    company = relationship("Company", back_populates="historical_prices")
    
    # Composite index for fast queries
    __table_args__ = (
        Index('ix_company_date', 'company_id', 'date', unique=True),
    )


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


class DataUpdateLog(Base):
    """Track data updates for each company"""
    __tablename__ = "data_update_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    
    # Update info
    data_type = Column(String(50), nullable=False)  # 'historical', 'financial', 'quarterly'
    last_update = Column(DateTime, nullable=False)
    records_updated = Column(Integer)
    status = Column(String(20))  # 'success', 'failed', 'partial'
    error_message = Column(Text)
    
    # Date range for historical data
    start_date = Column(Date)
    end_date = Column(Date)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_company_datatype', 'company_id', 'data_type'),
    )


# ==================== DATABASE FUNCTIONS ====================

def init_db():
    """Initialize database and create all tables"""
    Base.metadata.create_all(bind=engine)
    print(f"Database initialized at: {DB_PATH}")


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_company(db, symbol: str, **kwargs):
    """Get existing company or create new one"""
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        company = Company(symbol=symbol, **kwargs)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


# Initialize database on import
init_db()
