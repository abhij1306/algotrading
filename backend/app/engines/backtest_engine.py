"""
Consolidated Backtesting Engine
================================
Portfolio-level backtesting with index universe reconstruction.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_

from ..database import get_db_session
from ..models.backtest import BacktestDailyResult, BacktestRun
from ..models.universe import (
    IndexConstituentHistory,
    IndexUniverseDefinition,
)

logger = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run"""

    start_date: date
    end_date: date
    initial_capital: float = 1000000.0
    universe: str = "NIFTY50"
    strategy: str = "momentum"
    rebalance_frequency: str = "monthly"
    max_positions: int = 10
    brokerage: float = 0.001
    slippage: float = 0.0005


@dataclass
class Position:
    """Single position in backtest"""

    symbol: str
    entry_date: date
    entry_price: float
    quantity: int
    current_price: float = 0.0
    exit_date: date | None = None
    exit_price: float | None = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity

    @property
    def realized_pnl(self) -> float:
        if self.exit_price:
            return (self.exit_price - self.entry_price) * self.quantity
        return 0.0


@dataclass
class BacktestResult:
    """Results of a backtest run"""

    config: BacktestConfig
    equity_curve: list = field(default_factory=list)
    positions: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    run_id: str | None = None


class BacktestEngine:
    """
    Portfolio-level backtesting engine with historical universe support.
    """

    def __init__(self, config: BacktestConfig, save_to_db: bool = True):
        self.config = config
        self.save_to_db = save_to_db

        self.positions: dict[str, Position] = {}
        self.all_positions: list[Position] = []
        self.cash: float = config.initial_capital
        self.equity_history: list = []
        self.trade_history: list = []

        self._constituents_by_date: dict = {}
        self._current_constituents: set = set()
        self._last_rebalance: date | None = None
        self._daily_prices_cache: dict = {}

        self._run_id: str | None = None

    def run(self) -> BacktestResult:
        """Execute the backtest"""
        logger.info(f"Starting backtest: {self.config.start_date} to {self.config.end_date}")
        logger.info(f"Universe: {self.config.universe}")

        if self.save_to_db:
            self._init_db_run()

        # Load historical constituents
        self._load_historical_constituents()

        # Iterate through trading days
        current_date = self.config.start_date
        while current_date <= self.config.end_date:
            if current_date.weekday() < 5:
                self._process_day(current_date)
                if self.save_to_db and self._run_id:
                    self._save_daily_result(current_date)
            current_date += timedelta(days=1)

        # Close positions at end
        self._close_all_positions(self.config.end_date)
        self.equity_history.append((self.config.end_date, self._get_total_equity()))

        if self.save_to_db:
            self._finalize_db_run()

        result = BacktestResult(
            config=self.config,
            equity_curve=self.equity_history,
            positions=list(self.all_positions),
            trades=self.trade_history,
            metrics=self._calculate_metrics(),
            run_id=self._run_id,
        )

        logger.info(f"Backtest complete. Final equity: {self._get_total_equity():.2f}")
        return result

    def _init_db_run(self):
        """Initialize backtest run in database"""
        session = get_db_session()
        try:
            import uuid

            self._run_id = f"BT-{uuid.uuid4().hex[:8].upper()}"

            run = BacktestRun(
                run_id=self._run_id,
                strategy_id=self.config.strategy,
                universe=self.config.universe,
                start_date=self.config.start_date,
                end_date=self.config.end_date,
                initial_capital=self.config.initial_capital,
                status="running",
            )
            session.add(run)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to init DB run: {e}")
            self._run_id = None
        finally:
            session.close()

    def _save_daily_result(self, current_date: date):
        """Save daily result to database"""
        session = get_db_session()
        try:
            total_equity = self._get_total_equity()
            result = BacktestDailyResult(
                run_id=self._run_id,
                date=current_date,
                equity=total_equity,
                cash=self.cash,
                positions_count=len(self.positions),
                daily_return=0.0,
            )
            session.add(result)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to save daily result: {e}")
        finally:
            session.close()

    def _finalize_db_run(self):
        """Finalize backtest run in database"""
        session = get_db_session()
        try:
            metrics = self._calculate_metrics()
            run = session.query(BacktestRun).filter(BacktestRun.run_id == self._run_id).first()

            if run:
                run.final_capital = metrics.get("final_equity", self.cash)
                run.total_return = metrics.get("total_return", 0)
                run.sharpe_ratio = metrics.get("sharpe_ratio", 0)
                run.max_drawdown = metrics.get("max_drawdown", 0)
                run.status = "completed"
                session.commit()
        except Exception as e:
            logger.error(f"Failed to finalize DB run: {e}")
        finally:
            session.close()

    def _load_historical_constituents(self):
        """Load historical index constituent data"""
        session = get_db_session()

        try:
            # Get universe ID
            universe = (
                session.query(IndexUniverseDefinition)
                .filter(IndexUniverseDefinition.index_code == self.config.universe)
                .first()
            )

            if not universe:
                logger.warning(f"Universe {self.config.universe} not found")
                return

            # Query historical constituents
            history = (
                session.query(IndexConstituentHistory)
                .filter(
                    and_(
                        IndexConstituentHistory.universe_id == universe.id,
                        IndexConstituentHistory.effective_from <= self.config.end_date,
                    )
                )
                .order_by(IndexConstituentHistory.effective_from.desc())
                .all()
            )

            # Build date -> symbols mapping
            self._constituents_by_date = {}
            for record in history:
                eff_date = record.effective_from
                if eff_date not in self._constituents_by_date:
                    self._constituents_by_date[eff_date] = set()
                self._constituents_by_date[eff_date].add(record.symbol)

            logger.info(f"Loaded {len(self._constituents_by_date)} historical snapshots")

        except Exception as e:
            logger.error(f"Error loading constituents: {e}")
            self._constituents_by_date = {}
        finally:
            session.close()

    def _get_constituents_for_date(self, target_date: date) -> set:
        """Get index constituents as of a specific date"""
        effective_date = None
        for ed in sorted(self._constituents_by_date.keys()):
            if ed <= target_date:
                effective_date = ed
            else:
                break

        if effective_date:
            return self._constituents_by_date[effective_date]

        return set()

    def _process_day(self, current_date: date):
        """Process a single trading day"""
        self._current_constituents = self._get_constituents_for_date(current_date)

        if self._should_rebalance(current_date):
            self._rebalance(current_date)

        self._update_positions(current_date)
        total_equity = self._get_total_equity()
        self.equity_history.append((current_date, total_equity))

    def _should_rebalance(self, current_date: date) -> bool:
        """Check if we should rebalance today"""
        if not self._last_rebalance:
            return True

        freq = self.config.rebalance_frequency
        if freq == "daily":
            return self._last_rebalance != current_date
        elif freq == "weekly":
            if current_date.weekday() != 0:
                return False
            return self._last_rebalance.isocalendar()[:2] != current_date.isocalendar()[:2]
        elif freq == "monthly":
            if current_date.day > 5:
                return False
            return (self._last_rebalance.year, self._last_rebalance.month) != (
                current_date.year,
                current_date.month,
            )

        return False

    def _rebalance(self, current_date: date):
        """Rebalance portfolio based on current constituents"""
        sorted_symbols = sorted(self._current_constituents)
        target_symbols = sorted_symbols[: self.config.max_positions]

        if not target_symbols:
            logger.warning(f"No constituents for {current_date}")
            return

        per_position_value = self._get_total_equity() / len(target_symbols)

        # Close positions not in target
        symbols_to_close = set(self.positions.keys()) - set(target_symbols)
        for symbol in symbols_to_close:
            self._close_position(symbol, current_date)

        # Open new positions
        for symbol in target_symbols:
            if symbol not in self.positions:
                price = self._get_price(symbol, current_date)
                if price > 0:
                    quantity = int(per_position_value / price)
                    if quantity > 0:
                        self._open_position(symbol, current_date, price, quantity)

        self._last_rebalance = current_date

    def _open_position(self, symbol: str, date: date, price: float, quantity: int):
        """Open a new position"""
        buy_price = price * (1 + self.config.slippage)

        required_cost = buy_price * quantity * (1 + self.config.brokerage)
        if self.cash < required_cost:
            max_quantity = int(self.cash / (buy_price * (1 + self.config.brokerage)))
            if max_quantity < 1:
                return
            quantity = max_quantity

        position = Position(
            symbol=symbol,
            entry_date=date,
            entry_price=buy_price,
            quantity=quantity,
            current_price=buy_price,
        )

        self.positions[symbol] = position
        self.all_positions.append(position)

        cost = buy_price * quantity * (1 + self.config.brokerage)
        self.cash -= cost

        self.trade_history.append(
            {
                "date": date.isoformat(),
                "symbol": symbol,
                "action": "BUY",
                "price": buy_price,
                "quantity": quantity,
                "value": cost,
            }
        )

    def _close_position(self, symbol: str, date: date):
        """Close an existing position"""
        if symbol not in self.positions:
            return

        position = self.positions[symbol]

        current_price = self._get_price(symbol, date)
        if current_price == 0:
            current_price = position.entry_price

        sell_price = current_price * (1 - self.config.slippage)

        position.exit_date = date
        position.exit_price = sell_price

        proceeds = sell_price * position.quantity * (1 - self.config.brokerage)
        self.cash += proceeds

        self.trade_history.append(
            {
                "date": date.isoformat(),
                "symbol": symbol,
                "action": "SELL",
                "price": sell_price,
                "quantity": position.quantity,
                "value": proceeds,
                "pnl": position.realized_pnl,
            }
        )

        del self.positions[symbol]

    def _close_all_positions(self, date: date):
        """Close all open positions"""
        for symbol in list(self.positions.keys()):
            self._close_position(symbol, date)

    def _update_positions(self, current_date: date):
        """Update current prices for all positions"""
        all_needed = set(self.positions.keys()) | self._current_constituents

        if current_date not in self._daily_prices_cache:
            self._daily_prices_cache[current_date] = self._fetch_daily_prices(
                current_date, list(all_needed)
            )

        prices = self._daily_prices_cache[current_date]

        for position in self.positions.values():
            current_price = prices.get(position.symbol, 0)
            if current_price > 0:
                position.current_price = current_price

    def _fetch_daily_prices(self, target_date, symbols: list) -> dict:
        """Fetch prices for all symbols"""
        # Simplified: return empty for now - actual implementation would query historical_prices
        return {}

    def _get_price(self, symbol: str, target_date: date) -> float:
        """Get closing price for symbol on date"""
        if target_date in self._daily_prices_cache:
            return self._daily_prices_cache[target_date].get(symbol, 0.0)
        return 0.0

    def _get_total_equity(self) -> float:
        """Calculate total portfolio equity"""
        position_value = sum(p.market_value for p in self.positions.values())
        return self.cash + position_value

    def _calculate_metrics(self) -> dict:
        """Calculate backtest performance metrics"""
        if not self.equity_history:
            return {}

        import numpy as np
        import pandas as pd

        equity_df = pd.DataFrame(self.equity_history, columns=["date", "equity"])
        equity_df["date"] = pd.to_datetime(equity_df["date"])
        equity_df = equity_df.set_index("date")

        equity_df["returns"] = equity_df["equity"].pct_change()

        total_return = (equity_df["equity"].iloc[-1] / equity_df["equity"].iloc[0]) - 1

        days = (self.config.end_date - self.config.start_date).days
        years = days / 365
        annualized_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        if equity_df["returns"].std() > 0:
            sharpe = equity_df["returns"].mean() / equity_df["returns"].std() * np.sqrt(252)
        else:
            sharpe = 0

        rolling_max = equity_df["equity"].cummax()
        drawdown = (equity_df["equity"] - rolling_max) / rolling_max
        max_drawdown = drawdown.min()

        closed_trades = [t for t in self.trade_history if t.get("action") == "SELL"]
        winning_trades = [t for t in closed_trades if t.get("pnl", 0) > 0]
        win_rate = len(winning_trades) / len(closed_trades) if closed_trades else 0

        return {
            "final_equity": equity_df["equity"].iloc[-1],
            "total_return": total_return,
            "annualized_return": annualized_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_drawdown,
            "total_trades": len(self.trade_history),
            "winning_trades": len(winning_trades),
            "losing_trades": len(closed_trades) - len(winning_trades),
            "win_rate": win_rate,
        }


def run_backtest(config: BacktestConfig, save_to_db: bool = True) -> BacktestResult:
    """Convenience function to run a backtest"""
    engine = BacktestEngine(config, save_to_db=save_to_db)
    return engine.run()
