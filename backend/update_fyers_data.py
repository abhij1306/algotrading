"""
Script to update historical data from Fyers API
Fetches latest data for all active companies and updates the database
"""
import sys
import os
from datetime import datetime, timedelta

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, Company
from app.data_repository import DataRepository
from app.data_fetcher import fetch_historical_data

def update_all_stocks():
    """Update historical data for all active stocks"""
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        # Get all active companies
        companies = db.query(Company).filter(Company.is_active == True).all()
        
        print(f"Found {len(companies)} active companies")
        print("Starting data update from Fyers...")
        
        success_count = 0
        error_count = 0
        
        for i, company in enumerate(companies, 1):
            try:
                print(f"\n[{i}/{len(companies)}] Updating {company.symbol}...", end=" ")
                
                # Fetch last 365 days of data
                df = fetch_historical_data(company.symbol, days=365)
                
                if df is not None and not df.empty:
                    # Save to database
                    records_added = repo.save_historical_prices(company.symbol, df, source='fyers')
                    print(f"✓ Added {records_added} new records")
                    success_count += 1
                else:
                    print("✗ No data received")
                    error_count += 1
                    
            except Exception as e:
                print(f"✗ Error: {str(e)}")
                error_count += 1
                continue
        
        print(f"\n{'='*60}")
        print(f"Update complete!")
        print(f"Success: {success_count} | Errors: {error_count}")
        print(f"{'='*60}")
        
    finally:
        db.close()

def update_specific_symbols(symbols: list):
    """Update historical data for specific symbols"""
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        print(f"Updating {len(symbols)} symbols from Fyers...")
        
        for symbol in symbols:
            try:
                print(f"\nUpdating {symbol}...", end=" ")
                
                # Fetch last 365 days of data
                df = fetch_historical_data(symbol, days=365)
                
                if df is not None and not df.empty:
                    records_added = repo.save_historical_prices(symbol, df, source='fyers')
                    print(f"✓ Added {records_added} new records")
                else:
                    print("✗ No data received")
                    
            except Exception as e:
                print(f"✗ Error: {str(e)}")
                continue
        
        print("\nUpdate complete!")
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Update historical data from Fyers')
    parser.add_argument('--symbols', nargs='+', help='Specific symbols to update (e.g., RELIANCE TCS INFY)')
    parser.add_argument('--all', action='store_true', help='Update all active companies')
    
    args = parser.parse_args()
    
    if args.all:
        update_all_stocks()
    elif args.symbols:
        update_specific_symbols(args.symbols)
    else:
        # Default: update a few test symbols
        print("No arguments provided. Updating test symbols...")
        print("Use --all to update all stocks or --symbols SYMBOL1 SYMBOL2 for specific stocks")
        update_specific_symbols(['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK'])
