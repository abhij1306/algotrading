
from ...engines.base_strategy import BaseStrategy
from ...engines.data_provider import DataProvider
from datetime import date, timedelta
from typing import List, Dict, Any
import pandas as pd
import math

class OvernightGapReversionStrategy(BaseStrategy):
    """
    Overnight Gap Reversion (Stocks)
    - Universe: NIFTY50_ONLY
    - Timeframe: Daily
    - Entry: Large gap at open relative to prior close
    - Exit: Target profit, Stop loss, or Max holding days
    """
    
    def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
        super().__init__(strategy_id, universe_id, parameters)
        self.gap_threshold = parameters.get("gap_size_threshold", 0.02) # 2% gap
        self.max_holding_days = parameters.get("max_holding_days", 3)
        self.max_positions = parameters.get("max_positions", 10)
        self.regime_tag = "EVENT"
        
        # State across days
        self.active_positions = {} # symbol: {entry_date, entry_price, size, days_held}

    def run_day(self, current_date: date, symbols: List[str], db_session: Any) -> Dict[str, Any]:
        provider = DataProvider(db_session)
        
        # We need today's open/close and yesterday's close
        # Fetch 5 days of data to be safe for weekends/holidays
        start_fetch = current_date - timedelta(days=5)
        data = provider.get_daily_data(symbols, start_fetch, current_date)
        
        # Validate we got data
        if not data:
            # No data available, return zero result
            return self.get_standard_result(current_date, daily_return=0.0, gross_pnl=0.0, trades=0)
        
        total_pnl = 0.0
        trades_count = 0
        
        # 1. Update existing positions
        for symbol, pos in list(self.active_positions.items()):
            # Check if we have data for this symbol
            if symbol not in data:
                continue
            
            df = data[symbol]
            # Check if DataFrame is empty or doesn't have current date
            if df.empty or current_date not in df.index:
                continue
                
            curr = df.loc[current_date]
            
            # Validate we have close price
            if pd.isna(curr["close"]) or curr["close"] <= 0:
                continue
                
            pos["days_held"] += 1
            
            # Check for exit (Max holding days or simple stop/target)
            # For this simple implementation, we exit at Close on the max holding day
            if pos["days_held"] >= self.max_holding_days:
                exit_price = curr["close"]
                pnl = (exit_price - pos["entry_price"]) * pos["size"]
                total_pnl += pnl
                trades_count += 1
                del self.active_positions[symbol]

        # 2. Search for new entries
        if len(self.active_positions) < self.max_positions:
            for symbol, df in data.items():
                if symbol in self.active_positions:
                    continue
                
                # Validate DataFrame
                if df.empty or current_date not in df.index:
                    continue
                
                # Get index of current_date
                idx_list = list(df.index)
                try:
                    curr_idx = idx_list.index(current_date)
                except ValueError:
                    continue
                
                if curr_idx < 1:
                    continue # Need at least one prior day
                    
                today = df.iloc[curr_idx]
                prev = df.iloc[curr_idx - 1]
                
                # Validate we have required prices
                if pd.isna(today["open"]) or pd.isna(prev["close"]) or prev["close"] <= 0:
                    continue
                
                # Calculation: Gap at open
                try:
                    gap = (today["open"] - prev["close"]) / prev["close"]
                except (ZeroDivisionError, TypeError):
                    continue
                
                # Gap Reversion logic: 
                # If gap is up > 2%, short it (expecting fall)
                # If gap is down > 2%, buy it (expecting bounce)
                # Here we implement LONG only (down gap reversion)
                if gap < -self.gap_threshold:
                    entry_price = today["open"]
                    
                    # Validate entry price is positive
                    if entry_price <= 0:
                        continue
                    
                    # Sizing: 100k per position
                    try:
                        size = math.floor(100000 / entry_price)
                        if size <= 0:
                            continue
                    except (ZeroDivisionError, ValueError):
                        continue
                        
                    self.active_positions[symbol] = {
                        "entry_date": current_date,
                        "entry_price": entry_price,
                        "size": size,
                        "days_held": 0
                    }

        daily_return = (total_pnl / 1000000.0) if total_pnl != 0 else 0.0
        return self.get_standard_result(current_date, daily_return=daily_return, gross_pnl=total_pnl, trades=trades_count)
