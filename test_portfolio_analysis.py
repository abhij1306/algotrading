import sys
sys.path.insert(0, 'c:/AlgoTrading/backend')

from app.portfolio_risk import PortfolioRiskEngine
from app.data_repository import DataRepository
from app.database import SessionLocal
import pandas as pd
import numpy as np

# Initialize
db = SessionLocal()
repo = DataRepository(db)
engine = PortfolioRiskEngine()

# Get INFY data
print("Fetching INFY historical data...")
hist = repo.get_historical_prices('INFY', days=252)
print(f"Got {len(hist)} days of data")
print(f"Columns: {hist.columns.tolist()}")

# Create prices dataframe
prices_df = pd.DataFrame({'INFY': hist['close']})
print(f"Prices dataframe shape: {prices_df.shape}")

# Create weights
weights = np.array([1.0])

# Create market prices (use INFY itself for simplicity)
market_prices = hist['close']

# Create financials
financials = [{'debt_to_equity': 0, 'roe': 0, 'current_ratio': 1.0, 'free_cash_flow': 0}]

print("\nCalling analyze_portfolio...")
try:
    result = engine.analyze_portfolio(
        prices=prices_df,
        weights=weights,
        market_prices=market_prices,
        financials=financials,
        lookback_days=252
    )
    print("SUCCESS!")
    print(f"Result keys: {result.keys()}")
except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()

db.close()
