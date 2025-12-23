"""
Database models for NSE Trading Screener
Supports historical prices, financial data, and quarterly results
Now using PostgreSQL instead of SQLite
"""
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, Date, DateTime, Boolean, Text, ForeignKey, Index, JSON, func, text, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os
# from dotenv import load_dotenv
from .utils.env_loader import load_dotenv

import sys
# Load environment variables with resilient path discovery
def get_env_file():
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        # Check standard locations for .env relative to frozen exe
        potential_paths = [
            os.path.join(exe_dir, ".env"),
            os.path.join(os.path.dirname(exe_dir), ".env"),
            os.path.join(os.getcwd(), ".env")
        ]
        for p in potential_paths:
            if os.path.exists(p): return p
        return ".env" # Fallback
    else:
        # Development mode
        return Path(__file__).resolve().parent.parent.parent / '.env'

env_path = get_env_file()
load_dotenv(env_path)
load_dotenv(env_path)

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
    learning_artifacts = relationship("LearningArtifact", back_populates="company", cascade="all, delete-orphan")

class LearningArtifact(Base):
    __tablename__ = "learning_artifacts"
    
    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True) # Can be null for general market learning
    agent_id = Column(String(50), nullable=False) # 'data_agent', 'strategy_agent', etc.
    artifact_type = Column(String(50), nullable=False) # 'insight', 'pattern', 'model_v1'
    content = Column(JSON, nullable=False)
    version = Column(Integer, default=1)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    company = relationship("Company", back_populates="learning_artifacts")


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
    volume = Column(BigInteger, nullable=False)
    
    # Delivery data
    deliverable_qty = Column(BigInteger)
    delivery_pct = Column(Float)
    
    # Technical Indicators - Basic
    ema_20 = Column(Float)
    ema_50 = Column(Float)
    rsi_14 = Column(Float)
    atr_14 = Column(Float)
    
    # Technical Indicators - Advanced (NEW)
    macd = Column(Float)  # MACD line
    macd_signal = Column(Float)  # Signal line
    macd_histogram = Column(Float)  # Histogram
    stoch_k = Column(Float)  # Stochastic %K
    stoch_d = Column(Float)  # Stochastic %D
    bb_upper = Column(Float)  # Bollinger upper band
    bb_middle = Column(Float)  # Bollinger middle band (20 SMA)
    bb_lower = Column(Float)  # Bollinger lower band
    adx = Column(Float)  # Average Directional Index
    obv = Column(BigInteger)  # On-Balance Volume
    
    # Trend indicators
    high_20d = Column(Float)  # 20-day high
    is_breakout = Column(Boolean)  # Price at/above 20-day high
    
    # New Trend Metrics (Pre-calculated)
    trend_7d = Column(Float)   # % Change over 7 days (5 trading days)
    trend_30d = Column(Float)  # % Change over 30 days (21 trading days)
    
    # Data source tracking
    source = Column(String(20))  # 'fyers', 'yfinance', etc.
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    company = relationship("Company", back_populates="historical_prices")
    
    # Composite index for fast queries
    __table_args__ = (
        Index('ix_company_date', 'company_id', 'date', unique=True),
    )


# ==================== QUANT MODULE MODELS ====================

class AllocationSensitivity(str, Enum):
    LOW = "LOW"         # Slow reaction to volatility
    MEDIUM = "MEDIUM"   # Balanced
    HIGH = "HIGH"       # Fast cutbacks

class CorrelationPenalty(str, Enum):
    NONE = "NONE"       # Ignore correlation
    LOW = "LOW"         # Light penalty
    MODERATE = "MODERATE"
    HIGH = "HIGH"       # Strict diversification

class PortfolioStatus(str, Enum):
    RESEARCH = "RESEARCH"
    LIVE = "LIVE"
    ARCHIVED = "ARCHIVED"

