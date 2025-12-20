"""
Consolidate All Data - Merge Fyers + NSE, Remove Yahoo, Clean Structure
This script:
1. Merges Fyers daily data with existing NSE Parquet files
2. Moves Fyers intraday data to proper location
3. Deletes Yahoo data folder
4. Removes duplicate folders
5. Creates clean unified structure
"""

import pandas as pd
from pathlib import Path
import shutil
import sys

# Add AlgoTrading root to path
algotrading_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(algotrading_root))

# Directories
RAW_DIR = algotrading_root / "nse_data" / "raw"
PROCESSED_DIR = algotrading_root / "nse_data" / "processed"

FYERS_INDICES = RAW_DIR / "fyers_indices"
FYERS_EQUITIES = RAW_DIR / "fyers_equities"
FYERS_INTRADAY = RAW_DIR / "fyers_intraday"
YAHOO_DIR = RAW_DIR / "yahoo"

NSE_INDICES = RAW_DIR / "indices"
NSE_EQUITIES = RAW_DIR / "equities"
INTRADAY_DIR = RAW_DIR / "intraday"  # New unified location

INDICES_CLEAN = PROCESSED_DIR / "indices_clean"
EQUITIES_CLEAN = PROCESSED_DIR / "equities_clean"


def merge_index_data():
    """Merge Fyers index data with existing NSE index Parquet"""
    print("\n" + "="*80)
    print("📊 MERGING INDEX DATA (Fyers + NSE)")
    print("="*80)
    
    # Read existing index Parquet
    index_parquet = INDICES_CLEAN / "index_ohlcv.parquet"
    
    if index_parquet.exists():
        print(f"📂 Reading existing index data: {index_parquet.name}")
        df_existing = pd.read_parquet(index_parquet)
        print(f"   ✅ Loaded {len(df_existing):,} rows")
    else:
        print(f"⚠️  No existing index data found, creating new")
        df_existing = pd.DataFrame()
    
    # Read Fyers index CSVs
    fyers_files = list(FYERS_INDICES.glob("*.csv"))
    print(f"\n📂 Found {len(fyers_files)} Fyers index files")
    
    all_new_data = []
    
    for csv_file in fyers_files:
        index_name = csv_file.stem  # NIFTY50 or BANKNIFTY
        print(f"   Reading {csv_file.name}...")
        
        df = pd.read_csv(csv_file)
        df['date'] = pd.to_datetime(df['date'])
        df['index'] = index_name
        
        # Standardize columns
        df = df.rename(columns={
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume'
        })
        
        df = df[['date', 'index', 'open', 'high', 'low', 'close', 'volume']]
        all_new_data.append(df)
        print(f"      ✅ {len(df)} rows")
    
    if all_new_data:
        df_fyers = pd.concat(all_new_data, ignore_index=True)
        
        # Merge with existing
        if not df_existing.empty:
            # Ensure date column is datetime in existing data
            if 'trade_date' in df_existing.columns:
                df_existing = df_existing.rename(columns={'trade_date': 'date'})
            df_existing['date'] = pd.to_datetime(df_existing['date'])
            
            # Combine
            df_combined = pd.concat([df_existing, df_fyers], ignore_index=True)
        else:
            df_combined = df_fyers
        
        # Remove duplicates (keep latest)
        print(f"\n🧹 Removing duplicates...")
        before = len(df_combined)
        df_combined = df_combined.drop_duplicates(subset=['date', 'index'], keep='last')
        after = len(df_combined)
        print(f"   ✅ Removed {before - after:,} duplicates")
        
        # Sort
        df_combined = df_combined.sort_values(['index', 'date']).reset_index(drop=True)
        
        # Save
        print(f"\n💾 Saving merged index data...")
        df_combined.to_parquet(index_parquet, index=False)
        print(f"   ✅ Saved {len(df_combined):,} rows to {index_parquet.name}")
        
        return True
    
    return False


def move_intraday_data():
    """Move Fyers intraday data to unified location"""
    print("\n" + "="*80)
    print("📁 ORGANIZING INTRADAY DATA")
    print("="*80)
    
    # Create intraday directory if it doesn't exist
    INTRADAY_DIR.mkdir(parents=True, exist_ok=True)
    
    # Move complete files from fyers_intraday to intraday
    complete_files = list(FYERS_INTRADAY.glob("*_complete.csv"))
    
    print(f"📂 Moving {len(complete_files)} intraday files to unified location")
    
    for file in complete_files:
        dest = INTRADAY_DIR / file.name
        print(f"   Moving {file.name}...")
        shutil.move(str(file), str(dest))
        print(f"      ✅ Moved to {INTRADAY_DIR.name}/")
    
    # Delete fyers_intraday folder if empty
    if FYERS_INTRADAY.exists():
        remaining = list(FYERS_INTRADAY.glob("*"))
        if not remaining:
            print(f"\n🗑️  Deleting empty folder: {FYERS_INTRADAY.name}")
            FYERS_INTRADAY.rmdir()
            print(f"   ✅ Deleted")
        else:
            print(f"\n⚠️  {FYERS_INTRADAY.name} still has {len(remaining)} files, not deleting")
    
    return True


