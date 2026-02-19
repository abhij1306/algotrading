"""
Backtest V2 Models
==================
Enhanced database models for comprehensive backtest tracking.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..base import Base


class BacktestRunV2(Base):
    """Enhanced backtest run tracking"""
    __tablename__ = "backtest_runs_v2"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), unique=True, nullable=False, index=True)
    user_id = Column(String(50), nullable=True, index=True)  # For multi-user support

    # Configuration (stored as JSON for flexibility)
    config = Column(JSON, nullable=False)
    asset_type = Column(String(20), nullable=False, index=True)  # stock, option, index
    strategy = Column(String(50), nullable=False, index=True)

    # Date range
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)

    # Capital
    initial_capital = Column(Float, nullable=False)
    final_capital = Column(Float)

    # Performance Metrics
    total_return = Column(Float)
    cagr = Column(Float)
    annualized_volatility = Column(Float)
    sharpe_ratio = Column(Float)
    sortino_ratio = Column(Float)
    max_drawdown = Column(Float)
    max_drawdown_duration = Column(Integer)  # days
    calmar_ratio = Column(Float)
    var_95 = Column(Float)

    # Trade Statistics
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    win_rate = Column(Float)
    profit_factor = Column(Float)
    avg_trade_return = Column(Float)
    avg_win = Column(Float)
    avg_loss = Column(Float)
    largest_win = Column(Float)
    largest_loss = Column(Float)
    avg_trade_duration = Column(Float)  # days

    # Status
    status = Column(String(20), default="running", index=True)  # running, completed, failed
    error_message = Column(Text, nullable=True)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # Relationships
    equity_curve = relationship("BacktestEquityPoint", back_populates="run", cascade="all, delete-orphan")
    trades = relationship("BacktestTrade", back_populates="run", cascade="all, delete-orphan")
    monthly_returns = relationship("BacktestMonthlyReturn", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index('ix_backtest_v2_user_created', 'user_id', 'created_at'),
        Index('ix_backtest_v2_strategy_dates', 'strategy', 'start_date', 'end_date'),
    )


class BacktestEquityPoint(Base):
    """Daily equity curve points"""
    __tablename__ = "backtest_equity_points"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs_v2.run_id", ondelete="CASCADE"), index=True)
    date = Column(Date, nullable=False, index=True)

    equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    positions_value = Column(Float, default=0)
    drawdown = Column(Float, default=0)  # Current drawdown from peak

    # Relationship
    run = relationship("BacktestRunV2", back_populates="equity_curve")

    __table_args__ = (
        UniqueConstraint('run_id', 'date', name='uq_equity_point_run_date'),
        Index('ix_equity_curve_run_date', 'run_id', 'date'),
    )


class BacktestTrade(Base):
    """Individual trade records"""
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs_v2.run_id", ondelete="CASCADE"), index=True)

    trade_id = Column(String(50), nullable=False)
    symbol = Column(String(50), nullable=False, index=True)

    # Trade type
    entry_date = Column(Date, nullable=False, index=True)
    exit_date = Column(Date, nullable=True, index=True)

    # Prices
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)

    # Position
    quantity = Column(Integer, nullable=False)
    position_type = Column(String(10), default="long")  # long, short

    # P&L
    pnl = Column(Float)
    return_pct = Column(Float)  # Percentage return

    # Duration
    duration_days = Column(Integer)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship
    run = relationship("BacktestRunV2", back_populates="trades")

    __table_args__ = (
        Index('ix_trades_run_symbol', 'run_id', 'symbol'),
        Index('ix_trades_entry_date', 'run_id', 'entry_date'),
    )


class BacktestMonthlyReturn(Base):
    """Monthly aggregated returns"""
    __tablename__ = "backtest_monthly_returns"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs_v2.run_id", ondelete="CASCADE"), index=True)

    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)  # 1-12
    return_pct = Column(Float, nullable=False)

    # Relationship
    run = relationship("BacktestRunV2", back_populates="monthly_returns")

    __table_args__ = (
        UniqueConstraint('run_id', 'year', 'month', name='uq_monthly_return_run_ym'),
    )


class BacktestComparison(Base):
    """Saved backtest comparisons"""
    __tablename__ = "backtest_comparisons"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), nullable=True, index=True)

    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Comparison configuration
    run_ids = Column(JSON, nullable=False)  # List of run IDs to compare
    comparison_metrics = Column(JSON)  # Cached comparison results

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SavedBacktestConfig(Base):
    """User-saved backtest configurations"""
    __tablename__ = "saved_backtest_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(50), nullable=True, index=True)

    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    # Config
    config = Column(JSON, nullable=False)
    asset_type = Column(String(20), nullable=False)
    strategy = Column(String(50), nullable=False)

    # Tags for organization
    tags = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('ix_saved_config_user', 'user_id', 'created_at'),
    )
