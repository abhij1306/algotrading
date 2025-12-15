
import sys
import os
import time


# Add project root to path (c:\AlgoTrading)
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(project_root)

from app.database import SessionLocal
from app.data_repository import DataRepository
from app.data_fetcher import fetch_historical_data

def update_all_data():
    print("Starting bulk data update...")
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        companies = repo.get_all_companies()
        total = len(companies)
        print(f"Found {total} companies in database.")
        
        updated_count = 0
        failed_count = 0
        
        for i, company in enumerate(companies):
            symbol = company.symbol
            print(f"[{i+1}/{total}] Checking {symbol}...")
            
            try:
                # This function handles: Check DB -> If old/missing -> Fetch Fyers -> Save DB
                # It also has the YFinance fallback I added
                df = fetch_historical_data(symbol, days=365)
                
                if df is not None and not df.empty:
                    updated_count += 1
                else:
                    print(f"  Warning: No data found for {symbol}")
                    failed_count += 1
            except Exception as e:
                print(f"  Error updating {symbol}: {e}")
                failed_count += 1
                
            # Rate limiting to be safe
            time.sleep(0.1)
            
        print("\nUpdate Complete!")
        print(f"Updated: {updated_count}")
        print(f"Failed/No Data: {failed_count}")
        
    finally:
        db.close()

if __name__ == "__main__":
    update_all_data()
