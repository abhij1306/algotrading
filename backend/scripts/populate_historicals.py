
import sys
import os
import json
import argparse
from datetime import datetime
import pandas as pd

# Ensure backend directory is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from backend.app.database import SessionLocal, init_db
from backend.app.data_repository import DataRepository
from backend.app.data_fetcher import fetch_historical_data
from backend.app.config import config

def populate_historicals(limit=None, loop_delay=0.5):
    """
    Populate historical data for NSE companies.
    
    Args:
        limit: Max number of companies to process
        loop_delay: Time to sleep between requests (not strictly needed with Fyers/yfinance but good for politeness)
    """
    import time
    
    # Load company list
    companies_file = os.path.join(os.path.dirname(__file__), '../data/nse_companies.json')
    if not os.path.exists(companies_file):
        print(f"Error: {companies_file} not found. Run fetch_nse_symbols.py first.")
        return

    with open(companies_file, 'r') as f:
        symbols = json.load(f)
        
    if limit:
        symbols = symbols[:int(limit)]
        
    print(f"Starting historical data population for {len(symbols)} companies...")
    print(f"Data Source: Fyers")
    
    # Initialize DB
    init_db()
    db = SessionLocal()
    repo = DataRepository(db)
    
    success_count = 0
    failed_count = 0
    skipped_count = 0 # If data is current
    
    try:
        for i, symbol in enumerate(symbols):
            try:
                print(f"[{i+1}/{len(symbols)}] Processing {symbol}...", end='', flush=True)
                
                # 1. Ensure Company Exists
                print(f" Checking/Creating company...", end='')
                company = repo.get_or_create_company(symbol, is_active=True)
                print(" Done.", end='')
                
                # Check current status in DB to avoid unnecessary re-fetch if recently updated
                # (fetch_historical_data already does this logic, but good to be aware)
                
                # 2. Fetch Data (this handles DB check, Fyers)
                # Requesting 365 days by default as per requirement
                df = fetch_historical_data(symbol, days=365)
                
                if df is not None and not df.empty:
                    # fetch_historical_data implicitly saves to DB if it fetched new data
                    # But we should double check if we need to explicitly save or if fetcher did it.
                    # Looking at fetch_historical_data implementation in data_fetcher.py:
                    # - It CALLS repo.save_historical_prices internally when it fetches from Fyers/yfinance.
                    # - It returns the DF.
                    
                    print(f" ✅ (Records: {len(df)})")
                    success_count += 1
                else:
                    print(f" ❌ No Data")
                    failed_count += 1
                    
                # Sleep briefly
                # time.sleep(loop_delay) 
                
            except KeyboardInterrupt:
                print("\nOperation cancelled by user.")
                break
            except Exception as e:
                db.rollback()
                print(f" ❌ Error: {str(e)[:100]}")
                failed_count += 1
                
    finally:
        db.close()
        print("\n" + "="*50)
        print(f"Completed Processing {len(symbols)} companies")
        print(f"Success: {success_count}")
        print(f"Failed: {failed_count}")
        print("="*50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Populate historical data for NSE companies')
    parser.add_argument('--limit', type=int, help='Limit number of companies to process')
    args = parser.parse_args()
    
    populate_historicals(limit=args.limit)
