
import sys
import os
import pandas as pd
from datetime import datetime
import logging

# Setup path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from app.database import SessionLocal
from app.data_repository import DataRepository
from app.strategies.orb_strategy import ORBStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_backtest():
    db = SessionLocal()
    repo = DataRepository(db)
    
    symbol = "INFY"
    start_date = datetime(2024, 1, 1)
    end_date = datetime(2024, 1, 10)
    
    print(f"--- Debugging Backtest for {symbol} ---")
    
    # 1. Test Data Fetching
    print("\n1. Fetching Data...")
    try:
        data = repo.get_intraday_candles(
            symbol=symbol,
            timeframe=5,
            start_date=start_date,
            end_date=end_date
        )
        
        if data is None or data.empty:
            print("❌ No Intraday Data Found. Trying Daily...")
            data = repo.get_historical_prices(symbol, days=30)
            if data is None or data.empty:
                 print("❌ No Daily Data Found either.")
                 return
            print(f"✅ Found Daily Data: {len(data)} rows")
            # Mock timestamp for daily data to work with strategy
            if 'timestamp' not in data.columns:
                 data['timestamp'] = pd.to_datetime(data.index)
        else:
            print(f"✅ Found Intraday Data: {len(data)} rows")
            
    except Exception as e:
        print(f"❌ Data Fetch Error: {e}")
        return

    # 2. Test Strategy Logic
    print("\n2. Running Strategy...")
    params = {
        "symbol": symbol,
        "opening_range_minutes": 15,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 2.0,
        "max_positions_per_day": 1,
        "trade_type": "equity", # Simple equity for test
        "days_to_expiry": 7
    }
    
    strategy = ORBStrategy(params)
    signals = []
    
    # Simulate feed
    # Strategy expects current_data (1 row) and historical_data (N rows)
    # We'll simulate a simple loop
    
    window_size = 50
    for i in range(window_size, len(data)):
        current_row = data.iloc[i:i+1]
        historical_data = data.iloc[i-window_size:i]
        
        try:
            signal = strategy.on_data(current_row, historical_data)
            if signal:
                print(f"✅ Signal Generated: {signal}")
                signals.append(signal)
        except Exception as e:
            print(f"❌ Strategy Error at index {i}: {e}")
            break
            
    print(f"\nTotal Signals: {len(signals)}")
    if len(signals) == 0:
        print("⚠️ No signals generated. Check strategy logic parameters (Opening Range, etc).")

if __name__ == "__main__":
    debug_backtest()
