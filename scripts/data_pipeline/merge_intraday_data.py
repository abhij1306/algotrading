"""
Merge Intraday 5-Minute Data Files
Combines 2016-2019 and 2019-present files into complete datasets
"""

import pandas as pd
from pathlib import Path
import sys

# Add AlgoTrading root to path
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))

# Configuration
INTRADAY_DIR = algotrading_root / "nse_data" / "raw" / "fyers_intraday"

def merge_intraday_files(index_name: str):
    """
    Merge 2016-2019 and 2019-present files for a given index
    
    Args:
        index_name: Index name (e.g., "NIFTY50", "BANKNIFTY")
    """
    print(f"\n{'='*80}")
    print(f"📊 Merging {index_name} 5-Minute Data")
    print(f"{'='*80}")
    
    # File paths
    file_2016_2019 = INTRADAY_DIR / f"{index_name}_5min_2016_2019.csv"
    file_2019_present = INTRADAY_DIR / f"{index_name}_5min_2019_present.csv"
    output_file = INTRADAY_DIR / f"{index_name}_5min_complete.csv"
    
    # Check if files exist
    if not file_2016_2019.exists():
        print(f"❌ File not found: {file_2016_2019.name}")
        return False
    
    if not file_2019_present.exists():
        print(f"❌ File not found: {file_2019_present.name}")
        return False
    
    print(f"📂 Reading {file_2016_2019.name}...")
    df1 = pd.read_csv(file_2016_2019)
    df1['datetime'] = pd.to_datetime(df1['datetime'])
    print(f"   ✅ Loaded {len(df1):,} candles ({df1['datetime'].min()} to {df1['datetime'].max()})")
    
    print(f"📂 Reading {file_2019_present.name}...")
    df2 = pd.read_csv(file_2019_present)
    df2['datetime'] = pd.to_datetime(df2['datetime'])
    print(f"   ✅ Loaded {len(df2):,} candles ({df2['datetime'].min()} to {df2['datetime'].max()})")
    
    # Combine dataframes
    print(f"\n🔗 Merging dataframes...")
    df_combined = pd.concat([df1, df2], ignore_index=True)
    
    # Remove duplicates (keep first occurrence)
    print(f"🧹 Removing duplicates...")
    before_count = len(df_combined)
    df_combined = df_combined.drop_duplicates(subset=['datetime'], keep='first')
    after_count = len(df_combined)
    duplicates_removed = before_count - after_count
    print(f"   ✅ Removed {duplicates_removed:,} duplicate candles")
    
    # Sort by datetime
    print(f"📊 Sorting by datetime...")
    df_combined = df_combined.sort_values('datetime').reset_index(drop=True)
    
    # Save merged file
    print(f"\n💾 Saving merged file...")
    df_combined.to_csv(output_file, index=False)
    
    # Delete source files after successful merge
    print(f"\n🗑️  Deleting source files...")
    try:
        file_2016_2019.unlink()
        print(f"   ✅ Deleted {file_2016_2019.name}")
    except Exception as e:
        print(f"   ⚠️  Could not delete {file_2016_2019.name}: {e}")
    
    try:
        file_2019_present.unlink()
        print(f"   ✅ Deleted {file_2019_present.name}")
    except Exception as e:
        print(f"   ⚠️  Could not delete {file_2019_present.name}: {e}")
    
    # Also delete the old 1-year file if it exists
    old_file = INTRADAY_DIR / f"{index_name}_5min.csv"
    if old_file.exists():
        try:
            old_file.unlink()
            print(f"   ✅ Deleted {old_file.name} (old 1-year file)")
        except Exception as e:
            print(f"   ⚠️  Could not delete {old_file.name}: {e}")
    
    # Summary
    file_size_mb = output_file.stat().st_size / 1024 / 1024
    trading_days = df_combined['date'].nunique()
    avg_candles_per_day = len(df_combined) / trading_days if trading_days > 0 else 0
    
    print(f"\n{'='*80}")
    print(f"✅ {index_name} Merge Complete!")
    print(f"{'='*80}")
    print(f"📊 Summary:")
    print(f"   - Total candles: {len(df_combined):,}")
    print(f"   - Date range: {df_combined['datetime'].min()} to {df_combined['datetime'].max()}")
    print(f"   - Trading days: {trading_days:,}")
    print(f"   - Avg candles/day: {avg_candles_per_day:.0f}")
    print(f"   - File size: {file_size_mb:.2f} MB")
    print(f"   - Output: {output_file.name}")
    print(f"{'='*80}\n")
    
    return True


def main():
    """Merge intraday files for all indices"""
    print("\n" + "="*80)
    print("🔗 MERGING INTRADAY 5-MINUTE DATA FILES")
    print("="*80)
    print(f"Directory: {INTRADAY_DIR}")
    
    # Indices to merge
    indices = ["NIFTY50", "BANKNIFTY"]
    
    success_count = 0
    
    for index_name in indices:
        if merge_intraday_files(index_name):
            success_count += 1
    
    print("\n" + "="*80)
    print(f"✅ MERGE COMPLETE! ({success_count}/{len(indices)} successful)")
    print("="*80)
    
    if success_count == len(indices):
        print("\n💡 Next Steps:")
        print("   1. Use complete files for backtesting (2016-2025)")
        print("   2. Calculate intraday technical indicators")
        print("   3. Analyze 9 years of intraday patterns")
        print("   4. Test ORB and other intraday strategies")
        print("\n📁 Complete Files:")
        for index_name in indices:
            complete_file = INTRADAY_DIR / f"{index_name}_5min_complete.csv"
            if complete_file.exists():
                size_mb = complete_file.stat().st_size / 1024 / 1024
                print(f"   - {complete_file.name} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
