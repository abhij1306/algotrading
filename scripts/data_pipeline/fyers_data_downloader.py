"""
Fyers Data Downloader - Download historical equity and index data from Fyers API
Downloads daily OHLCV data from 2016 to present for NSE equities and indices
"""

import sys
import os
from pathlib import Path
import pandas as pd
from datetime import datetime, timedelta
import time
from typing import Optional, List

# Add AlgoTrading root to path
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))

# Import fyers client
sys.path.insert(0, str(algotrading_root / "fyers"))
import fyers_client

from backend.app.database import SessionLocal, Company
from sqlalchemy import text

# Configuration
START_DATE = "2016-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
RATE_LIMIT_DELAY = 1.5  # seconds between API calls
BATCH_SIZE = 100  # Process in batches

# Output directories
EQUITY_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_equities"
INDEX_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_indices"

# Create output directories
EQUITY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
INDEX_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_historical_data(symbol: str, start_date: str, end_date: str, is_index: bool = False) -> Optional[pd.DataFrame]:
    """
    Fetch historical data from Fyers API
    
    Args:
        symbol: Fyers symbol (e.g., "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        is_index: True if fetching index data
        
    Returns:
        DataFrame with OHLCV data or None if error
    """
    try:
        response = fyers_client.get_historical_data(
            symbol=symbol,
            timeframe="D",  # Daily
            range_from=start_date,
            range_to=end_date
        )
        
        if response.get('s') != 'ok' or 'candles' not in response:
            print(f"  ❌ Error: {response.get('message', 'Unknown error')}")
            return None
        
        candles = response['candles']
        if not candles:
            print(f"  ⚠️  No data returned")
            return None
        
        # Convert to DataFrame
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='s').dt.date
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
        
        print(f"  ✅ Downloaded {len(df)} candles ({df['date'].min()} to {df['date'].max()})")
        return df
        
    except Exception as e:
        print(f"  ❌ Exception: {str(e)}")
        return None


def download_equity_data(limit: Optional[int] = None):
    """
    Download historical data for all NSE equities from database
    
    Args:
        limit: Optional limit on number of symbols to download (for testing)
    """
    print("\n" + "="*80)
    print("📊 DOWNLOADING EQUITY DATA FROM FYERS")
    print("="*80)
    print(f"Date Range: {START_DATE} to {END_DATE}")
    print(f"Output Directory: {EQUITY_OUTPUT_DIR}")
    print(f"Rate Limit: {RATE_LIMIT_DELAY}s between requests")
    
    # Get list of companies from database
    db = SessionLocal()
    try:
        query = db.query(Company).filter(Company.is_active == True).order_by(Company.symbol)
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"\n📋 Found {total} active companies in database")
        
        success_count = 0
        error_count = 0
        
        for idx, company in enumerate(companies, 1):
            symbol = company.symbol
            fyers_symbol = f"NSE:{symbol}-EQ"
            
            print(f"\n[{idx}/{total}] {symbol}")
            
            # Check if file already exists
            output_file = EQUITY_OUTPUT_DIR / f"{symbol}.csv"
            if output_file.exists():
                print(f"  ⏭️  Skipping (file exists)")
                success_count += 1
                continue
            
            # Fetch data
            df = fetch_historical_data(fyers_symbol, START_DATE, END_DATE, is_index=False)
            
            if df is not None and not df.empty:
                # Save to CSV
                df.to_csv(output_file, index=False)
                success_count += 1
            else:
                error_count += 1
            
            # Rate limiting
            if idx < total:
                time.sleep(RATE_LIMIT_DELAY)
        
        print("\n" + "="*80)
        print(f"✅ Equity Download Complete!")
        print(f"   Success: {success_count}/{total}")
        print(f"   Errors: {error_count}/{total}")
        print("="*80)
        
    finally:
        db.close()


