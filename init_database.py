"""
Initialize database with F&O universe and populate with historical data
Run this once to build the database
"""
import sys
sys.path.insert(0, '.')

from backend.app.database import SessionLocal, init_db, Company
from backend.app.data_repository import DataRepository
from backend.app.data_fetcher import fetch_fyers_historical
from backend.app.config import config
import json
from pathlib import Path
from datetime import datetime

print("="*80)
print("DATABASE INITIALIZATION")
print("="*80)

# Initialize database
print("\n1. Creating database tables...")
init_db()
print("   ✅ Database tables created")

# Load F&O universe
print("\n2. Loading F&O universe...")
universe_file = Path('backend/data/nse_fno_universe.json')
with open(universe_file, 'r') as f:
    universe = json.load(f)
print(f"   ✅ Loaded {len(universe)} F&O stocks")

# Create database session
db = SessionLocal()
repo = DataRepository(db)

# Add companies to database
print("\n3. Adding companies to database...")
for symbol in universe:
    company = repo.get_or_create_company(symbol, is_fno=True, is_active=True)
print(f"   ✅ Added {len(universe)} companies")

# Ask user if they want to populate historical data
print("\n" + "="*80)
print("HISTORICAL DATA POPULATION")
print("="*80)
print("\nOptions:")
print("1. Skip (test with empty database)")
print("2. Populate 10 stocks (quick test - ~30 seconds)")
print("3. Populate 50 stocks (medium test - ~2 minutes)")
print("4. Populate all 200 stocks (full database - ~10 minutes)")

choice = input("\nEnter choice (1-4): ").strip()

stock_count = 0
if choice == '1':
    print("\n✅ Skipping data population. Database is ready for testing.")
    stock_count = 0
elif choice == '2':
    stock_count = 10
elif choice == '3':
    stock_count = 50
elif choice == '4':
    stock_count = len(universe)
else:
    print("Invalid choice. Exiting.")
    sys.exit(1)

if stock_count > 0:
    print(f"\n4. Populating historical data for {stock_count} stocks...")
    print("   This will fetch 1 year of data from Fyers/yfinance")
    print("   Progress:")
    
    success_count = 0
    failed_count = 0
    
    for i, symbol in enumerate(universe[:stock_count]):
        try:
            # Try Fyers first
            hist = None
            if config.HAS_FYERS:
                hist = fetch_fyers_historical(symbol, days=365)
            
            if hist is not None and not hist.empty:
                # Save to database
                records = repo.save_historical_prices(
                    symbol, 
                    hist, 
                    source='fyers'
                )
                success_count += 1
                print(f"   [{i+1}/{stock_count}] {symbol}: {records} records saved ✅")
            else:
                failed_count += 1
                print(f"   [{i+1}/{stock_count}] {symbol}: No data ❌")
                
        except Exception as e:
            failed_count += 1
            print(f"   [{i+1}/{stock_count}] {symbol}: Error - {str(e)[:50]} ❌")
    
    print(f"\n   ✅ Completed: {success_count} successful, {failed_count} failed")

# Summary
print("\n" + "="*80)
print("DATABASE SUMMARY")
print("="*80)

total_companies = db.query(Company).count()
from backend.app.database import HistoricalPrice
total_prices = db.query(HistoricalPrice).count()

print(f"\nCompanies in database: {total_companies}")
print(f"Historical price records: {total_prices:,}")
print(f"Database location: backend/data/screener.db")

# Check database size
db_path = Path('backend/data/screener.db')
if db_path.exists():
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"Database size: {size_mb:.2f} MB")

print("\n✅ Database is ready!")
print("\nYou can now:")
print("1. Test the screener without API calls")
print("2. View data in the database")
print("3. Add more stocks later by running this script again")

db.close()
