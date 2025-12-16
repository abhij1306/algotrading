"""
Database models for NSE Trading Screener
Supports historical prices, financial data, and quarterly results
Now using PostgreSQL instead of SQLite
"""
from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, Boolean, Text, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os
from dotenv import load_dotenv

# Load environment variables
# Load environment variables
BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / '.env')

# Database setup - PostgreSQL
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'algotrading')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
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
    """Daily OHLCV data with technical indicators"""
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
    
    # Technical Indicators (calculated and stored)
    ema_20 = Column(Float)  # 20-day EMA
    ema_34 = Column(Float)  # 34-day EMA
    ema_50 = Column(Float)  # 50-day EMA
    rsi = Column(Float)  # 14-day RSI
    atr = Column(Float)  # 14-day ATR
    atr_pct = Column(Float)  # ATR as percentage of close
    
    # Volume metrics
    avg_volume = Column(Float)  # 20-day average volume
    volume_percentile = Column(Float)  # Volume percentile (0-100)
    
    # Trend indicators
    high_20d = Column(Float)  # 20-day high
    is_breakout = Column(Boolean)  # Price at/above 20-day high
    
    # Data source tracking
    source = Column(String(20))  # 'fyers', 'yfinance', etc.
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    company = relationship("Company", back_populates="historical_prices")
    
    # Composite index for fast queries
    __table_args__ = (
        Index('ix_company_date', 'company_id', 'date', unique=True),
    )


class IntradayCandle(Base):
    """Intraday OHLCV candle data for backtesting"""
    __tablename__ = "intraday_candles"
    
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    
    # Timeframe (1, 5, 15, 30, 60 minutes)
    timeframe = Column(Integer, nullable=False)  # in minutes
    
    # OHLCV
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    
    # Additional fields
    trades = Column(Integer)  # Number of trades in this candle
    
    # Data source tracking
    source = Column(String(20), default='fyers')  # 'fyers', 'zerodha', etc.
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    company = relationship("Company")
    
    # Composite index for fast queries by company, timeframe, and timestamp
    __table_args__ = (
        Index('ix_intraday_company_tf_ts', 'company_id', 'timeframe', 'timestamp', unique=True),
        Index('ix_intraday_timestamp', 'timestamp'),
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


class UserPortfolio(Base):
    """User portfolio for risk analysis"""
    __tablename__ = "user_portfolios"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), default='default_user')  # For future multi-user support
    portfolio_name = Column(String(200), nullable=False)
    description = Column(Text)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    positions = relationship("PortfolioPosition", back_populates="portfolio", cascade="all, delete-orphan")
    risk_metrics = relationship("ComputedRiskMetric", back_populates="portfolio", cascade="all, delete-orphan")


class PortfolioPosition(Base):
    """Individual stock position in a portfolio"""
    __tablename__ = "portfolio_positions"
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("user_portfolios.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    
    # Position details
    quantity = Column(Float)  # Number of shares
    avg_buy_price = Column(Float)  # Average purchase price
    invested_value = Column(Float, nullable=False)  # Total invested amount
    allocation_pct = Column(Float)  # Percentage allocation (auto-calculated)
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    portfolio = relationship("UserPortfolio", back_populates="positions")
    company = relationship("Company")
    
    # Composite unique constraint
    __table_args__ = (
        Index('ix_portfolio_company', 'portfolio_id', 'company_id', unique=True),
    )


class ComputedRiskMetric(Base):
    """Cached risk metrics for portfolios"""
    __tablename__ = "computed_risk_metrics"
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("user_portfolios.id"), nullable=False)
    
    # Metric details
    metric_name = Column(String(100), nullable=False)
    metric_value = Column(Float)
    metric_metadata = Column(Text)  # JSON string for additional context
    
    # Timestamp
    computed_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    portfolio = relationship("UserPortfolio", back_populates="risk_metrics")
    
    # Index for fast lookups
    __table_args__ = (
        Index('ix_portfolio_metric', 'portfolio_id', 'metric_name'),
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


class MarketNews(Base):
    """Market news articles for real-time news ticker"""
    __tablename__ = "market_news"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Article details
    title = Column(String(500), nullable=False)
    summary = Column(Text)
    source = Column(String(100))  # 'Google News', 'Yahoo Finance', etc.
    url = Column(Text)
    
    # Timing
    published_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Stock symbols mentioned (comma-separated)
    symbols = Column(String(500))  # e.g., "RELIANCE,TCS,INFY"
    
    # Sentiment analysis
    sentiment = Column(String(20))  # 'positive', 'negative', 'neutral'
    
    # Indexes for fast queries
    __table_args__ = (
    Index('ix_published_at', 'published_at', postgresql_ops={'published_at': 'DESC'}),
        Index('ix_symbols', 'symbols'),
    )


class SmartTraderSignal(Base):
    """Signals generated by Smart Trader system"""
    __tablename__ = "smart_trader_signals"
    
    id = Column(Integer, primary_key=True, index=True)
    
    # Signal identification
    composite_id = Column(String(100), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    direction = Column(String(10), nullable=False)
    
    # Signal classification
    signal_families = Column(Text)  # JSON list
    signal_names = Column(Text)  # JSON list
    confluence_count = Column(Integer, nullable=False)
    aggregate_strength = Column(Float, nullable=False)
    
    # Confidence
    base_confidence = Column(Float, nullable=False)
    final_confidence = Column(Float, nullable=False)
    confidence_level = Column(String(10), nullable=False)
    
    # Deterministic reasoning
    deterministic_reasons = Column(Text)  # JSON list
    merged_features = Column(Text)  # JSON dict
    
    # LLM enhancement (optional)
    llm_confidence_adjustment = Column(Float)
    llm_narrative = Column(Text)
    llm_risk_flags = Column(Text)  # JSON list
    llm_model = Column(String(50))
    llm_processing_time_ms = Column(Integer)
    
    # Trade setup (if constructed)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    target = Column(Float)
    risk_reward_ratio = Column(Float)
    quantity = Column(Integer)
    
    # Trade review (if reviewed)
    llm_review_recommendation = Column(String(20))
    llm_review_reasoning = Column(Text)
    
    # Execution status
    status = Column(String(20), default='PENDING')
    executed_at = Column(DateTime)
    rejection_reason = Column(Text)
    
    # Timestamps
    first_signal_time = Column(DateTime, nullable=False)
    last_signal_time = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Additional data
    signal_metadata = Column(Text)  # JSON - renamed from metadata to avoid SQLAlchemy conflict


# ==================== DATABASE INITIALIZATION ====================

def init_db():
    """Initialize database and create all tables"""
    Base.metadata.create_all(bind=engine)
    print(f"Database initialized: PostgreSQL at {DB_HOST}:{DB_PORT}/{DB_NAME}")


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
