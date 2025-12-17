"""
Yahoo Finance Data Downloader for NSE Pipeline
Downloads equity and index data from Yahoo Finance for 2024-present
Saves raw CSVs to nse_data/raw/yahoo/
"""

import yfinance as yf
import pandas as pd
from datetime import date, datetime
from pathlib import Path
import time
import sys
import os

# Add backend to path for database access
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from app.database import SessionLocal, Company

# ============== CONFIGURATION ==============
START_DATE = "2024-01-01"
END_DATE = date.today().strftime("%Y-%m-%d")
SLEEP_SECONDS = 1.5  # Rate limiting

# Yahoo Finance Index Mapping
INDEX_MAPPING = {
    "NIFTY 50": "^NSEI",
    "NIFTY BANK": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
    "NIFTY REALTY": "^CNXREALTY",
    "NIFTY ENERGY": "^CNXENERGY",
    "NIFTY INFRA": "^CNXINFRA"
}

# Output directories
RAW_EQUITY_DIR = Path("nse_data/raw/yahoo/equities")
RAW_INDEX_DIR = Path("nse_data/raw/yahoo/indices")

# ============== HELPER FUNCTIONS ==============

def ensure_directories():
    """Create output directories if they don't exist"""
    RAW_EQUITY_DIR.mkdir(parents=True, exist_ok=True)
    RAW_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"✓ Output directories ready:")
    print(f"  - {RAW_EQUITY_DIR}")
    print(f"  - {RAW_INDEX_DIR}")


def nse_to_yahoo_symbol(nse_symbol: str) -> str:
    """Convert NSE symbol to Yahoo format (add .NS suffix)"""
    return f"{nse_symbol}.NS"


