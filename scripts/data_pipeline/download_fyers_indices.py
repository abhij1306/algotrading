"""
Download Index Data from Fyers - Simplified version for Nifty and Bank Nifty
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

# Configuration
START_DATE = "2024-01-01"  # Updated to fetch only recent data
END_DATE = datetime.now().strftime("%Y-%m-%d")
RATE_LIMIT_DELAY = 1.5  # seconds between API calls

# Output directory
INDEX_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_indices"
INDEX_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_historical_data(symbol: str, start_date: str, end_date: str):
    """
    Fetch historical data from Fyers API
    
    Note: Fyers API limits daily data to 366 days per request.
    This function automatically splits requests into chunks.
    
    Args:
        symbol: Fyers symbol (e.g., "NSE:SBIN-EQ" or "NSE:NIFTY50-INDEX")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        
    Returns:
        DataFrame with OHLCV data or None if error
    """
    from datetime import datetime, timedelta
    
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        # Fyers API limit: 366 days for daily data
        MAX_DAYS = 365
        all_data = []
        
        current_start = start_dt
        chunk_num = 1
        
        while current_start < end_dt:
            # Calculate chunk end date (366 days from current start or end_dt, whichever is earlier)
            current_end = min(current_start + timedelta(days=MAX_DAYS), end_dt)
            
            chunk_start_str = current_start.strftime("%Y-%m-%d")
            chunk_end_str = current_end.strftime("%Y-%m-%d")
            
            print(f"    Chunk {chunk_num}: {chunk_start_str} to {chunk_end_str}")
            
            # Fetch data for this chunk
            response = fyers_client.get_historical_data(
                symbol=symbol,
                timeframe="D",  # Daily
                range_from=chunk_start_str,
                range_to=chunk_end_str
            )
            
            if response.get('s') != 'ok' or 'candles' not in response:
                print(f"    ❌ Chunk {chunk_num} error: {response.get('message', 'Unknown error')}")
                # Continue with next chunk even if one fails
                current_start = current_end + timedelta(days=1)
                chunk_num += 1
                time.sleep(RATE_LIMIT_DELAY)
                continue
            
            candles = response['candles']
            if candles:
                all_data.extend(candles)
                print(f"    ✅ Chunk {chunk_num}: {len(candles)} candles")
            else:
                print(f"    ⚠️  Chunk {chunk_num}: No data")
            
            # Move to next chunk
            current_start = current_end + timedelta(days=1)
            chunk_num += 1
            
            # Rate limiting between chunks
            if current_start < end_dt:
                time.sleep(RATE_LIMIT_DELAY)
        
        if not all_data:
            print(f"  ❌ No data retrieved for any chunk")
            return None
        
        # Convert combined data to DataFrame
        df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='s').dt.date
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
        
        # Remove duplicates (in case of overlapping data)
        df = df.drop_duplicates(subset=['date'], keep='first')
        df = df.sort_values('date')
        
        print(f"  ✅ Total: {len(df)} candles ({df['date'].min()} to {df['date'].max()})")
        return df
        
    except Exception as e:
        print(f"  ❌ Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Download index data"""
    print("\n" + "="*80)
    print("📈 DOWNLOADING INDEX DATA FROM FYERS")
    print("="*80)
    print(f"Date Range: {START_DATE} to {END_DATE}")
    print(f"Output Directory: {INDEX_OUTPUT_DIR}")
    
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
    
    print(f"\n📋 Downloading {total} indices...")
    
    for idx, (name, fyers_symbol) in enumerate(indices, 1):
        print(f"\n[{idx}/{total}] {name}")
        
        # Check if file already exists
        output_file = INDEX_OUTPUT_DIR / f"{name}.csv"
        if output_file.exists():
            print(f"  ⏭️  Skipping (file exists)")
            success_count += 1
            continue
        
        # Fetch data
        df = fetch_historical_data(fyers_symbol, START_DATE, END_DATE)
        
        if df is not None and not df.empty:
            # Save to CSV
            df.to_csv(output_file, index=False)
            print(f"  💾 Saved to {output_file.name}")
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
    print(f"   Output: {INDEX_OUTPUT_DIR}")
    print("="*80)


if __name__ == "__main__":
    main()
