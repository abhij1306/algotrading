"""
Daily Data Update Script
Fetches yesterday's data from Fyers and updates NSE data files
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from datetime import datetime, timedelta
import pandas as pd
from pathlib import Path

# Import Fyers client
from fyers.fyers_client import get_historical_data

# Import NSE data components
from backend.app.data_repository import DataRepository
from backend.app.database import SessionLocal

print("=" * 60)
print("Daily Market Data Update")
print("=" * 60)

# Get yesterday's date
yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
print(f"\nTarget Date: {yesterday}")

# Initialize database session
db = SessionLocal()
repo = DataRepository(db)

# ============================================================
# 1. Update Nifty 50 Index Data
# ============================================================
print("\n1. Updating Nifty 50 Index Data...")

try:
    # Fetch Nifty 50 data from Fyers
    nifty_symbol = "NSE:NIFTY50-INDEX"
    
    print(f"   Fetching {nifty_symbol} data for {yesterday}...")
    nifty_response = get_historical_data(
        symbol=nifty_symbol,
        timeframe="D",
        range_from=yesterday,
        range_to=yesterday
    )
    
    if nifty_response and nifty_response.get('s') == 'ok' and 'candles' in nifty_response:
        candles = nifty_response['candles']
        
        if candles:
            # Prepare data for NSE format
            nifty_df = pd.DataFrame({
                'trade_date': [pd.to_datetime(candle[0], unit='s') for candle in candles],
                'index_name': 'nifty50',
                'open': [candle[1] for candle in candles],
                'high': [candle[2] for candle in candles],
                'low': [candle[3] for candle in candles],
                'close': [candle[4] for candle in candles],
                'volume': [candle[5] for candle in candles],
                'turnover': [0 for _ in candles]  # Not available from Fyers
            })
            
            # Load existing index data
            index_file = Path("nse_data/processed/indices_clean/index_ohlcv.parquet")
            if index_file.exists():
                existing = pd.read_parquet(index_file)
                
                # Remove yesterday's data if it exists (to avoid duplicates)
                existing = existing[pd.to_datetime(existing['trade_date']).dt.date != pd.to_datetime(yesterday).date()]
                
                # Append new data
                updated = pd.concat([existing, nifty_df], ignore_index=True)
                updated = updated.sort_values('trade_date')
                
                # Save back
                updated.to_parquet(index_file, engine='pyarrow', compression='snappy', index=False)
                print(f"   ✅ Updated Nifty 50 data: {len(nifty_df)} records")
            else:
                print("   ⚠️  Index file not found, skipping")
        else:
            print("   ⚠️  No candles data in response")
    else:
        print(f"   ⚠️  No Nifty 50 data available: {nifty_response.get('message', 'Unknown error')}")
        
except Exception as e:
    print(f"   ✗ Error updating Nifty 50: {e}")

# ============================================================
# 2. Update Equity Data (Top 50 stocks)
# ============================================================
print("\n2. Updating Equity Data...")

try:
    # Get list of active F&O stocks from database
    from backend.app.database import Company
    
    companies = db.query(Company).filter(
        Company.is_fno == True,
        Company.is_active == True
    ).limit(50).all()
    
    print(f"   Fetching data for {len(companies)} stocks...")
    
    updated_count = 0
    failed_count = 0
    
    for company in companies:
        try:
            # Fetch data from Fyers
            fyers_symbol = f"NSE:{company.symbol}-EQ"
            
            response = get_historical_data(
                symbol=fyers_symbol,
                timeframe="D",
                range_from=yesterday,
                range_to=yesterday
            )
            
            if response and response.get('s') == 'ok' and 'candles' in response:
                candles = response['candles']
                
                if candles:
                    # Convert to DataFrame format expected by save_historical_prices
                    data = pd.DataFrame({
                        'Open': [candle[1] for candle in candles],
                        'High': [candle[2] for candle in candles],
                        'Low': [candle[3] for candle in candles],
                        'Close': [candle[4] for candle in candles],
                        'Volume': [candle[5] for candle in candles]
                    }, index=[pd.to_datetime(candle[0], unit='s') for candle in candles])
                    
                    # Save to database (Postgres - Warm layer)
                    records = repo.save_historical_prices(company.symbol, data, source='fyers')
                    if records > 0:
                        updated_count += 1
                        print(f"      ✓ {company.symbol}: {records} records")
                else:
                    failed_count += 1
            else:
                failed_count += 1
                
        except Exception as e:
            failed_count += 1
            print(f"      ✗ {company.symbol}: {e}")
    
    print(f"\n   ✅ Updated: {updated_count} stocks")
    print(f"   ⚠️  Failed: {failed_count} stocks")
    
except Exception as e:
    print(f"   ✗ Error updating equity data: {e}")

# ============================================================
# 3. Summary
# ============================================================
print("\n" + "=" * 60)
print("✅ Data Update Complete!")
print("=" * 60)
print(f"\nUpdated data for: {yesterday}")
print("  - Nifty 50 index data updated in NSE files")
print("  - Equity data updated in Postgres (Warm layer)")
print("\nNote: NSE equity Parquet files are for historical data (2016-2024)")
print("Recent data is stored in Postgres and accessed via UnifiedDataService")
print("=" * 60)

# Close database session
db.close()
