"""
Download Equity Data from Fyers - For all NSE stocks from 2024 onwards
"""

import sys
import os
from pathlib import Path
import pandas as pd
from datetime import datetime
import time

# Add AlgoTrading root to path
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))

# Import fyers client
sys.path.insert(0, str(algotrading_root / "fyers"))
import fyers_client

from backend.app.database import SessionLocal, Company

# Configuration
START_DATE = "2024-01-01"  # Only recent data
END_DATE = datetime.now().strftime("%Y-%m-%d")
RATE_LIMIT_DELAY = 1.5  # seconds between API calls
MAX_DAYS = 365  # Fyers API limit for daily data

# Output directory
EQUITY_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_equities"
EQUITY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_historical_data(symbol: str, start_date: str, end_date: str):
    """Fetch historical data from Fyers API with chunking for large date ranges"""
    from datetime import datetime, timedelta
    
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        all_data = []
        current_start = start_dt
        chunk_num = 1
        
        while current_start < end_dt:
            current_end = min(current_start + timedelta(days=MAX_DAYS), end_dt)
            
            chunk_start_str = current_start.strftime("%Y-%m-%d")
            chunk_end_str = current_end.strftime("%Y-%m-%d")
            
            # Fetch data for this chunk
            response = fyers_client.get_historical_data(
                symbol=symbol,
                timeframe="D",
                range_from=chunk_start_str,
                range_to=chunk_end_str
            )
            
            if response.get('s') != 'ok' or 'candles' not in response:
                current_start = current_end + timedelta(days=1)
                chunk_num += 1
                time.sleep(RATE_LIMIT_DELAY)
                continue
            
            candles = response['candles']
            if candles:
                all_data.extend(candles)
            
            current_start = current_end + timedelta(days=1)
            chunk_num += 1
            
            if current_start < end_dt:
                time.sleep(RATE_LIMIT_DELAY)
        
        if not all_data:
            return None
        
        # Convert to DataFrame
        df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='s').dt.date
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
        df = df.drop_duplicates(subset=['date'], keep='first')
        df = df.sort_values('date')
        
        return df
        
    except Exception as e:
        return None


def main():
    """Download equity data for all NSE stocks"""
    print("\n" + "="*80)
    print("📊 DOWNLOADING EQUITY DATA FROM FYERS")
    print("="*80)
    print(f"Date Range: {START_DATE} to {END_DATE}")
    print(f"Output Directory: {EQUITY_OUTPUT_DIR}")
    
    # Validate Fyers connection
    print("\n🔍 Validating Fyers API Connection...")
    try:
        response = fyers_client.get_quotes("NSE:SBIN-EQ")
        if response.get('s') == 'ok':
            print("✅ Fyers API connection successful!")
        else:
            print(f"❌ Fyers API error: {response}")
            return
    except Exception as e:
        print(f"❌ Failed to connect to Fyers API: {str(e)}")
        return
    
    # Get list of companies from database
    db = SessionLocal()
    try:
        companies = db.query(Company).filter(Company.is_active == True).order_by(Company.symbol).all()
        total = len(companies)
        
        print(f"\n📋 Found {total} active companies in database")
        
        success_count = 0
        error_count = 0
        skipped_count = 0
        
        for idx, company in enumerate(companies, 1):
            symbol = company.symbol
            fyers_symbol = f"NSE:{symbol}-EQ"
            
            # Check if file already exists
            output_file = EQUITY_OUTPUT_DIR / f"{symbol}.csv"
            if output_file.exists():
                # Check if file needs update (if it's from today, skip)
                file_mod_time = datetime.fromtimestamp(output_file.stat().st_mtime).date()
                if file_mod_time == datetime.now().date():
                    skipped_count += 1
                    if idx % 100 == 0:
                        print(f"[{idx}/{total}] Progress: {success_count} success, {error_count} errors, {skipped_count} skipped")
                    continue
            
            # Fetch data
            df = fetch_historical_data(fyers_symbol, START_DATE, END_DATE)
            
            if df is not None and not df.empty:
                df.to_csv(output_file, index=False)
                success_count += 1
                if idx % 50 == 0:
                    print(f"[{idx}/{total}] {symbol} ✅ ({len(df)} candles)")
            else:
                error_count += 1
            
            # Rate limiting
            if idx < total:
                time.sleep(RATE_LIMIT_DELAY)
        
        print("\n" + "="*80)
        print(f"✅ Equity Download Complete!")
        print(f"   Success: {success_count}/{total}")
        print(f"   Errors: {error_count}/{total}")
        print(f"   Skipped (up-to-date): {skipped_count}/{total}")
        print(f"   Output: {EQUITY_OUTPUT_DIR}")
        print("="*80)
        
    finally:
        db.close()


if __name__ == "__main__":
    main()
