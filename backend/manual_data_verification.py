
import sys
import os
import pandas as pd
from datetime import datetime, timedelta

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from app.database import SessionLocal, Company, HistoricalPrice
from app.data_repository import DataRepository

def test_data_fetching():
    db = SessionLocal()
    repo = DataRepository(db)
    
    # Symbols to test (Common ones + potential ones user added)
    test_symbols = ['RELIANCE', 'TATASTEEL', 'INFY', 'HDFCBANK', 'RELIGARE', 'SBIN']
    
    print("--- Testing Data Repository ---")
    
    for symbol in test_symbols:
        print(f"\nChecking {symbol}:")
        
        # 1. Check Company Table
        company = db.query(Company).filter(Company.symbol == symbol).first()
        if not company:
            print(f"❌ Company check: Not found in DB")
            continue
        print(f"✅ Company check: Found (ID: {company.id})")
        
        # 2. Check get_historical_prices
        try:
            hist = repo.get_historical_prices(symbol, days=365)
            if hist is None:
                 print(f"❌ get_historical_prices: Returned None")
            elif hist.empty:
                 print(f"❌ get_historical_prices: Returned Empty DataFrame")
            else:
                 print(f"✅ get_historical_prices: Found {len(hist)} records")
                 print(f"   Latest: {hist.index[-1]}")
                 print(f"   Columns: {hist.columns.tolist()}")
        except Exception as e:
            print(f"❌ get_historical_prices: Exception - {e}")

if __name__ == "__main__":
    test_data_fetching()
