from datetime import datetime, timezone

from sqlalchemy import JSON, Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text

from ..base import Base


class BacktestRun(Base):
    """Frozen snapshot of a backtest execution"""

    __tablename__ = "backtest_runs"

    run_id = Column(String(50), primary_key=True)
    name = Column(String(100))
    status = Column(String(20), default="running", index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )
    completed_at = Column(DateTime(timezone=True))

    # Compatibility fields used by legacy engine paths.
    strategy_id = Column(String(50), index=True)
    universe = Column(String(50), index=True)
    initial_capital = Column(Float)
    final_capital = Column(Float)
    total_return = Column(Float)
    sharpe_ratio = Column(Float)
    max_drawdown = Column(Float)

    # Phase-1 immutable run snapshot.
    instrument_type = Column(String(20), default="equity")
    selection_mode = Column(String(20), default="universe")
    scope_label = Column(String(200))
    universe_id = Column(String(50), index=True)
    strategy_configs = Column(JSON)
    portfolio_config = Column(JSON)
    capital_mode = Column(String(20))
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    summary_metrics = Column(JSON)
    strategy_versions = Column(JSON)
    request_payload = Column(JSON)
    result_payload = Column(JSON)
    error_message = Column(Text)


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
    regime_tag = Column(String(20))

    __table_args__ = (Index("ix_run_strat_date", "run_id", "strategy_id", "date", unique=True),)


class PortfolioDailyResult(Base):
    """Aggregated daily output for a multi-strategy portfolio"""

    __tablename__ = "portfolio_daily_results"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(50), ForeignKey("backtest_runs.run_id", ondelete="CASCADE"), index=True)
    date = Column(Date, nullable=False, index=True)

    portfolio_return = Column(Float)
    cumulative_equity = Column(Float)
    portfolio_drawdown = Column(Float)
    strategy_weights = Column(JSON)

    __table_args__ = (Index("ix_portfolio_run_date", "run_id", "date", unique=True),)
