"""
Simplified Yahoo Data Cleaner - Avoids categorical type issues
"""
import pandas as pd
from pathlib import Path
from datetime import datetime
import sys

# Paths
YAHOO_EQUITY_DIR = Path("nse_data/raw/yahoo/equities")
NSE_EQUITY_PARQUET = Path("nse_data/processed/equities_clean/equity_ohlcv.parquet")
BACKUP_DIR = Path("nse_data/processed/backups")

def main():
    print("\n" + "="*60)
    print("YAHOO DATA CLEANER - SIMPLIFIED VERSION")
    print("="*60)
    
    # Check Yahoo data
    csv_files = list(YAHOO_EQUITY_DIR.glob("*.csv"))
    if not csv_files:
        print("❌ No Yahoo CSV files found!")
        return 1
    
    print(f"\n📁 Found {len(csv_files)} Yahoo CSV files")
    
    # Process Yahoo CSVs
    print("\n🔄 Processing Yahoo CSVs...")
    all_data = []
    
    for i, csv_file in enumerate(csv_files, 1):
        if i % 100 == 0:
            print(f"  Progress: {i}/{len(csv_files)}")
        
        try:
            df = pd.read_csv(csv_file)
            
            # Convert to NSE schema
            nse_df = pd.DataFrame({
                'trade_date': pd.to_datetime(df['date']),
                'symbol': df['symbol'].astype(str),
                'open': df['open'].astype(float),
                'high': df['high'].astype(float),
                'low': df['low'].astype(float),
                'close': df['close'].astype(float),
                'volume': df['volume'].astype('int64'),
                'turnover': (df['volume'] * ((df['high'] + df['low']) / 2)).astype('float64')
            })
            
            # Remove nulls and validate
            nse_df = nse_df.dropna()
            valid_mask = (
                (nse_df['high'] >= nse_df['low']) &
                (nse_df['volume'] > 0)
            )
            nse_df = nse_df[valid_mask]
            
            if not nse_df.empty:
                all_data.append(nse_df)
                
        except Exception as e:
            print(f"  ⚠️  Error in {csv_file.name}: {e}")
            continue
    
    if not all_data:
        print("\n❌ No valid data processed!")
        return 1
    
    # Combine Yahoo data
    print(f"\n✓ Processed {len(all_data)} files successfully")
    yahoo_combined = pd.concat(all_data, ignore_index=True)
    print(f"✓ Yahoo data: {len(yahoo_combined)} records, {yahoo_combined['symbol'].nunique()} symbols")
    
    # Load existing NSE data
    if NSE_EQUITY_PARQUET.exists():
        print(f"\n📂 Loading existing NSE Parquet...")
        # Read as plain dataframe
        nse_existing = pd.read_parquet(NSE_EQUITY_PARQUET, engine='pyarrow')
        
        # Convert all columns to standard types
        nse_existing['trade_date'] = pd.to_datetime(nse_existing['trade_date'])
        nse_existing['symbol'] = nse_existing['symbol'].astype(str)
        for col in ['open', 'high', 'low', 'close', 'turnover']:
            nse_existing[col] = nse_existing[col].astype(float)
        nse_existing['volume'] = nse_existing['volume'].astype('int64')
        
        print(f"  ✓ Existing: {len(nse_existing)} records, {nse_existing['symbol'].nunique()} symbols")
        print(f"  ✓ Date range: {nse_existing['trade_date'].min()} to {nse_existing['trade_date'].max()}")
        
        # Create backup
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = BACKUP_DIR / f"equity_ohlcv_backup_{timestamp}.parquet"
        import shutil
        shutil.copy2(NSE_EQUITY_PARQUET, backup_file)
        print(f"  ✓ Backup: {backup_file}")
        
        # Merge
        print("\n🔀 Merging...")
        merged = pd.concat([nse_existing, yahoo_combined], ignore_index=True)
        
        # Remove duplicates
        before = len(merged)
        merged = merged.drop_duplicates(subset=['trade_date', 'symbol'], keep='first')
        print(f"  ✓ Removed {before - len(merged)} duplicates")
        
    else:
        print("\n  ℹ️  No existing data, creating new file")
        merged = yahoo_combined
    
    # Sort and save
    merged = merged.sort_values(['trade_date', 'symbol']).reset_index(drop=True)
    
    print(f"\n💾 Saving to {NSE_EQUITY_PARQUET}...")
    NSE_EQUITY_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(NSE_EQUITY_PARQUET, index=False, engine='pyarrow')
    
    print(f"\n{'='*60}")
    print("✅ COMPLETE!")
    print(f"{'='*60}")
    print(f"✓ Total records: {len(merged):,}")
    print(f"✓ Total symbols: {merged['symbol'].nunique()}")
    print(f"✓ Date range: {merged['trade_date'].min()} to {merged['trade_date'].max()}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
