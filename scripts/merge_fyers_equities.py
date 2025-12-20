"""
Merge Fyers equities with existing equities
"""

import os
import shutil
from pathlib import Path

source_dir = Path("nse_data/raw/fyers_equities")
target_dir = Path("nse_data/raw/equities")

print("=" * 60)
print("MERGING FYERS EQUITIES WITH EQUITIES")
print("=" * 60)

if not source_dir.exists():
    print(f"❌ Source directory not found: {source_dir}")
    exit(1)

if not target_dir.exists():
    target_dir.mkdir(parents=True)
    print(f"✅ Created target directory: {target_dir}")

# Get all CSV files from fyers_equities
fyers_files = list(source_dir.glob("*.csv"))
print(f"\nFound {len(fyers_files)} Fyers equity files")

merged_count = 0
skipped_count = 0
updated_count = 0

for fyers_file in fyers_files:
    target_file = target_dir / fyers_file.name
    
    if target_file.exists():
        # File exists - check if we should update
        fyers_size = fyers_file.stat().st_size
        target_size = target_file.stat().st_size
        
        if fyers_size > target_size:
            # Fyers file is larger, replace it
            shutil.copy2(fyers_file, target_file)
            updated_count += 1
            if updated_count % 100 == 0:
                print(f"Updated {updated_count} files...")
        else:
            skipped_count += 1
    else:
        # New file, copy it
        shutil.copy2(fyers_file, target_file)
        merged_count += 1
        if merged_count % 100 == 0:
            print(f"Merged {merged_count} new files...")

print("\n" + "=" * 60)
print("MERGE COMPLETE")
print("=" * 60)
print(f"✅ New files merged: {merged_count}")
print(f"✅ Existing files updated: {updated_count}")
print(f"ℹ️  Files skipped (already up-to-date): {skipped_count}")
print(f"\n📊 Total equity files: {len(list(target_dir.glob('*.csv')))}")