class PortfolioPolicy(Base):
    """
    Risk Governance Rules (The 'Mortar')
    Users define these rules to control how strategies are allocated.
    """
    __tablename__ = "portfolio_policies"

    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    
    # Risk Limits
    cash_reserve_percent = Column(Float, default=20.0)      # e.g., 20% always in Liquid Bees
    daily_stop_loss_percent = Column(Float, default=2.0)    # e.g., Close all if down 2% in a day
    max_equity_exposure_percent = Column(Float, default=80.0) # Max capital deployed
    max_strategy_allocation_percent = Column(Float, default=25.0) # Max per strategy
    
    # Allocator Logic
    allocation_sensitivity = Column(String(20), default="MEDIUM") # Enum stored as string
    correlation_penalty = Column(String(20), default="MODERATE") # Enum stored as string
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    portfolios = relationship("ResearchPortfolio", back_populates="policy")

class ResearchPortfolio(Base):
    """
    Immutable Strategy Composition (The 'Bricks')
    A collection of strategies with target weights.
    """
    __tablename__ = "research_portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    
    policy_id = Column(String(50), ForeignKey("portfolio_policies.id"), nullable=False)
    status = Column(String(20), default="RESEARCH")
    
    # Metadata
    description = Column(String(500))
    benchmark = Column(String(50), default="NIFTY 50")
    initial_capital = Column(Float, default=100000.0)

    # Composition: List of {strategy_id: str, allocation_percent: float}
    # Using JSONB for efficient querying if needed, falling back to JSON
    composition = Column(JSON, nullable=False) 
    
    total_return = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    policy = relationship("PortfolioPolicy", back_populates="portfolios")


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



class AgentAuditLog(Base):
    """Audit log for all agent decisions"""
    __tablename__ = "agent_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String(50), nullable=False, index=True)
    action_type = Column(String(50), nullable=False)  # 'SIGNAL_GENERATION', 'TRADE_EXECUTION', 'RISK_CHECK'
    symbol = Column(String(20))
    
    # Context
    input_snapshot = Column(JSON)  # What the agent saw
    decision = Column(JSON)        # What the agent decided
    reasoning = Column(Text)       # Why
    confidence = Column(Float)
    
    # Outcome
    status = Column(String(20))    # 'SUCCESS', 'FAILURE', 'SKIPPED'
    execution_time_ms = Column(Integer)
    
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PaperOrder(Base):
    """Paper Trading Order Book"""
    __tablename__ = "paper_orders"
    
    id = Column(String(50), primary_key=True)  # UUID
    user_id = Column(String(50), default='default_user', index=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)  # BUY/SELL
    quantity = Column(Integer, nullable=False)
    product_type = Column(String(20), default='INTRADAY')  # CNC/INTRADAY
    order_type = Column(String(20), default='MARKET')      # MARKET/LIMIT
    
    price = Column(Float)
    trigger_price = Column(Float)
    
    status = Column(String(20), default='PENDING', index=True)
    filled_quantity = Column(Integer, default=0)
    average_price = Column(Float)
    
    message = Column(Text)
    parent_order_id = Column(String(50))  # For bracket orders
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PaperTrade(Base):
    """Paper Trading Trade Book (Fills)"""
    __tablename__ = "paper_trades"
    
    id = Column(String(50), primary_key=True)  # UUID
    order_id = Column(String(50), ForeignKey("paper_orders.id"), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    value = Column(Float, nullable=False)
    
    commission = Column(Float, default=0.0)
    realized_pnl = Column(Float)  # Only for closing trades
    
    trade_time = Column(DateTime, default=datetime.utcnow)


class PaperPosition(Base):
    """Paper Trading Open Positions"""
    __tablename__ = "paper_positions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), default='default_user')
    symbol = Column(String(20), nullable=False, index=True)
    product_type = Column(String(20), default='INTRADAY')
    side = Column(String(10), nullable=False)  # LONG/SHORT
    
    quantity = Column(Integer, nullable=False)
    average_price = Column(Float, nullable=False)
    
    ltp = Column(Float)
    pnl = Column(Float)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_paper_position_sym', 'user_id', 'symbol', 'product_type', unique=True),
    )


class PaperFund(Base):
    """Paper Trading Funds (Ledger)"""
    __tablename__ = "paper_funds"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(String(50), unique=True, nullable=False)
    
    available_balance = Column(Float, default=1000000.0)
    used_margin = Column(Float, default=0.0)
    total_balance = Column(Float, default=1000000.0)
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)



