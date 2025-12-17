"""
Quick script to convert NSE Parquet file to remove categorical types
This fixes the concat issue in yahoo_data_cleaner.py
"""
import pandas as pd
from pathlib import Path

parquet_file = Path("nse_data/processed/equities_clean/equity_ohlcv.parquet")

print(f"Reading {parquet_file}...")
df = pd.read_parquet(parquet_file)

print(f"Original dtypes:")
print(df.dtypes)
print(f"\nRows: {len(df)}")

# Convert all categorical columns to appropriate types
for col in df.select_dtypes(include=['category']).columns:
    print(f"Converting {col} from category to string...")
    df[col] = df[col].astype(str)

# Convert trade_date to datetime if it's not already
if df['trade_date'].dtype == 'object':
    print("Converting trade_date to datetime...")
    df['trade_date'] = pd.to_datetime(df['trade_date'])

print(f"\nNew dtypes:")
print(df.dtypes)

# Save back
print(f"\nSaving back to {parquet_file}...")
df.to_parquet(parquet_file, index=False, engine='pyarrow')

print("✓ Done! Categorical types removed.")