def fetch_yahoo_data(symbol: str, yahoo_symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    Fetch OHLCV data from Yahoo Finance
    
    Args:
        symbol: Original NSE symbol (for logging)
        yahoo_symbol: Yahoo Finance symbol (with .NS or ^)
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
    
    Returns:
        DataFrame with OHLCV data or None if failed
    """
    try:
        df = yf.download(
            yahoo_symbol,
            start=start_date,
            end=end_date,
            progress=False,
            auto_adjust=False,  # Get raw prices (not adjusted)
            threads=False       # Avoid connection issues
        )
        
        if df.empty:
            print(f"  ⚠️  No data returned")
            return None
        
        # Reset index to make Date a column
        df.reset_index(inplace=True)
        
        # Standardize column names (remove multi-level if present)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # Ensure standard column names
        df.columns = [col.lower().replace(' ', '_') for col in df.columns]
        
        # Add symbol column
        df['symbol'] = symbol
        
        return df
        
    except Exception as e:
        print(f"  ❌ Error: {str(e)}")
        return None


def save_csv(df: pd.DataFrame, output_dir: Path, symbol: str, start_date: str, end_date: str):
    """Save DataFrame to CSV file"""
    filename = f"{symbol}_{start_date}_{end_date}.csv"
    filepath = output_dir / filename
    df.to_csv(filepath, index=False)
    return filepath


# ============== EQUITY DOWNLOADER ==============

def download_equities(limit: int = None):
    """
    Download equity data for all active companies
    
    Args:
        limit: Optional limit on number of symbols to download (for testing)
    """
    print("\n" + "="*60)
    print("DOWNLOADING EQUITY DATA FROM YAHOO FINANCE")
    print("="*60)
    
    # Get active companies from database
    db = SessionLocal()
    try:
        query = db.query(Company).filter(Company.is_active == True)
        if limit:
            query = query.limit(limit)
        companies = query.all()
        
        total = len(companies)
        print(f"\n📊 Found {total} active companies")
        print(f"📅 Date range: {START_DATE} to {END_DATE}")
        print(f"⏱️  Rate limit: {SLEEP_SECONDS}s per symbol")
        
        if limit:
            print(f"🔬 TEST MODE: Downloading only {limit} symbols")
        
        estimated_time = total * SLEEP_SECONDS / 60
        print(f"⏰ Estimated time: ~{estimated_time:.0f} minutes\n")
        
        success_count = 0
        error_count = 0
        no_data_count = 0
        
        for i, company in enumerate(companies, 1):
            symbol = company.symbol
            yahoo_symbol = nse_to_yahoo_symbol(symbol)
            
            print(f"[{i}/{total}] {symbol:15s} ({yahoo_symbol})", end=" ... ")
            
            df = fetch_yahoo_data(symbol, yahoo_symbol, START_DATE, END_DATE)
            
            if df is not None and not df.empty:
                filepath = save_csv(df, RAW_EQUITY_DIR, symbol, START_DATE, END_DATE)
                print(f"✓ {len(df)} records saved")
                success_count += 1
            elif df is not None and df.empty:
                no_data_count += 1
            else:
                error_count += 1
            
            # Rate limiting
            if i < total:
                time.sleep(SLEEP_SECONDS)
        
        print(f"\n{'='*60}")
        print(f"EQUITY DOWNLOAD COMPLETE")
        print(f"{'='*60}")
        print(f"✓ Success: {success_count}")
        print(f"⚠️  No data: {no_data_count}")
        print(f"❌ Errors: {error_count}")
        print(f"📁 Files saved to: {RAW_EQUITY_DIR}")
        
    finally:
        db.close()


# ============== INDEX DOWNLOADER ==============

def download_indices():
    """Download index data for major NSE indices"""
    print("\n" + "="*60)
    print("DOWNLOADING INDEX DATA FROM YAHOO FINANCE")
    print("="*60)
    
    total = len(INDEX_MAPPING)
    print(f"\n📊 Downloading {total} indices")
    print(f"📅 Date range: {START_DATE} to {END_DATE}\n")
    
    success_count = 0
    error_count = 0
    
    for i, (index_name, yahoo_symbol) in enumerate(INDEX_MAPPING.items(), 1):
        print(f"[{i}/{total}] {index_name:20s} ({yahoo_symbol})", end=" ... ")
        
        # Use index name without spaces for filename
        file_symbol = index_name.replace(" ", "_").lower()
        
        df = fetch_yahoo_data(file_symbol, yahoo_symbol, START_DATE, END_DATE)
        
        if df is not None and not df.empty:
            filepath = save_csv(df, RAW_INDEX_DIR, file_symbol, START_DATE, END_DATE)
            print(f"✓ {len(df)} records saved")
            success_count += 1
        else:
            error_count += 1
        
        # Rate limiting
        if i < total:
            time.sleep(SLEEP_SECONDS)
    
    print(f"\n{'='*60}")
    print(f"INDEX DOWNLOAD COMPLETE")
    print(f"{'='*60}")
    print(f"✓ Success: {success_count}")
    print(f"❌ Errors: {error_count}")
    print(f"📁 Files saved to: {RAW_INDEX_DIR}")


# ============== MAIN ==============

def main():
    """Main execution function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Download Yahoo Finance data for NSE equities and indices')
    parser.add_argument('--equities-only', action='store_true', help='Download only equity data')
    parser.add_argument('--indices-only', action='store_true', help='Download only index data')
    parser.add_argument('--limit', type=int, help='Limit number of equity symbols (for testing)')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    
    args = parser.parse_args()
    
    # Use provided dates or defaults
    global START_DATE, END_DATE
    if args.start:
        START_DATE = args.start
    if args.end:
        END_DATE = args.end
    
    # Ensure directories exist
    ensure_directories()
    
    start_time = datetime.now()
    
    # Download based on arguments
    if args.indices_only:
        download_indices()
    elif args.equities_only:
        download_equities(limit=args.limit)
    else:
        # Download both
        download_indices()
        download_equities(limit=args.limit)
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds() / 60
    
    print(f"\n{'='*60}")
    print(f"TOTAL TIME: {duration:.1f} minutes")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
