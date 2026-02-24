from sqlalchemy import JSON, Column, Date, Float, ForeignKey, Index, Integer, String

from ..base import Base


class BacktestRun(Base):
    """Frozen snapshot of a backtest execution"""

    __tablename__ = "backtest_runs"

    run_id = Column(String(50), primary_key=True)
    universe_id = Column(String(50), index=True)
    strategy_configs = Column(JSON)
    portfolio_config = Column(JSON)
    capital_mode = Column(String(20))
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)

    summary_metrics = Column(JSON)


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
