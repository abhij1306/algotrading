"""
Yahoo Data Cleaner for NSE Pipeline
Converts Yahoo Finance CSVs to NSE schema and merges with existing Parquet files
"""

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path
from datetime import datetime
import sys

# ============== CONFIGURATION ==============

# Input directories (Yahoo raw CSVs)
YAHOO_EQUITY_DIR = Path("nse_data/raw/yahoo/equities")
YAHOO_INDEX_DIR = Path("nse_data/raw/yahoo/indices")

# Output directories (NSE Parquet files)
NSE_EQUITY_PARQUET = Path("nse_data/processed/equities_clean/equity_ohlcv.parquet")
NSE_INDEX_PARQUET = Path("nse_data/processed/indices_clean/index_ohlcv.parquet")

# Backup directory
BACKUP_DIR = Path("nse_data/processed/backups")

# ============== HELPER FUNCTIONS ==============

def create_backup(parquet_file: Path):
    """Create timestamped backup of existing Parquet file"""
    if not parquet_file.exists():
        print(f"  ℹ️  No existing file to backup: {parquet_file}")
        return None
    
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"{parquet_file.stem}_backup_{timestamp}.parquet"
    
    # Copy file
    import shutil
    shutil.copy2(parquet_file, backup_file)
    print(f"  ✓ Backup created: {backup_file}")
    return backup_file


def clean_yahoo_equity_csv(csv_file: Path) -> pd.DataFrame:
    """
    Read Yahoo equity CSV and convert to NSE schema
    
    Yahoo columns: date, adj_close, close, high, low, open, volume, symbol
    NSE columns: trade_date, symbol, open, high, low, close, volume, turnover
    """
    try:
        df = pd.read_csv(csv_file)
        
        # Convert to NSE schema
        nse_df = pd.DataFrame({
            'trade_date': pd.to_datetime(df['date']).dt.date,
            'symbol': df['symbol'],
            'open': df['open'],
            'high': df['high'],
            'low': df['low'],
            'close': df['close'],
            'volume': df['volume'].astype('int64'),
            'turnover': (df['volume'] * ((df['high'] + df['low']) / 2)).astype('float64')  # Approximate turnover
        })
        
        # Remove rows with null values
        nse_df = nse_df.dropna()
        
        # Validate OHLCV constraints
        valid_mask = (
            (nse_df['high'] >= nse_df['low']) &
            (nse_df['high'] >= nse_df['open']) &
            (nse_df['high'] >= nse_df['close']) &
            (nse_df['low'] <= nse_df['open']) &
            (nse_df['low'] <= nse_df['close']) &
            (nse_df['volume'] > 0)
        )
        
        invalid_count = (~valid_mask).sum()
        if invalid_count > 0:
            print(f"    ⚠️  Removed {invalid_count} invalid rows from {csv_file.name}")
            nse_df = nse_df[valid_mask]
        
        return nse_df
        
    except Exception as e:
        print(f"    ❌ Error processing {csv_file.name}: {e}")
        return pd.DataFrame()


def clean_yahoo_index_csv(csv_file: Path) -> pd.DataFrame:
    """
    Read Yahoo index CSV and convert to NSE schema
    
    Yahoo columns: date, adj_close, close, high, low, open, volume, symbol
    NSE columns: trade_date, symbol, open, high, low, close, volume, turnover
    """
    try:
        df = pd.read_csv(csv_file)
        
        # Convert to NSE schema (same as equity)
        nse_df = pd.DataFrame({
            'trade_date': pd.to_datetime(df['date']).dt.date,
            'symbol': df['symbol'].str.upper(),  # Uppercase for consistency
            'open': df['open'],
            'high': df['high'],
            'low': df['low'],
            'close': df['close'],
            'volume': df['volume'].astype('int64'),
            'turnover': (df['volume'] * ((df['high'] + df['low']) / 2)).astype('float64')
        })
        
        # Remove rows with null values
        nse_df = nse_df.dropna()
        
        return nse_df
        
    except Exception as e:
        print(f"    ❌ Error processing {csv_file.name}: {e}")
        return pd.DataFrame()


# ============== EQUITY CLEANER ==============

