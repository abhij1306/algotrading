
from app.engines.base_strategy import BaseStrategy
from app.engines.data_provider import DataProvider
from datetime import date, time, datetime
from typing import List, Dict, Any
import pandas as pd
import numpy as np

class IntradayMomentumStrategy(BaseStrategy):
    """
    Intraday Momentum (Trend Expansion)
    - Universe: NIFTY100_LIQUID_50
    - Timeframe: 5-minute
    - Entry: Opening range breakout with volume expansion
    - Exit: ATR-based stop or EOD
    """
    
    def __init__(self, strategy_id: str, universe_id: str, parameters: Dict[str, Any]):
        super().__init__(strategy_id, universe_id, parameters)
        self.opening_range_mins = parameters.get("opening_range_duration", 15)
        self.atr_lookback = parameters.get("atr_lookback", 14)
        self.vol_expansion_threshold = parameters.get("volume_expansion_threshold", 2.0)
        self.max_positions = parameters.get("max_positions", 5)
        self.risk_per_trade = parameters.get("risk_per_trade", 0.01) # 1% of daily capital
        self.regime_tag = "TREND"

    def run_day(self, current_date: date, symbols: List[str], db_session: Any) -> Dict[str, Any]:
        provider = DataProvider(db_session)
        data = provider.get_intraday_data(symbols, current_date)
        
        if not data:
            return self.get_standard_result(current_date)

        total_pnl = 0.0
        trades_count = 0
        active_positions = {} # symbol: {entry_price, size, stop_loss}
        
        # Combine all timestamps into a sorted unique list
        all_timestamps = sorted(list(set([ts for df in data.values() for ts in df.index])))
        
        # Define market hours
        market_open = datetime.combine(current_date, time(9, 15))
        opening_range_end = market_open + pd.Timedelta(minutes=self.opening_range_mins)
        eod_exit_time = datetime.combine(current_date, time(15, 15))
        
        # Pre-calculate opening ranges
        opening_ranges = {}
        for symbol, df in data.items():
            range_df = df[df.index <= opening_range_end]
            if not range_df.empty:
                opening_ranges[symbol] = {
                    "high": range_df["high"].max(),
                    "low": range_df["low"].min(),
                    "avg_vol": range_df["volume"].mean()
                }

        # Step through time
        for ts in all_timestamps:
            if ts <= opening_range_end:
                continue
            
            # 1. Exit at EOD
            if ts >= eod_exit_time:
                for symbol, pos in list(active_positions.items()):
                    exit_price = data[symbol].loc[ts, "close"] if ts in data[symbol].index else data[symbol].iloc[-1]["close"]
                    pnl = (exit_price - pos["entry_price"]) * pos["size"]
                    total_pnl += pnl
                    trades_count += 1
                    del active_positions[symbol]
                break

            # 2. Check current positions for Stop Loss
            for symbol, pos in list(active_positions.items()):
                if ts in data[symbol].index:
                    current_low = data[symbol].loc[ts, "low"]
                    if current_low <= pos["stop_loss"]:
                        # Stopped out
                        pnl = (pos["stop_loss"] - pos["entry_price"]) * pos["size"]
                        total_pnl += pnl
                        trades_count += 1
                        del active_positions[symbol]

            # 3. Check for new entries
            if len(active_positions) < self.max_positions:
                for symbol, df in data.items():
                    if symbol in active_positions or symbol not in opening_ranges:
                        continue
                    
                    if ts not in df.index:
                        continue
                        
                    curr_bar = df.loc[ts]
                    op_range = opening_ranges[symbol]
                    
                    # Long entry condition: Breakout above opening range high + volume
                    if curr_bar["close"] > op_range["high"] and curr_bar["volume"] > op_range["avg_vol"] * self.vol_expansion_threshold:
                        # Entry!
                        # Position sizing: Risk 1% of typical capital (e.g. 1M)
                        # Stop loss at opening range low
                        entry_price = curr_bar["close"]
                        stop_loss = op_range["low"]
                        risk_amount = stop_loss - entry_price # Negative for long
                        
                        if abs(risk_amount) > 0:
                            # Risk 1% of 1,000,000 = 10,000
                            size = floor(10000 / abs(risk_amount))
                            active_positions[symbol] = {
                                "entry_price": entry_price,
                                "size": size,
                                "stop_loss": stop_loss
                            }

        # Return daily summary
        daily_return = (total_pnl / 1000000.0) if total_pnl != 0 else 0.0
        return self.get_standard_result(
            current_date, 
            daily_return=daily_return, 
            gross_pnl=total_pnl, 
            capital=1000000.0, 
            trades=trades_count
        )

# Helper
def floor(val):
    import math
    return math.floor(val)
