"""
Backtest Engine Module
Provides shared core execution engine with separate wrappers for Analyst and Quant modes
"""
from .core import BacktestCore, BacktestResult
from .analyst_wrapper import AnalystBacktestRunner
from .quant_wrapper import QuantBacktestRunner
from .schemas import BacktestConfig, StrategyParams

__all__ = [
    'BacktestCore',
    'BacktestResult',
    'AnalystBacktestRunner',
    'QuantBacktestRunner',
    'BacktestConfig',
    'StrategyParams'
]
