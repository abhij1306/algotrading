"""
Download 2024 Index Data from Fyers and merge with existing indices
"""

import sys
from pathlib import Path
import pandas as pd
from datetime import datetime
import time

# Add paths
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))
sys.path.insert(0, str(algotrading_root / "fyers"))
import fyers_client

# Configuration
START_DATE = "2024-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
OUTPUT_DIR = algotrading_root / "nse_data" / "raw" / "indices"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Indices to download
INDICES = [
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

print("=" * 60)
print("DOWNLOADING 2024 INDEX DATA FROM FYERS")
print("=" * 60)
print(f"Date Range: {START_DATE} to {END_DATE}")

success = 0
errors = 0

for name, symbol in INDICES:
    print(f"\n📊 {name}...")
    
    try:
        # Fetch data
        response = fyers_client.get_historical_data(
            symbol=symbol,
            timeframe="D",
            range_from=START_DATE,
            range_to=END_DATE
        )
        
        if response.get('s') != 'ok' or 'candles' not in response:
            print(f"  ❌ Error: {response.get('message', 'Unknown')}")
            errors += 1
            continue
        
        # Convert to DataFrame
        candles = response['candles']
        df_new = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_new['date'] = pd.to_datetime(df_new['timestamp'], unit='s').dt.date
        df_new = df_new[['date', 'open', 'high', 'low', 'close', 'volume']]
        
        # Check if file exists and merge
        output_file = OUTPUT_DIR / f"{name}.csv"
        if output_file.exists():
            df_old = pd.read_csv(output_file)
            df_old['date'] = pd.to_datetime(df_old['date']).dt.date
            
            # Combine and remove duplicates
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            df_combined = df_combined.drop_duplicates(subset=['date'], keep='last')
            df_combined = df_combined.sort_values('date')
            df_combined.to_csv(output_file, index=False)
            print(f"  ✅ Merged: {len(df_new)} new rows, total {len(df_combined)} rows")
        else:
            df_new.to_csv(output_file, index=False)
            print(f"  ✅ Created: {len(df_new)} rows")
        
        success += 1
        time.sleep(1)  # Rate limiting
        
    except Exception as e:
        print(f"  ❌ Exception: {e}")
        errors += 1

print("\n" + "=" * 60)
print(f"✅ Success: {success}/{len(INDICES)}")
print(f"❌ Errors: {errors}/{len(INDICES)}")
print("=" * 60)