def clean_and_merge_equities():
    """Clean Yahoo equity CSVs and merge with existing NSE Parquet"""
    print("\n" + "="*60)
    print("CLEANING AND MERGING EQUITY DATA")
    print("="*60)
    
    # Check if Yahoo data exists
    if not YAHOO_EQUITY_DIR.exists() or not list(YAHOO_EQUITY_DIR.glob("*.csv")):
        print("❌ No Yahoo equity CSV files found!")
        print(f"   Expected location: {YAHOO_EQUITY_DIR}")
        return False
    
    csv_files = list(YAHOO_EQUITY_DIR.glob("*.csv"))
    print(f"\n📁 Found {len(csv_files)} Yahoo equity CSV files")
    
    # Process all Yahoo CSVs
    print("\n🔄 Processing Yahoo CSVs...")
    yahoo_dfs = []
    
    for i, csv_file in enumerate(csv_files, 1):
        print(f"  [{i}/{len(csv_files)}] {csv_file.name}", end=" ... ")
        df = clean_yahoo_equity_csv(csv_file)
        if not df.empty:
            yahoo_dfs.append(df)
            print(f"✓ {len(df)} records")
        else:
            print("⚠️  Skipped (empty or error)")
    
    if not yahoo_dfs:
        print("\n❌ No valid Yahoo data to merge!")
        return False
    
    # Combine all Yahoo data
    yahoo_combined = pd.concat(yahoo_dfs, ignore_index=True)
    print(f"\n✓ Combined Yahoo data: {len(yahoo_combined)} records, {yahoo_combined['symbol'].nunique()} symbols")
    
    # Load existing NSE Parquet (if exists)
    if NSE_EQUITY_PARQUET.exists():
        print(f"\n📂 Loading existing NSE Parquet: {NSE_EQUITY_PARQUET}")
        nse_existing = pd.read_parquet(NSE_EQUITY_PARQUET)
        print(f"  ✓ Existing data: {len(nse_existing)} records, {nse_existing['symbol'].nunique()} symbols")
        print(f"  ✓ Date range: {nse_existing['trade_date'].min()} to {nse_existing['trade_date'].max()}")
        
        # Create backup
        create_backup(NSE_EQUITY_PARQUET)
        
        # Merge: Append Yahoo data, remove duplicates (keep NSE data if overlap)
        print("\n🔀 Merging datasets...")
        merged = pd.concat([nse_existing, yahoo_combined], ignore_index=True)
        
        # Remove duplicates (keep first occurrence = NSE data)
        before_dedup = len(merged)
        merged = merged.drop_duplicates(subset=['trade_date', 'symbol'], keep='first')
        duplicates_removed = before_dedup - len(merged)
        
        if duplicates_removed > 0:
            print(f"  ✓ Removed {duplicates_removed} duplicate records")
        
    else:
        print(f"\n  ℹ️  No existing NSE Parquet found, creating new file")
        merged = yahoo_combined
    
    # Sort by date and symbol
    merged = merged.sort_values(['trade_date', 'symbol']).reset_index(drop=True)
    
    # Save merged Parquet
    print(f"\n💾 Saving merged Parquet: {NSE_EQUITY_PARQUET}")
    NSE_EQUITY_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(NSE_EQUITY_PARQUET, index=False, engine='pyarrow')
    
    print(f"\n{'='*60}")
    print(f"EQUITY MERGE COMPLETE")
    print(f"{'='*60}")
    print(f"✓ Total records: {len(merged)}")
    print(f"✓ Total symbols: {merged['symbol'].nunique()}")
    print(f"✓ Date range: {merged['trade_date'].min()} to {merged['trade_date'].max()}")
    print(f"✓ File saved: {NSE_EQUITY_PARQUET}")
    
    return True


# ============== INDEX CLEANER ==============

