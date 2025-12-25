
from ...engines.base_strategy import BaseStrategy
from ...engines.data_provider import DataProvider
from datetime import date, time, datetime
from typing import List, Dict, Any
import pandas as pd
import numpy as np

class IntradayMeanReversionStrategy(BaseStrategy):
    """
    Intraday Mean Reversion
    - Universe: NIFTY100_MEAN_REV
    - Timeframe: 5-minute
    - Entry: VWAP deviation + RSI overshoot
    - Exit: Target VWAP or EOD
    """
    
    def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
        super().__init__(strategy_id, universe_id, parameters)
        self.vwap_dev_threshold = parameters.get("vwap_deviation_threshold", 0.02) # 2% deviation
        self.rsi_threshold = parameters.get("rsi_threshold", 30) # Oversold for long
        self.max_positions = parameters.get("max_positions", 5)
        self.regime_tag = "RANGE"

    def calculate_vwap(self, df: pd.DataFrame):
        v = df['volume'].values
        p = df['close'].values
        return (p * v).cumsum() / v.cumsum()

    def calculate_rsi(self, df: pd.DataFrame, period=14):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    def run_day(self, current_date: date, symbols: List[str], db_session: Any) -> Dict[str, Any]:
        provider = DataProvider(db_session)
        data = provider.get_intraday_data(symbols, current_date)
        
        if not data:
            return self.get_standard_result(current_date)

        total_pnl = 0.0
        trades_count = 0
        active_positions = {}
        
        # Pre-process indicators for the day
        indicators = {}
        for symbol, df in data.items():
            if len(df) < 20: continue
            df = df.copy()
            df['vwap'] = self.calculate_vwap(df)
            df['rsi'] = self.calculate_rsi(df)
            indicators[symbol] = df

        all_timestamps = sorted(list(set([ts for df in indicators.values() for ts in df.index])))
        eod_exit_time = datetime.combine(current_date, time(15, 15))

        for ts in all_timestamps:
            if ts >= eod_exit_time:
                for symbol, pos in list(active_positions.items()):
                    exit_price = indicators[symbol].loc[ts, "close"] if ts in indicators[symbol].index else indicators[symbol].iloc[-1]["close"]
                    total_pnl += (exit_price - pos["entry_price"]) * pos["size"]
                    trades_count += 1
                    del active_positions[symbol]
                break

            # 1. Exit positions at VWAP (Mean Reversion)
            for symbol, pos in list(active_positions.items()):
                if ts in indicators[symbol].index:
                    curr = indicators[symbol].loc[ts]
                    # Long exit if price crosses back to VWAP
                    if curr["close"] >= curr["vwap"]:
                        total_pnl += (curr["close"] - pos["entry_price"]) * pos["size"]
                        trades_count += 1
                        del active_positions[symbol]

            # 2. Search for entries
            if len(active_positions) < self.max_positions:
                for symbol, df in indicators.items():
                    if symbol in active_positions or ts not in df.index:
                        continue
                        
                    curr = df.loc[ts]
                    # Mean Reversion Long: Price below VWAP by threshold + RSI oversold
                    dev = (curr["vwap"] - curr["close"]) / curr["vwap"]
                    if dev > self.vwap_dev_threshold and curr["rsi"] < self.rsi_threshold:
                        entry_price = curr["close"]
                        # Fixed notional sizing for simplicity
                        size = floor(200000 / entry_price) 
                        active_positions[symbol] = {
                            "entry_price": entry_price,
                            "size": size
                        }

        daily_return = (total_pnl / 1000000.0) if total_pnl != 0 else 0.0
        return self.get_standard_result(current_date, daily_return=daily_return, gross_pnl=total_pnl, trades=trades_count)

def floor(val):
    import math
    return math.floor(val)