def download_index_data():
    """
    Download historical data for major NSE indices
    """
    print("\n" + "="*80)
    print("📈 DOWNLOADING INDEX DATA FROM FYERS")
    print("="*80)
    print(f"Date Range: {START_DATE} to {END_DATE}")
    print(f"Output Directory: {INDEX_OUTPUT_DIR}")
    
    # Major NSE indices
    indices = [
        ("NIFTY50", "NSE:NIFTY50-INDEX"),
        ("BANKNIFTY", "NSE:BANKNIFTY-INDEX"),
        ("NIFTYNXT50", "NSE:NIFTYNXT50-INDEX"),
        ("NIFTY100", "NSE:NIFTY100-INDEX"),
        ("NIFTY200", "NSE:NIFTY200-INDEX"),
        ("NIFTY500", "NSE:NIFTY500-INDEX"),
        ("NIFTYMIDCAP50", "NSE:NIFTYMIDCAP50-INDEX"),
        ("NIFTYMIDCAP100", "NSE:NIFTYMIDCAP100-INDEX"),
        ("NIFTYSMLCAP100", "NSE:NIFTYSMLCAP100-INDEX"),
        ("NIFTYIT", "NSE:NIFTYIT-INDEX"),
    ]
    
    total = len(indices)
    success_count = 0
    error_count = 0
    
    for idx, (name, fyers_symbol) in enumerate(indices, 1):
        print(f"\n[{idx}/{total}] {name}")
        
        # Check if file already exists
        output_file = INDEX_OUTPUT_DIR / f"{name}.csv"
        if output_file.exists():
            print(f"  ⏭️  Skipping (file exists)")
            success_count += 1
            continue
        
        # Fetch data
        df = fetch_historical_data(fyers_symbol, START_DATE, END_DATE, is_index=True)
        
        if df is not None and not df.empty:
            # Save to CSV
            df.to_csv(output_file, index=False)
            success_count += 1
        else:
            error_count += 1
        
        # Rate limiting
        if idx < total:
            time.sleep(RATE_LIMIT_DELAY)
    
    print("\n" + "="*80)
    print(f"✅ Index Download Complete!")
    print(f"   Success: {success_count}/{total}")
    print(f"   Errors: {error_count}/{total}")
    print("="*80)


def validate_fyers_connection():
    """
    Validate Fyers API connection before starting download
    """
    print("\n🔍 Validating Fyers API Connection...")
    
    try:
        # Test with a simple quote request
        response = fyers_client.get_quotes("NSE:SBIN-EQ")
        
        if response.get('s') == 'ok':
            print("✅ Fyers API connection successful!")
            return True
        else:
            print(f"❌ Fyers API error: {response}")
            return False
            
    except Exception as e:
        print(f"❌ Failed to connect to Fyers API: {str(e)}")
        return False


def main():
    """
    Main execution function
    """
    print("\n" + "="*80)
    print("🚀 FYERS HISTORICAL DATA DOWNLOADER")
    print("="*80)
    print(f"Start Date: {START_DATE}")
    print(f"End Date: {END_DATE}")
    print(f"Rate Limit: {RATE_LIMIT_DELAY}s per request")
    
    # Validate connection
    if not validate_fyers_connection():
        print("\n❌ Cannot proceed without valid Fyers API connection")
        print("Please ensure you are logged in to Fyers")
        return
    
    # Ask user what to download
    print("\n📋 What would you like to download?")
    print("1. Index data only (Nifty, Bank Nifty, etc.)")
    print("2. Equity data only (all NSE stocks)")
    print("3. Both index and equity data")
    print("4. Test mode (10 equities + indices)")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice == "1":
        download_index_data()
    elif choice == "2":
        download_equity_data()
    elif choice == "3":
        download_index_data()
        download_equity_data()
    elif choice == "4":
        print("\n🧪 TEST MODE - Downloading 10 equities + all indices")
        download_index_data()
        download_equity_data(limit=10)
    else:
        print("❌ Invalid choice")
        return
    
    print("\n" + "="*80)
    print("🎉 DOWNLOAD COMPLETE!")
    print("="*80)
    print(f"\nEquity data saved to: {EQUITY_OUTPUT_DIR}")
    print(f"Index data saved to: {INDEX_OUTPUT_DIR}")
    print("\nNext steps:")
    print("1. Review the downloaded data")
    print("2. Run data cleaning scripts to process into Parquet format")
    print("3. Update PostgreSQL database with new data")


if __name__ == "__main__":
    main()
