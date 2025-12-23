
from abc import ABC, abstractmethod
from datetime import date
from typing import List, Dict, Any
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class BaseStrategy(ABC):
    """
    Abstract Base Class for all strategies.
    Emits daily normalized results.
    """
    
    def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
        self.strategy_id = strategy_id
        self.universe_id = universe_id
        self.params = parameters
        self.regime_tag = parameters.get("regime_tag", "UNKNOWN")

    @abstractmethod
    def run_day(self, current_date: date, symbols: List[str], data_provider: Any) -> Dict[str, Any]:
        """
        Executes strategy logic for a single day.
        Returns a result dictionary following the Strategy Output Contract.
        """
        pass

    def get_standard_result(self, current_date: date, daily_return: float = 0.0, 
                            gross_pnl: float = 0.0, capital: float = 0.0, 
                            trades: int = 0, max_dd: float = 0.0, win_rate: float = 0.0):
        """
        Helper to format the output contract.
        """
        return {
            "date": current_date.isoformat(),
            "strategy_id": self.strategy_id,
            "universe_id": self.universe_id,
            "daily_return": float(daily_return),
            "gross_pnl": float(gross_pnl),
            "capital_allocated": float(capital),
            "number_of_trades": int(trades),
            "max_intraday_drawdown": float(max_dd),
            "win_rate": float(win_rate),
            "regime_tag": self.regime_tag
        }