def delete_yahoo_data():
    """Delete Yahoo data folder"""
    print("\n" + "="*80)
    print("🗑️  DELETING YAHOO DATA")
    print("="*80)
    
    if YAHOO_DIR.exists():
        print(f"📂 Deleting {YAHOO_DIR}...")
        shutil.rmtree(YAHOO_DIR)
        print(f"   ✅ Deleted Yahoo data folder")
        return True
    else:
        print(f"⚠️  Yahoo folder not found, skipping")
        return False


def consolidate_equity_folders():
    """Merge fyers_equities into equities folder"""
    print("\n" + "="*80)
    print("📁 CONSOLIDATING EQUITY FOLDERS")
    print("="*80)
    
    # Create equities folder if it doesn't exist
    NSE_EQUITIES.mkdir(parents=True, exist_ok=True)
    
    if FYERS_EQUITIES.exists():
        fyers_files = list(FYERS_EQUITIES.glob("*.csv"))
        print(f"📂 Moving {len(fyers_files)} equity files from fyers_equities to equities")
        
        moved_count = 0
        for file in fyers_files:
            dest = NSE_EQUITIES / file.name
            
            # If file exists in destination, keep the newer one
            if dest.exists():
                # Compare modification times
                if file.stat().st_mtime > dest.stat().st_mtime:
                    dest.unlink()
                    shutil.move(str(file), str(dest))
                    moved_count += 1
                else:
                    file.unlink()  # Delete older Fyers file
            else:
                shutil.move(str(file), str(dest))
                moved_count += 1
            
            if moved_count % 100 == 0:
                print(f"   Progress: {moved_count}/{len(fyers_files)} files")
        
        print(f"   ✅ Moved/merged {moved_count} files")
        
        # Delete fyers_equities folder if empty
        remaining = list(FYERS_EQUITIES.glob("*"))
        if not remaining:
            print(f"\n🗑️  Deleting empty folder: {FYERS_EQUITIES.name}")
            FYERS_EQUITIES.rmdir()
            print(f"   ✅ Deleted")
    
    return True


def consolidate_index_folders():
    """Merge fyers_indices into indices folder"""
    print("\n" + "="*80)
    print("📁 CONSOLIDATING INDEX FOLDERS")
    print("="*80)
    
    # Create indices folder if it doesn't exist
    NSE_INDICES.mkdir(parents=True, exist_ok=True)
    
    if FYERS_INDICES.exists():
        fyers_files = list(FYERS_INDICES.glob("*.csv"))
        print(f"📂 Moving {len(fyers_files)} index files from fyers_indices to indices")
        
        for file in fyers_files:
            dest = NSE_INDICES / file.name
            
            # If file exists, keep the newer one
            if dest.exists():
                if file.stat().st_mtime > dest.stat().st_mtime:
                    dest.unlink()
                    shutil.move(str(file), str(dest))
                else:
                    file.unlink()
            else:
                shutil.move(str(file), str(dest))
        
        print(f"   ✅ Moved {len(fyers_files)} files")
        
        # Delete fyers_indices folder if empty
        remaining = list(FYERS_INDICES.glob("*"))
        if not remaining:
            print(f"\n🗑️  Deleting empty folder: {FYERS_INDICES.name}")
            FYERS_INDICES.rmdir()
            print(f"   ✅ Deleted")
    
    return True


def print_final_structure():
    """Print final directory structure"""
    print("\n" + "="*80)
    print("📊 FINAL DATA STRUCTURE")
    print("="*80)
    
    print("\n📁 nse_data/raw/")
    for subdir in sorted(RAW_DIR.iterdir()):
        if subdir.is_dir():
            file_count = len(list(subdir.glob("*")))
            print(f"   ├── {subdir.name}/ ({file_count} files)")
    
    print("\n📁 nse_data/processed/")
    for subdir in sorted(PROCESSED_DIR.iterdir()):
        if subdir.is_dir():
            file_count = len(list(subdir.rglob("*")))
            print(f"   ├── {subdir.name}/ ({file_count} files)")


def main():
    """Main consolidation workflow"""
    print("\n" + "="*80)
    print("🔗 DATA CONSOLIDATION - MERGE & CLEANUP")
    print("="*80)
    print("This will:")
    print("  1. Merge Fyers index data with NSE Parquet files")
    print("  2. Move intraday data to unified location")
    print("  3. Consolidate equity folders (fyers_equities → equities)")
    print("  4. Consolidate index folders (fyers_indices → indices)")
    print("  5. Delete Yahoo data folder")
    print("  6. Remove empty Fyers folders")
    
    # Execute consolidation steps
    merge_index_data()
    move_intraday_data()
    consolidate_equity_folders()
    consolidate_index_folders()
    delete_yahoo_data()
    
    # Show final structure
    print_final_structure()
    
    print("\n" + "="*80)
    print("✅ CONSOLIDATION COMPLETE!")
    print("="*80)
    print("\n💡 Next Steps:")
    print("   1. All data is now in unified nse_data structure")
    print("   2. Daily data: nse_data/raw/equities/ and nse_data/raw/indices/")
    print("   3. Intraday data: nse_data/raw/intraday/")
    print("   4. Processed data: nse_data/processed/")
    print("   5. Yahoo data has been removed")
    print("   6. No duplicate folders remain")


if __name__ == "__main__":
    main()
