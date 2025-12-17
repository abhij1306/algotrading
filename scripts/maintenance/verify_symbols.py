"""
Verify Symbol Metadata and Database Consistency
Ensures all NSE metadata symbols are in the database
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

import pandas as pd
from pathlib import Path
from backend.app.database import SessionLocal, Company

print("=" * 60)
print("Symbol Metadata Verification")
print("=" * 60)

# Load NSE metadata
metadata_file = Path("nse_data/metadata/equity_list.csv")
if not metadata_file.exists():
    print("❌ Metadata file not found!")
    exit(1)

metadata_df = pd.read_csv(metadata_file)
print(f"\n✅ Loaded NSE metadata: {len(metadata_df)} symbols")

# Get database symbols
db = SessionLocal()
db_companies = db.query(Company).all()
db_symbols = {c.symbol for c in db_companies}
print(f"✅ Database has: {len(db_symbols)} symbols")

# Compare
metadata_symbols = set(metadata_df['SYMBOL'].str.upper())
print(f"\n📊 Comparison:")
print(f"   NSE Metadata: {len(metadata_symbols)} symbols")
print(f"   Database: {len(db_symbols)} symbols")

# Find differences
in_metadata_not_db = metadata_symbols - db_symbols
in_db_not_metadata = db_symbols - metadata_symbols

if in_metadata_not_db:
    print(f"\n⚠️  Symbols in NSE metadata but NOT in database: {len(in_metadata_not_db)}")
    print(f"   First 20: {sorted(list(in_metadata_not_db))[:20]}")
else:
    print(f"\n✅ All NSE metadata symbols are in database!")

if in_db_not_metadata:
    print(f"\n📝 Symbols in database but NOT in NSE metadata: {len(in_db_not_metadata)}")
    print(f"   First 20: {sorted(list(in_db_not_metadata))[:20]}")

# Check active F&O stocks
fno_stocks = db.query(Company).filter(
    Company.is_fno == True,
    Company.is_active == True
).all()
print(f"\n📈 Active F&O stocks in database: {len(fno_stocks)}")
print(f"   Sample: {[s.symbol for s in fno_stocks[:10]]}")

# Check if F&O stocks have historical data
from backend.app.database import HistoricalPrice
from sqlalchemy import func

stocks_with_data = db.query(Company.symbol).join(
    HistoricalPrice, Company.id == HistoricalPrice.company_id
).filter(
    Company.is_fno == True,
    Company.is_active == True
).distinct().all()

print(f"\n📊 F&O stocks with historical data: {len(stocks_with_data)}")

stocks_without_data = set([s.symbol for s in fno_stocks]) - set([s[0] for s in stocks_with_data])
if stocks_without_data:
    print(f"⚠️  F&O stocks WITHOUT historical data: {len(stocks_without_data)}")
    print(f"   Sample: {sorted(list(stocks_without_data))[:10]}")
else:
    print(f"✅ All F&O stocks have historical data!")

# Summary
print("\n" + "=" * 60)
print("✅ Verification Complete!")
print("=" * 60)
print(f"\nSummary:")
print(f"  - NSE Metadata: {len(metadata_symbols)} symbols")
print(f"  - Database: {len(db_symbols)} symbols")
print(f"  - Active F&O: {len(fno_stocks)} stocks")
print(f"  - With Historical Data: {len(stocks_with_data)} stocks")

if in_metadata_not_db:
    print(f"\n⚠️  Action Required: {len(in_metadata_not_db)} symbols from NSE metadata need to be added to database")
else:
    print(f"\n✅ Database is in sync with NSE metadata!")

print("=" * 60)

db.close()
