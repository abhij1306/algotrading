import sys
import os
from datetime import date, datetime
import logging

# Fix Path to include backend
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend'))

from app.database import SessionLocal, engine, Base
from app.engines.backtest.quant_wrapper import QuantBacktestRunner
from app.engines.strategies.nifty_strategies import NiftyVolContraction

# Config Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_engine():
    print("--- Starting Engine Debug ---")
    db = SessionLocal()
    
    try:
        # 1. Initialize Runner
        runner = QuantBacktestRunner(db)
        print("1. Runner Initialized")
        
        # 2. Test Data Fetch
        print("2. Testing Data Provider...")
        test_symbol = "NIFTY 50" 
        # Check if data_loader is the attribute
        provider = getattr(runner, 'data_provider', getattr(runner, 'data_loader', None))
        if not provider:
             raise Exception("Could not find data provider on runner")
             
        df = provider.get_history(test_symbol, "1D", date(2023, 1, 1), 10)
        print(f"   Data Shape: {df.shape}")
        
        # 3. Test Strategy Logic
        print("3. Testing Strategy Logic (NiftyVolContraction)...")
        # Mock strategy instantiation
        strategy = NiftyVolContraction(db, universe_id="NIFTY_50", parameters={})
        test_date = date(2024, 6, 1) 
        res = strategy.run_day(test_date, ["NIFTY 50"], provider)
        print(f"   Strategy Result: {res}")
        
        # 4. Test Full Run
        print("4. Testing Full Backtest Run...")
        run_id = f"DEBUG_RUN_{datetime.now().strftime('%H%M%S')}"
        
        result = runner.run_strategy_backtest(
            strategy_id="NIFTY_VOL_CONTRACTION",
            universe_id="NIFTY_50", 
            start_date=date(2023, 1, 1),
            end_date=date(2023, 6, 1), 
            run_id=run_id
        )
        
        metrics = result['metrics']
        print(f"   Run Complete. Metrics: {metrics}")
        
    except Exception as e:
        print(f"!!! EXCEPTION DURING DEBUG !!!")
        print(e)
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    test_engine()
