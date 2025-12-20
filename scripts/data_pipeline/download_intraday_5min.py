"""
Download 5-Minute Intraday Data for Nifty50 and Bank Nifty from Fyers
Downloads last 1 year of 5-minute candles
"""

import sys
import os
from pathlib import Path
import pandas as pd
from datetime import datetime, timedelta
import time

# Add AlgoTrading root to path
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))

# Import fyers client
sys.path.insert(0, str(algotrading_root / "fyers"))
import fyers_client

# Configuration
START_DATE = datetime(2019, 1, 1)  # From 2019 onwards (6 years of data)
END_DATE = datetime.now()
RATE_LIMIT_DELAY = 1.5
MAX_DAYS = 100  # Fyers API limit for minute resolutions

# Output directory
INTRADAY_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_intraday"
INTRADAY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_intraday_data(symbol: str, start_date: datetime, end_date: datetime, resolution: str = "5"):
    """
    Fetch intraday data from Fyers API
    
    Args:
        symbol: Fyers symbol (e.g., "NSE:NIFTY50-INDEX")
        start_date: Start datetime
        end_date: End datetime
        resolution: Candle resolution in minutes (default: "5" for 5-minute)
        
    Returns:
        DataFrame with OHLCV data or None if error
    """
    try:
        all_data = []
        current_start = start_date
        chunk_num = 1
        
        while current_start < end_date:
            # Calculate chunk end (100 days max for minute resolutions)
            current_end = min(current_start + timedelta(days=MAX_DAYS), end_date)
            
            chunk_start_str = current_start.strftime("%Y-%m-%d")
            chunk_end_str = current_end.strftime("%Y-%m-%d")
            
            print(f"    Chunk {chunk_num}: {chunk_start_str} to {chunk_end_str}")
            
            # Fetch data for this chunk
            response = fyers_client.get_historical_data(
                symbol=symbol,
                timeframe=resolution,  # 5-minute candles
                range_from=chunk_start_str,
                range_to=chunk_end_str
            )
            
            if response.get('s') != 'ok' or 'candles' not in response:
                print(f"    ❌ Chunk {chunk_num} error: {response.get('message', 'Unknown error')}")
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
            if current_start < end_date:
                time.sleep(RATE_LIMIT_DELAY)
        
        if not all_data:
            print(f"  ❌ No data retrieved for any chunk")
            return None
        
        # Convert to DataFrame
        df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
        df['date'] = df['datetime'].dt.date
        df['time'] = df['datetime'].dt.time
        df = df[['datetime', 'date', 'time', 'open', 'high', 'low', 'close', 'volume']]
        
        # Remove duplicates
        df = df.drop_duplicates(subset=['datetime'], keep='first')
        df = df.sort_values('datetime')
        
        print(f"  ✅ Total: {len(df)} candles ({df['datetime'].min()} to {df['datetime'].max()})")
        return df
        
    except Exception as e:
        print(f"  ❌ Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """Download 5-minute intraday data for Nifty50 and Bank Nifty"""
    print("\n" + "="*80)
    print("📊 DOWNLOADING 5-MINUTE INTRADAY DATA FROM FYERS")
    print("="*80)
    print(f"Date Range: {START_DATE.strftime('%Y-%m-%d')} to {END_DATE.strftime('%Y-%m-%d')}")
    print(f"Resolution: 5-minute candles")
    print(f"Output Directory: {INTRADAY_OUTPUT_DIR}")
    
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
    
    # Download Nifty50 and Bank Nifty 5-minute data
    indices = [
        ("NIFTY50", "NSE:NIFTY50-INDEX"),
        ("BANKNIFTY", "NSE:NIFTYBANK-INDEX"),
    ]
    
    success_count = 0
    
    for name, fyers_symbol in indices:
        print(f"\n📈 Downloading {name} (5-minute candles)...")
        
        output_file = INTRADAY_OUTPUT_DIR / f"{name}_5min_2019_present.csv"
        
        # Fetch data
        df = fetch_intraday_data(fyers_symbol, START_DATE, END_DATE, resolution="5")
        
        if df is not None and not df.empty:
            # Save to CSV
            df.to_csv(output_file, index=False)
            print(f"  💾 Saved to {output_file.name}")
            
            # Show summary
            total_days = (df['date'].max() - df['date'].min()).days
            avg_candles_per_day = len(df) / total_days if total_days > 0 else 0
            print(f"  📊 Summary:")
            print(f"     - Total candles: {len(df):,}")
            print(f"     - Date range: {df['date'].min()} to {df['date'].max()}")
            print(f"     - Trading days: {df['date'].nunique()}")
            print(f"     - Avg candles/day: {avg_candles_per_day:.0f}")
            print(f"     - File size: {output_file.stat().st_size / 1024 / 1024:.2f} MB")
            
            success_count += 1
        else:
            print(f"  ❌ Failed to download {name}")
    
    print("\n" + "="*80)
    print(f"✅ Download Complete! ({success_count}/2 successful)")
    print(f"   Output: {INTRADAY_OUTPUT_DIR}")
    print("="*80)
    
    print("\n💡 Next Steps:")
    print("   1. Use this data for intraday strategy backtesting")
    print("   2. Calculate intraday technical indicators")
    print("   3. Test ORB (Opening Range Breakout) strategies")
    print("   4. Analyze intraday volatility patterns")


if __name__ == "__main__":
    main()