class StrategyConfig(Base):
    """Runtime Configuration for Strategies/Agents"""
    __tablename__ = "strategy_configs"
    
    key = Column(String(100), primary_key=True, index=True)
    value = Column(JSON, nullable=False) # Store complex config as JSON
    description = Column(String(200))
    # Category (Risk, Trade, System)
    category = Column(String(50), default="GENERAL", index=True) 
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ActionCenter(Base):
    """Pending Actions/Orders requiring human approval"""
    __tablename__ = "action_center"
    
    id = Column(Integer, primary_key=True, index=True)
    source_agent = Column(String(50), nullable=False) # 'FastLoop', 'StrategyAgent'
    action_type = Column(String(50), nullable=False) # 'PLACE_ORDER', 'CLOSE_POSITION'
    
    # Action Details (JSON)
    payload = Column(JSON, nullable=False) # The order dict
    
    # Context
    reason = Column(String(500))
    confidence = Column(Float)
    
    # Status
    status = Column(String(20), default='PENDING', index=True) # PENDING, APPROVED, REJECTED, EXECUTED, FAILED
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


# Initialize database on import (attempt only)
try:
    init_db()
except Exception as e:
    print(f"⚠️ Warning: Database initialization failed: {e}")
    print("  (This is expected during tests or if DB is offline)")
class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    instrument_type = Column(String, default="EQ")
    added_at = Column(DateTime, default=func.now())


# --- Portfolio Backtesting System Models ---

