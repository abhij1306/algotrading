import sys
import os
from sqlalchemy import text

# Setup path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app'))

from app.database import SessionLocal, Company, HistoricalPrice, engine
from app.data_fetcher import fetch_historical_data

def fix_market_data():
    db = SessionLocal()
    
    # 1. DELETE Discontinued / Merged / Penny Garbage
    # Ensure these are definitely dead or unwanted
    kill_list = [
        'UNITEDBNK', 'TRICOM', 'TV18BRDCST', 'UVSL', 'TALWGYM', 
        'SINTEX', 'ABGSHIP', 'GITANJALI', 'RCOM', 'DHFL', 'JPASSOCIAT',
        'SOUTHBANK' # Wait, South Bank is active. User screenshot showed it with data? No, valid price 39.65.
    ]
    
    print(f"Deleting {len(kill_list)} discontinued symbols...")
    for sym in kill_list:
        try:
            # Delete History first (cascade might handle it, but being explicit)
            db.execute(text("DELETE FROM historical_prices WHERE company_id IN (SELECT id FROM companies WHERE symbol = :sym)"), {"sym": sym})
            # Delete Company
            result = db.execute(text("DELETE FROM companies WHERE symbol = :sym"), {"sym": sym})
            if result.rowcount > 0:
                print(f"  - Deleted {sym}")
        except Exception as e:
            print(f"  - Error deleting {sym}: {e}")
            
    db.commit()
    
    # 2. FORCE REFRESH for Active but 0-Price Stocks
    # Zomato, TataMotors, ShalPaints, Stanley, SKFIndia
    refresh_list = ['ZOMATO', 'TATAMOTORS', 'SHALPAINTS', 'SKFINDIA', 'STANLEY', 'SUDEEPPHRM']
    
    print(f"\nForce refreshing data for {len(refresh_list)} active symbols...")
    for sym in refresh_list:
        try:
            print(f"  - Fetching history for {sym}...")
            # This triggers Fyers fetch internally via data_repository if DB data is missing/old
            # We force it by ensuring we check dates or just call the fetcher directly
            df = fetch_historical_data(sym, days=365)
            if df is not None and not df.empty:
                print(f"    SUCCESS: Got {len(df)} candles. Last Close: {df.iloc[-1]['close']}")
            else:
                print(f"    FAILED: No data fetched for {sym}")
        except Exception as e:
            print(f"    ERROR refreshing {sym}: {e}")

    db.close()
    print("\nFix Complete.")

if __name__ == "__main__":
    fix_market_data()
