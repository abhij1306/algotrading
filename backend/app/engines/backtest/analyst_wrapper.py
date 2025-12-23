"""
Analyst Backtest Wrapper
For "retail/playground" mode - accepts dynamic parameters from UI

Behavior:
- User can tune ATR filters, Risk %, TP/SL
- Parameters override strategy defaults
- No database lookups - purely in-memory
"""
from typing import Dict, Any
from datetime import datetime
import pandas as pd

from ...strategies import ORBStrategy
from ...data_repository import DataRepository
from .core import BacktestCore
from .schemas import BacktestConfig, StrategyParams, BacktestResult


class AnalystBacktestRunner:
    """
    Analyst Mode Backtest Runner
    Accepts dynamic parameters for flexible experimentation
    """
    
    def __init__(self, db_session):
        self.db = db_session
        self.repo = DataRepository(db_session)
    
    async def run(
        self,
        strategy_name: str,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str,
        initial_capital: float,
        params: Dict[str, Any]
    ) -> BacktestResult:
        """
        Execute backtest with user-provided parameters
        
        Args:
            strategy_name: Strategy to use (e.g., 'ORB')
            symbol: Trading symbol
            start_date, end_date: Date range
            timeframe: '1D', '5MIN', etc.
            initial_capital: Starting

 capital
            params: Strategy-specific parameters (dynamic)
            
        Returns:
            BacktestResult
        """
        # 1. Fetch data
        data = await self._fetch_data(symbol, start_date, end_date, timeframe)
        
        # 2. Initialize strategy with DYNAMIC params
        strategy = self._load_strategy(strategy_name, params)
        
        # 3. Create config
        config = BacktestConfig(
            initial_capital=initial_capital,
            commission_pct=0.03,
            slippage_pct=0.05,
            max_positions=1,
            force_intraday_exit=True # Analyst mode is strictly intraday/playground
        )
        
        # 4. Execute via core engine
        core = BacktestCore(strategy, config)
        result = core.execute(data, symbol, start_date, end_date)
        
        return result
    
    def _load_strategy(self, name: str, params: Dict[str, Any]):
        """Load strategy and apply user params"""
        if name == 'ORB':
            strategy = ORBStrategy()
            
            # Override defaults with user params
            if 'stopLoss' in params:
                strategy.params['stopLoss'] = params['stopLoss']
            if 'takeProfit' in params:
                strategy.params['takeProfit'] = params['takeProfit']
            if 'riskPerTrade' in params:
                strategy.params['riskPerTrade'] = params['riskPerTrade']
            
            return strategy
        
        raise ValueError(f"Unknown strategy: {name}")
    
    async def _fetch_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str
    ) -> pd.DataFrame:
        """Fetch market data for backtesting"""
        # Intraday
        if timeframe in ['1MIN', '5MIN', '15MIN', '30MIN', '60MIN']:
            timeframe_map = {
                '1MIN': 1, '5MIN': 5, '15MIN': 15, '30MIN': 30, '60MIN': 60
            }
            tf_minutes = timeframe_map[timeframe]
            
            data = self.repo.get_intraday_candles(
                symbol=symbol,
                timeframe=tf_minutes,
                start_date=start_date,
                end_date=end_date
            )
            
            if data is None or data.empty:
                # Fallback to daily
                days = (end_date - start_date).days + 30
                data = self.repo.get_historical_prices(symbol, days=days)
        
        # Daily
        else:
            days = (end_date - start_date).days + 30
            data = self.repo.get_historical_prices(symbol, days=days)
        
        if data is None or data.empty:
            raise ValueError(f"No data found for {symbol}")
        
        return data