class StockUniverse(Base):
    """Immutable stock universe definitions with historical membership"""
    __tablename__ = "stock_universes"
    
    id = Column(String(50), primary_key=True)  # e.g. "NIFTY100_CORE"
    description = Column(String(500))
    # symbols_by_date: { "YYYY-MM-DD": ["SYM1", "SYM2", ...], ... }
    # Only entries for dates where membership changes.
    symbols_by_date = Column(JSON, nullable=False) 
    rebalance_frequency = Column(String(20))  # MONTHLY | NONE
    selection_rules = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserStockPortfolio(Base):
    """User-defined explicit stock lists"""
    __tablename__ = "user_stock_portfolios"
    portfolio_id = Column(String(50), primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    symbols = Column(JSON, nullable=False)  # List of symbols
    created_at = Column(DateTime, default=datetime.now)

class BacktestRun(Base):
    """Frozen snapshot of a backtest execution"""
    __tablename__ = "backtest_runs"
    
    run_id = Column(String(50), primary_key=True)  # UUID
    universe_id = Column(String(50), index=True)
    strategy_configs = Column(JSON)  # List of strategy params
    portfolio_config = Column(JSON)  # Allocation settings
    capital_mode = Column(String(20)) # FIXED | PERCENT
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    
    summary_metrics = Column(JSON) # { "cagr": ..., "sharpe": ..., "max_dd": ... }
    
    
# ============================================================================
# PORTFOLIO RESEARCH & LIVE MONITORING SYSTEM TABLES
# ============================================================================

class StrategyContract(Base):
    """Read-only strategy-universe-timeframe contracts"""
    __tablename__ = "strategy_contracts"
    
    strategy_id = Column(String(50), primary_key=True)
    allowed_universes = Column(JSON, nullable=False)  # List of universe IDs
    timeframe = Column(String(10), nullable=False)  # "5MIN" or "DAILY"
    holding_period = Column(String(20), nullable=False)  # "INTRADAY" or "MULTI_DAY"
    regime = Column(String(20), nullable=False)  # "TREND", "RANGE", "EVENT", "INDEX"
    when_loses = Column(Text)  # Plain English explanation
    description = Column(Text)
    parameters = Column(JSON)  # Strategy parameters
    created_at = Column(DateTime, default=datetime.now)
    
    # Lifecycle and Governance
    lifecycle_state = Column(String(20), default="RESEARCH")  # "RESEARCH", "PAPER", "LIVE", "RETIRED"
    state_since = Column(DateTime, default=datetime.now)
    approved_at = Column(DateTime)
    approved_by = Column(String(50))


class PortfolioDailyState(Base):
    """Daily snapshot of live portfolio state"""
    __tablename__ = "portfolio_daily_state"
    
    date = Column(Date, primary_key=True)
    run_id = Column(String(50))  # Links to active BacktestRun
    equity = Column(Float, nullable=False)
    drawdown = Column(Float)
    volatility = Column(Float)
    volatility_regime = Column(String(20))  # "LOW", "MODERATE", "HIGH"
    risk_state = Column(String(20))  # "NORMAL", "CAUTIOUS", "DEFENSIVE"
    risk_state_reason = Column(Text)
    strategy_weights = Column(JSON)  # Dict of strategy: weight
    created_at = Column(DateTime, default=datetime.now)

class AllocatorDecision(Base):
    """Audit trail of all allocator weight changes"""
    __tablename__ = "allocator_decisions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, index=True)
    strategy_id = Column(String(50), nullable=False)
    old_weight = Column(Float, nullable=False)
    new_weight = Column(Float, nullable=False)
    delta = Column(Float, nullable=False)
    reason = Column(Text, nullable=False)  # Plain English
    recovery_condition = Column(Text)  # What needs to happen to reverse
    severity = Column(String(20))  # "NORMAL", "CAUTIOUS", "DEFENSIVE"
    created_at = Column(DateTime, default=datetime.now)




class BacktestDailyResult(Base):
    """Daily normalized output for an individual strategy"""
    __tablename__ = "backtest_daily_results"
    
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs.run_id", ondelete="CASCADE"), index=True)
    date = Column(Date, nullable=False, index=True)
    strategy_id = Column(String(50), nullable=False)
    universe_id = Column(String(50))
    
    daily_return = Column(Float)
    gross_pnl = Column(Float)
    capital_allocated = Column(Float)
    number_of_trades = Column(Integer)
    max_intraday_drawdown = Column(Float)
    win_rate = Column(Float)
    regime_tag = Column(String(20)) # TREND|RANGE|EVENT|INDEX_RANGE
    
    __table_args__ = (
        Index('ix_run_strat_date', 'run_id', 'strategy_id', 'date', unique=True),
    )

class PortfolioDailyResult(Base):
    """Aggregated daily output for a multi-strategy portfolio"""
    __tablename__ = "portfolio_daily_results"
    
    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs.run_id", ondelete="CASCADE"), index=True)
    date = Column(Date, nullable=False, index=True)
    
    portfolio_return = Column(Float)
    cumulative_equity = Column(Float)
    portfolio_drawdown = Column(Float)
    strategy_weights = Column(JSON) # { "strat_id": weight }
    
    __table_args__ = (
        Index('ix_portfolio_run_date', 'run_id', 'date', unique=True),
    )


# ============================================================================
# QUANT LIVE MONITORING
# ============================================================================

class StrategyMetadata(Base):
    """
    Rich metadata for strategies (The 'Label')
    Contains forensic analysis notes, risk profile, and lifecycle status.
    """
    __tablename__ = "strategy_metadata"
    
    strategy_id = Column(String(50), primary_key=True) # Matches class name e.g., 'MomentumStrategy'
    display_name = Column(String(100))
    description = Column(Text)
    
    # "When it Loses" - Forensic Analysis
    regime_notes = Column(Text) # e.g. "Fails in chopping sideways markets"
    
    # Risk Profile (Cached)
    # { "sharpe": 1.2, "max_dd": 15.0, "win_rate": 45, "calmar": 0.8 }
    risk_profile = Column(JSON, default={}) 
    
    # Lifecycle
    lifecycle_status = Column(String(20), default='RESEARCH') # RESEARCH, INCUBATION, LIVE, RETIRED
    
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LivePortfolioState(Base):
    """
    Real-time snapshot of portfolio health for Monitoring Dashboard.
    Logged every minute or on-demand.
    """
    __tablename__ = "live_portfolio_states"
    
    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey('research_portfolios.id'), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    
    # Real-time Metrics
    total_equity = Column(Float)       # Total Account Value
    cash_balance = Column(Float)       # Available Cash
    deployed_capital = Column(Float)   # Amount in strategies
    current_drawdown_pct = Column(Float) # Calculated from Peak
    
    # Health Status
    is_breached = Column(Boolean, default=False)
    breach_details = Column(String(255)) # "Daily Stop Loss Exceeded (-2.5%)"
    
    # Strategy Performance Snapshot
    # { "MomentumStrategy": { "pnl": 500, "allocation": 0.25, "equity": 250000 } }
    strategy_performance = Column(JSON, default={})
    
    # Relationships
    portfolio = relationship("ResearchPortfolio")