def clean_and_merge_indices():
    """Clean Yahoo index CSVs and merge with existing NSE Parquet"""
    print("\n" + "="*60)
    print("CLEANING AND MERGING INDEX DATA")
    print("="*60)
    
    # Check if Yahoo data exists
    if not YAHOO_INDEX_DIR.exists() or not list(YAHOO_INDEX_DIR.glob("*.csv")):
        print("❌ No Yahoo index CSV files found!")
        print(f"   Expected location: {YAHOO_INDEX_DIR}")
        return False
    
    csv_files = list(YAHOO_INDEX_DIR.glob("*.csv"))
    print(f"\n📁 Found {len(csv_files)} Yahoo index CSV files")
    
    # Process all Yahoo CSVs
    print("\n🔄 Processing Yahoo CSVs...")
    yahoo_dfs = []
    
    for i, csv_file in enumerate(csv_files, 1):
        print(f"  [{i}/{len(csv_files)}] {csv_file.name}", end=" ... ")
        df = clean_yahoo_index_csv(csv_file)
        if not df.empty:
            yahoo_dfs.append(df)
            print(f"✓ {len(df)} records")
        else:
            print("⚠️  Skipped (empty or error)")
    
    if not yahoo_dfs:
        print("\n❌ No valid Yahoo data to merge!")
        return False
    
    # Combine all Yahoo data
    yahoo_combined = pd.concat(yahoo_dfs, ignore_index=True)
    print(f"\n✓ Combined Yahoo data: {len(yahoo_combined)} records, {yahoo_combined['symbol'].nunique()} indices")
    
    # Load existing NSE Parquet (if exists)
    if NSE_INDEX_PARQUET.exists():
        print(f"\n📂 Loading existing NSE Parquet: {NSE_INDEX_PARQUET}")
        nse_existing = pd.read_parquet(NSE_INDEX_PARQUET)
        print(f"  ✓ Existing data: {len(nse_existing)} records")
        print(f"  ✓ Date range: {nse_existing['trade_date'].min()} to {nse_existing['trade_date'].max()}")
        
        # Create backup
        create_backup(NSE_INDEX_PARQUET)
        
        # Merge: Append Yahoo data, remove duplicates
        print("\n🔀 Merging datasets...")
        merged = pd.concat([nse_existing, yahoo_combined], ignore_index=True)
        
        # Remove duplicates (keep first occurrence = NSE data)
        before_dedup = len(merged)
        merged = merged.drop_duplicates(subset=['trade_date', 'symbol'], keep='first')
        duplicates_removed = before_dedup - len(merged)
        
        if duplicates_removed > 0:
            print(f"  ✓ Removed {duplicates_removed} duplicate records")
        
    else:
        print(f"\n  ℹ️  No existing NSE Parquet found, creating new file")
        merged = yahoo_combined
    
    # Sort by date and symbol
    merged = merged.sort_values(['trade_date', 'symbol']).reset_index(drop=True)
    
    # Save merged Parquet
    print(f"\n💾 Saving merged Parquet: {NSE_INDEX_PARQUET}")
    NSE_INDEX_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(NSE_INDEX_PARQUET, index=False, engine='pyarrow')
    
    print(f"\n{'='*60}")
    print(f"INDEX MERGE COMPLETE")
    print(f"{'='*60}")
    print(f"✓ Total records: {len(merged)}")
    print(f"✓ Total indices: {merged['symbol'].nunique()}")
    print(f"✓ Date range: {merged['trade_date'].min()} to {merged['trade_date'].max()}")
    print(f"✓ File saved: {NSE_INDEX_PARQUET}")
    
    return True


# ============== MAIN ==============

def main():
    """Main execution function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Clean Yahoo Finance CSVs and merge with NSE Parquet files')
    parser.add_argument('--equities-only', action='store_true', help='Process only equity data')
    parser.add_argument('--indices-only', action='store_true', help='Process only index data')
    
    args = parser.parse_args()
    
    start_time = datetime.now()
    
    success = True
    
    # Process based on arguments
    if args.indices_only:
        success = clean_and_merge_indices()
    elif args.equities_only:
        success = clean_and_merge_equities()
    else:
        # Process both
        success_eq = clean_and_merge_equities()
        success_idx = clean_and_merge_indices()
        success = success_eq and success_idx
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    
    print(f"\n{'='*60}")
    print(f"TOTAL TIME: {duration:.1f} seconds")
    print(f"{'='*60}\n")
    
    if success:
        print("✅ Data cleaning and merging completed successfully!")
        print("\n📋 Next steps:")
        print("  1. Run: python nse_adjust_prices.py (apply corporate actions)")
        print("  2. Run: python nse_validate_adjusted.py (validate)")
        return 0
    else:
        print("❌ Data cleaning failed. Check errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
