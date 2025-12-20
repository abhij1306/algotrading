"""
Download Nifty and Bank Nifty Data from Fyers - Daily update script
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
START_DATE = "2024-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
RATE_LIMIT_DELAY = 1.5
MAX_DAYS = 365

# Output directory
INDEX_OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "fyers_indices"
INDEX_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_historical_data(symbol: str, start_date: str, end_date: str):
    """Fetch historical data from Fyers API"""
    from datetime import datetime, timedelta
    
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        
        all_data = []
        current_start = start_dt
        
        while current_start < end_dt:
            current_end = min(current_start + timedelta(days=MAX_DAYS), end_dt)
            
            response = fyers_client.get_historical_data(
                symbol=symbol,
                timeframe="D",
                range_from=current_start.strftime("%Y-%m-%d"),
                range_to=current_end.strftime("%Y-%m-%d")
            )
            
            if response.get('s') == 'ok' and 'candles' in response:
                candles = response['candles']
                if candles:
                    all_data.extend(candles)
            
            current_start = current_end + timedelta(days=1)
            if current_start < end_dt:
                time.sleep(RATE_LIMIT_DELAY)
        
        if not all_data:
            return None
        
        df = pd.DataFrame(all_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['timestamp'], unit='s').dt.date
        df = df[['date', 'open', 'high', 'low', 'close', 'volume']]
        df = df.drop_duplicates(subset=['date'], keep='first')
        df = df.sort_values('date')
        
        return df
        
    except Exception as e:
        print(f"  ❌ Error: {str(e)}")
        return None


def main():
    """Download Nifty and Bank Nifty data"""
    print("\n" + "="*80)
    print("📈 DOWNLOADING NIFTY50 & BANK NIFTY DATA FROM FYERS")
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
    
    # Download Nifty50 and Bank Nifty
    indices = [
        ("NIFTY50", "NSE:NIFTY50-INDEX"),
        ("BANKNIFTY", "NSE:NIFTYBANK-INDEX"),  # Bank Nifty added
    ]
    
    success_count = 0
    
    for name, fyers_symbol in indices:
        print(f"\n📊 Downloading {name}...")
        
        output_file = INDEX_OUTPUT_DIR / f"{name}.csv"
        
        # Fetch data
        df = fetch_historical_data(fyers_symbol, START_DATE, END_DATE)
        
        if df is not None and not df.empty:
            df.to_csv(output_file, index=False)
            print(f"  ✅ Downloaded {len(df)} candles ({df['date'].min()} to {df['date'].max()})")
            print(f"  💾 Saved to {output_file.name}")
            success_count += 1
        else:
            print(f"  ❌ Failed to download {name}")
    
    print("\n" + "="*80)
    print(f"✅ Download Complete! ({success_count}/2 successful)")
    print(f"   Output: {INDEX_OUTPUT_DIR}")
    print("="*80)


if __name__ == "__main__":
    main()
