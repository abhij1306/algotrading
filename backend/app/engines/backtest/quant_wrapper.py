"""
Quant Backtest Wrapper
For "institutional/fund" mode - uses locked strategies from database

Behavior:
- NO parameter tuning allowed
- Strategies are locked logic blocks from StrategyContract table
- Focus on portfolio composition and regime analysis
- Parameters frozen in database
"""
from typing import List, Dict, Any
from datetime import datetime
import pandas as pd
import numpy as np
from sqlalchemy.orm import Session

from ...database import StrategyContract
from ...strategies import ORBStrategy
from ...data_repository import DataRepository
from ...risk_metrics import RiskMetricsEngine
from .core import BacktestCore
from .schemas import BacktestConfig, BacktestResult


class QuantBacktestRunner:
    """
    Quant Mode Backtest Runner
    Uses locked strategy contracts from database
    """
    
    def __init__(self, db_session: Session):
        self.db = db_session
        self.repo = DataRepository(db_session)
    
    async def run_single_strategy(
        self,
        strategy_id: str,
        universe_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, Any]:
        """
        Run backtest for a single locked strategy
        
        Args:
            strategy_id: Strategy contract ID (e.g., 'ORB_NIFTY_5MIN')
            universe_id: Universe to run on (e.g., 'NIFTY50_CORE')
            start_date, end_date: Date range
            
        Returns:
            Aggregated results across universe
        """
        # 1. Load strategy contract from DB
        contract = self.db.query(StrategyContract).filter_by(
            strategy_id=strategy_id
        ).first()
        
        if not contract:
            raise ValueError(f"Strategy contract '{strategy_id}' not found")
        
        # 2. Verify universe is allowed
        if universe_id not in contract.allowed_universes:
            raise ValueError(
                f"Strategy '{strategy_id}' not allowed on universe '{universe_id}'"
            )
        
        # 3. Load universe symbols
        symbols = await self._load_universe(universe_id, start_date)
        
        # 4. Run backtest on each symbol
        results = []
        for symbol in symbols:
            try:
                result = await self._backtest_symbol(
                    contract,
                    symbol,
                    start_date,
                    end_date
                )
                results.append(result)
            except Exception as e:
                print(f"Error backtesting {symbol}: {e}")
                continue
        
        # 5. Aggregate results
        return self._aggregate_results(results, strategy_id, universe_id)
    
    async def run_portfolio(
        self,
        strategy_ids: List[str],
        universe_id: str,
        start_date: datetime,
        end_date: datetime,
        allocation: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        Run multi-strategy portfolio backtest
        
        Args:
            strategy_ids: List of strategy contract IDs
            universe_id: Universe to run on
            start_date, end_date: Date range
            allocation: Strategy weights (e.g., {'ORB_NIFTY': 0.5, 'TREND_BANK': 0.5})
            
        Returns:
            Portfolio-level metrics with correlation analysis
        """
        # Run each strategy
        strategy_results = []
        for strat_id in strategy_ids:
            result = await self.run_single_strategy(
                strat_id,
                universe_id,
                start_date,
                end_date
            )
            strategy_results.append(result)
        
        # Combine with allocation weights
        portfolio_metrics = self._calculate_portfolio_metrics(strategy_results, allocation)

        return {
            'strategies': strategy_results,
            'allocation': allocation,
            'portfolio_metrics': portfolio_metrics,
            'message': 'Portfolio backtest complete'
        }

    def _calculate_portfolio_metrics(
        self,
        strategy_results: List[Dict[str, Any]],
        allocation: Dict[str, float]
    ) -> Dict[str, Any]:
        """
        Calculate aggregated metrics for the entire portfolio

        Args:
            strategy_results: List of strategy result dictionaries (from _aggregate_results)
            allocation: Dictionary of weights {strategy_id: weight}
        """
        if not strategy_results:
            return {}

        # 1. Normalize allocation weights
        total_weight = sum(allocation.values())
        if total_weight == 0:
            return {'error': 'Total allocation weight is zero'}

        weights = {k: v / total_weight for k, v in allocation.items()}

        # 2. Extract and align equity curves
        # Convert each strategy's equity curve to daily returns
        returns_series = {}

        for res in strategy_results:
            strat_id = res['strategy_id']
            daily_equity = res.get('daily_equity', [])

            if not daily_equity:
                continue

            df = pd.DataFrame(daily_equity)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

            # Calculate daily returns
            # Handle division by zero or NaN
            series = df['equity'].pct_change().fillna(0)
            returns_series[strat_id] = series

        if not returns_series:
            return {'error': 'No return data available'}

        # Combine into DataFrame
        returns_df = pd.DataFrame(returns_series).fillna(0)

        # 3. Calculate Portfolio Daily Returns
        # R_port = w1*R1 + w2*R2 ...
        # Ensure we only use weights for strategies present in the results
        portfolio_returns = pd.Series(0.0, index=returns_df.index)

        used_weight_sum = 0
        for strat_id, series in returns_series.items():
            if strat_id in weights:
                w = weights[strat_id]
                portfolio_returns += series * w
                used_weight_sum += w

        # Re-normalize if some strategies were missing
        if used_weight_sum > 0:
            portfolio_returns = portfolio_returns / used_weight_sum

        # 4. Construct Portfolio Equity Curve (base 100)
        portfolio_equity = (1 + portfolio_returns).cumprod() * 100

        # 5. Calculate Metrics using RiskMetricsEngine
        risk_engine = RiskMetricsEngine()

        total_return_pct = (portfolio_equity.iloc[-1] - 100) if not portfolio_equity.empty else 0
        sharpe = risk_engine.sharpe_ratio(portfolio_returns)
        max_dd = risk_engine.max_drawdown(portfolio_equity) * 100
        volatility = risk_engine.annualized_volatility(portfolio_returns) * 100

        # Format for response
        daily_equity_list = [
            {
                'date': date.strftime('%Y-%m-%d'),
                'equity': val
            }
            for date, val in portfolio_equity.items()
        ]

        return {
            'total_return_pct': round(total_return_pct, 2),
            'sharpe_ratio': round(sharpe, 2),
            'max_drawdown_pct': round(abs(max_dd), 2),
            'volatility_pct': round(volatility, 2),
            'daily_equity': daily_equity_list
        }
    
    async def _backtest_symbol(
        self,
        contract: StrategyContract,
        symbol: str,
        start_date: datetime,
        end_date: datetime
    ) -> BacktestResult:
        """Run backtest for a single symbol using locked contract"""
        # Fetch data based on contract timeframe
        data = await self._fetch_data(symbol, start_date, end_date, contract.timeframe)
        
        # Load strategy with FROZEN parameters (no overrides)
        strategy = self._load_locked_strategy(contract)
        
        # Create config (institutional defaults)
        config = BacktestConfig(
            initial_capital=1000000,  # 10L for institutional
            commission_pct=0.03,
            slippage_pct=0.05,
            max_positions=1
        )
        
        # Execute
        core = BacktestCore(strategy, config)
        return core.execute(data, symbol, start_date, end_date)
    
    def _load_locked_strategy(self, contract: StrategyContract):
        """
        Load strategy with LOCKED parameters from contract
        No overrides allowed
        """
        # For now, hard-coded mapping
        # TODO: Make this dynamic via strategy registry
        if contract.strategy_id.startswith('ORB'):
            strategy = ORBStrategy()
            # Parameters are frozen - read from contract metadata if stored
            # For now, use defaults
            return strategy
        
        raise ValueError(f"Unknown strategy type: {contract.strategy_id}")
    
    async def _load_universe(self, universe_id: str, as_of_date: datetime) -> List[str]:
        """
        Load universe symbols as of a specific date
        Handles historical membership changes
        """
        from ...database import StockUniverse
        
        universe = self.db.query(StockUniverse).filter_by(id=universe_id).first()
        
        if not universe:
            raise ValueError(f"Universe '{universe_id}' not found")
        
        # Get symbols for the date
        # symbols_by_date format: {"2024-01-01": ["SYM1", "SYM2"], ...}
        symbols_dict = universe.symbols_by_date
        
        # Find the latest date <= as_of_date
        valid_date = None
        for date_str in sorted(symbols_dict.keys(), reverse=True):
            if datetime.strptime(date_str, '%Y-%m-%d') <= as_of_date:
                valid_date = date_str
                break
        
        if not valid_date:
            raise ValueError(f"No universe data for date {as_of_date}")
        
        return symbols_dict[valid_date]
    
    async def _fetch_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str
    ) -> pd.DataFrame:
        """Fetch market data"""
        # Same as Analyst wrapper
        if timeframe in ['5MIN', '15MIN', '1H']:
            timeframe_map = {'5MIN': 5, '15MIN': 15, '1H': 60}
            tf_minutes = timeframe_map[timeframe]
            
            data = self.repo.get_intraday_candles(
                symbol=symbol,
                timeframe=tf_minutes,
                start_date=start_date,
                end_date=end_date
            )
            
            if data is None or data.empty:
                days = (end_date - start_date).days + 30
                data = self.repo.get_historical_prices(symbol, days=days)
        else:
            days = (end_date - start_date).days + 30
            data = self.repo.get_historical_prices(symbol, days=days)
        
        if data is None or data.empty:
            raise ValueError(f"No data for {symbol}")
        
        return data
    
    def _aggregate_results(
        self,
        results: List[BacktestResult],
        strategy_id: str,
        universe_id: str
    ) -> Dict[str, Any]:
        """Aggregate individual symbol results into strategy-level metrics"""
        if not results:
            return {'error': 'No valid results'}
        
        # 1. Combine equity curves to form Strategy Equity Curve
        # Convert each result's equity curve to a Series
        equity_series_list = []
        for r in results:
            if not r.daily_equity:
                continue

            df = pd.DataFrame(r.daily_equity)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)
            equity_series_list.append(df['equity'])

        if not equity_series_list:
             return {'error': 'No equity data in results'}

        # Combine into a single DataFrame (outer join to handle slight mismatches, though unlikely in backtest)
        # Sum the equity values (Portfolio of Strategies)
        combined_df = pd.concat(equity_series_list, axis=1).fillna(method='ffill').fillna(0)
        strategy_equity = combined_df.sum(axis=1)

        # Calculate strategy-level metrics based on this combined equity
        initial_capital = strategy_equity.iloc[0] if not strategy_equity.empty else 0
        final_equity = strategy_equity.iloc[-1] if not strategy_equity.empty else 0

        # Returns
        total_return_pct = ((final_equity - initial_capital) / initial_capital * 100) if initial_capital > 0 else 0

        # Daily returns for risk metrics
        returns = strategy_equity.pct_change().dropna()

        # Use RiskMetricsEngine for consistent calculation
        risk_engine = RiskMetricsEngine()
        sharpe = risk_engine.sharpe_ratio(returns)
        max_dd = risk_engine.max_drawdown(strategy_equity) * 100 # RiskEngine returns decimal

        # Reconstruct daily_equity list for frontend
        daily_equity_list = [
            {
                'date': date.strftime('%Y-%m-%d'),
                'equity': val
            }
            for date, val in strategy_equity.items()
        ]

        total_trades = sum(r.total_trades for r in results)
        
        return {
            'strategy_id': strategy_id,
            'universe_id': universe_id,
            'num_symbols': len(results),
            'total_return_pct': round(total_return_pct, 2),
            'sharpe_ratio': round(sharpe, 2),
            'max_drawdown_pct': round(abs(max_dd), 2), # Return positive value for display usually
            'total_trades': total_trades,
            'daily_equity': daily_equity_list,
            'symbol_results': [r.dict() for r in results]
        }
