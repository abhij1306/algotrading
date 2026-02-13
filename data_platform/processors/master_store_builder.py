"""
Merge Fyers equity data (2024) with NSE equity data (2016-2023)
Combines data within each CSV file, not just copying files
"""

import pandas as pd
from pathlib import Path
from datetime import datetime

source_dir = Path("nse_data/raw/fyers_equities")
target_dir = Path("nse_data/raw/equities")

print("=" * 60)
print("MERGING FYERS DATA INTO NSE EQUITY FILES")
print("=" * 60)

if not source_dir.exists():
    print(f"❌ Source directory not found: {source_dir}")
    exit(1)

target_dir.mkdir(parents=True, exist_ok=True)

fyers_files = list(source_dir.glob("*.csv"))
print(f"\nFound {len(fyers_files)} Fyers equity files")

merged_count = 0
new_files_count = 0
updated_count = 0
error_count = 0

for i, fyers_file in enumerate(fyers_files):
    try:
        target_file = target_dir / fyers_file.name
        
        # Read Fyers data
        fyers_df = pd.read_csv(fyers_file)
        
        if target_file.exists():
            # Read existing NSE data
            nse_df = pd.read_csv(target_file)
            
            # Combine and remove duplicates
            combined_df = pd.concat([nse_df, fyers_df], ignore_index=True)
            
            # Remove duplicates based on date (assuming there's a date column)
            if 'Date' in combined_df.columns:
                combined_df['Date'] = pd.to_datetime(combined_df['Date'])
                combined_df = combined_df.drop_duplicates(subset=['Date'], keep='last')
                combined_df = combined_df.sort_values('Date')
            elif 'date' in combined_df.columns:
                combined_df['date'] = pd.to_datetime(combined_df['date'])
                combined_df = combined_df.drop_duplicates(subset=['date'], keep='last')
                combined_df = combined_df.sort_values('date')
            
            # Save merged data
            combined_df.to_csv(target_file, index=False)
            merged_count += 1
            
        else:
            # New file, just copy
            fyers_df.to_csv(target_file, index=False)
            new_files_count += 1
        
        if (merged_count + new_files_count) % 100 == 0:
            print(f"Processed {merged_count + new_files_count}/{len(fyers_files)} files...")
            
    except Exception as e:
        error_count += 1
        if error_count < 10:  # Only show first 10 errors
            print(f"⚠️  Error processing {fyers_file.name}: {e}")

print("\n" + "=" * 60)
print("MERGE COMPLETE")
print("=" * 60)
print(f"✅ Files merged (data combined): {merged_count}")
print(f"✅ New files created: {new_files_count}")
print(f"❌ Errors: {error_count}")
print(f"\n📊 Total equity files: {len(list(target_dir.glob('*.csv')))}")
