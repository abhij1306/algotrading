
from app.engines.base_strategy import BaseStrategy
from app.engines.data_provider import DataProvider
from datetime import date, time, datetime
from typing import List, Dict, Any
import pandas as pd
import numpy as np

class IndexMeanReversionStrategy(BaseStrategy):
    """
    Index Mean Reversion
    - Instrument: NIFTY index OHLC (e.g., symbol 'NSE:NIFTY50')
    - Timeframe: 5-minute
    - Entry: Deviation from VWAP or Value Area
    - Exit: Target VWAP or EOD
    """
    
    def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
        super().__init__(strategy_id, universe_id, parameters)
        self.entry_deviation_pct = parameters.get("entry_deviation", 0.005) # 0.5%
        self.vwap_lookback = parameters.get("vwap_lookback", 100) # bars
        self.regime_tag = "INDEX_RANGE"

    def calculate_vwap(self, df: pd.DataFrame):
        v = df['volume'].values
        p = df['close'].values
        # For index, volume might be missing or placeholder, handle gracefully
        if v.sum() == 0:
            return df['close'].expanding().mean()
        return (p * v).cumsum() / v.cumsum()

    def run_day(self, current_date: date, symbols: List[str], db_session: Any) -> Dict[str, Any]:
        provider = DataProvider(db_session)
        # Typically run on a single index symbol, e.g. ["NSE:NIFTY50"]
        data = provider.get_intraday_data(symbols, current_date)
        
        if not data:
            return self.get_standard_result(current_date)

        symbol = symbols[0] # Assume first symbol is the index
        if symbol not in data:
            return self.get_standard_result(current_date)
            
        df = data[symbol].copy()
        df['vwap'] = self.calculate_vwap(df)
        
        total_pnl = 0.0
        trades_count = 0
        active_position = None # {type, entry_price, size}
        
        eod_exit_time = datetime.combine(current_date, time(15, 15))

        for ts, row in df.iterrows():
            if ts >= eod_exit_time:
                if active_position:
                    exit_price = row["close"]
                    side_mult = 1 if active_position["type"] == "LONG" else -1
                    total_pnl += (exit_price - active_position["entry_price"]) * active_position["size"] * side_mult
                    trades_count += 1
                break

            # 1. Exit at mean
            if active_position:
                if active_position["type"] == "LONG" and row["close"] >= row["vwap"]:
                    total_pnl += (row["close"] - active_position["entry_price"]) * active_position["size"]
                    trades_count += 1
                    active_position = None
                elif active_position["type"] == "SHORT" and row["close"] <= row["vwap"]:
                    total_pnl += (active_position["entry_price"] - row["close"]) * active_position["size"]
                    trades_count += 1
                    active_position = None

            # 2. Entries
            else:
                deviation = (row["close"] - row["vwap"]) / row["vwap"]
                
                # Overshoot below VWAP -> Long
                if deviation < -self.entry_deviation_pct:
                    active_position = {
                        "type": "LONG",
                        "entry_price": row["close"],
                        "size": 50 # 1 lot Nifty (approx)
                    }
                # Overshoot above VWAP -> Short
                elif deviation > self.entry_deviation_pct:
                    active_position = {
                        "type": "SHORT",
                        "entry_price": row["close"],
                        "size": 50
                    }

        daily_return = (total_pnl / 1000000.0) if total_pnl != 0 else 0.0
        return self.get_standard_result(current_date, daily_return=daily_return, gross_pnl=total_pnl, trades=trades_count)
