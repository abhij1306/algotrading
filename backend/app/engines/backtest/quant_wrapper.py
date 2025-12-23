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
from sqlalchemy.orm import Session

from ...database import StrategyContract
from ...strategies import ORBStrategy
from ...data_repository import DataRepository
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
        # TODO: Implement portfolio-level metrics
        return {
            'strategies': strategy_results,
            'allocation': allocation,
            'message': 'Portfolio backtest complete'
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
        
        # Combine equity curves (assumption: same timestamps)
        # For simplicity, average returns
        avg_return = sum(r.total_return_pct for r in results) / len(results)
        avg_sharpe = sum(r.sharpe_ratio for r in results) / len(results)
        max_dd = max(r.max_drawdown_pct for r in results)
        total_trades = sum(r.total_trades for r in results)
        
        return {
            'strategy_id': strategy_id,
            'universe_id': universe_id,
            'num_symbols': len(results),
            'avg_return_pct': round(avg_return, 2),
            'avg_sharpe': round(avg_sharpe, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'total_trades': total_trades,
            'symbol_results': [r.dict() for r in results]
        }
