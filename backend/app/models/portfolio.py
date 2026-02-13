from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Index, JSON, Text, func
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid
from ..base import Base

class PortfolioPolicy(Base):
    """
    Risk Governance Rules (The 'Mortar')
    Users define these rules to control how strategies are allocated.
    """
    __tablename__ = "portfolio_policies"

    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)

    # Risk Limits
    cash_reserve_percent = Column(Float, default=20.0)
    daily_stop_loss_percent = Column(Float, default=2.0)
    max_equity_exposure_percent = Column(Float, default=80.0)
    max_strategy_allocation_percent = Column(Float, default=25.0)

    # Allocator Logic
    allocation_sensitivity = Column(String(20), default="MEDIUM")
    correlation_penalty = Column(String(20), default="MODERATE")

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
    composition = Column(JSON, nullable=False)

    total_return = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    sharpe_ratio = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    policy = relationship("PortfolioPolicy", back_populates="portfolios")


class UserPortfolio(Base):
    """User portfolio for risk analysis"""
    __tablename__ = "user_portfolios"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), default='default_user')
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
    quantity = Column(Float)
    avg_buy_price = Column(Float)
    invested_value = Column(Float, nullable=False)
    allocation_pct = Column(Float)

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
    metric_metadata = Column(Text)

    # Timestamp
    computed_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    portfolio = relationship("UserPortfolio", back_populates="risk_metrics")

    # Index for fast lookups
    __table_args__ = (
        Index('ix_portfolio_metric', 'portfolio_id', 'metric_name'),
    )

class UserStockPortfolio(Base):
    """User-defined explicit stock lists"""
    __tablename__ = "user_stock_portfolios"
    portfolio_id = Column(String(50), primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)
    symbols = Column(JSON, nullable=False)  # List of symbols
    created_at = Column(DateTime, default=datetime.now)

class PortfolioDailyState(Base):
    """Daily snapshot of live portfolio state"""
    __tablename__ = "portfolio_daily_state"

    date = Column(Date, primary_key=True)
    run_id = Column(String(50))  # Links to active BacktestRun
    equity = Column(Float, nullable=False)
    drawdown = Column(Float)
    volatility = Column(Float)
    volatility_regime = Column(String(20))
    risk_state = Column(String(20))
    risk_state_reason = Column(Text)
    strategy_weights = Column(JSON)
    created_at = Column(DateTime, default=datetime.now)

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
    total_equity = Column(Float)
    cash_balance = Column(Float)
    deployed_capital = Column(Float)
    current_drawdown_pct = Column(Float)

    # Health Status
    is_breached = Column(Boolean, default=False)
    breach_details = Column(String(255))

    # Strategy Performance Snapshot
    strategy_performance = Column(JSON, default={})

    # Relationships
    portfolio = relationship("ResearchPortfolio")
