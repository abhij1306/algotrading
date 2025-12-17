"""
Corporate Actions Price Adjustment - Production Safe Implementation
Applies backward adjustments for splits and bonuses only
"""

import pandas as pd
import re
import glob
from pathlib import Path
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from backend.app.database import SessionLocal
from sqlalchemy import text

print("=" * 70)
print("CORPORATE ACTIONS PRICE ADJUSTMENT")
print("=" * 70)

# ============================================================
# STEP 1: SANITY CHECK THE FILE
# ============================================================
print("\nStep 1: Sanity Check")
print("-" * 70)

files = glob.glob('CF-CA-equities*.csv')
if not files:
    print("❌ No CF-CA-equities file found")
    sys.exit(1)

ca_file = sorted(files, key=os.path.getmtime, reverse=True)[0]
print(f"File: {ca_file}")

df = pd.read_csv(ca_file)
print(f"Rows: {len(df)}")
print(f"Columns: {df.columns.tolist()}")

# Check required columns
required = ['SYMBOL', 'EX-DATE', 'PURPOSE']
missing = [col for col in required if col not in df.columns]

if missing:
    print(f"❌ Missing columns: {missing}")
    sys.exit(1)

print(f"✅ All required columns present")

# ============================================================
# STEP 2 & 3: FILTER AND PARSE ADJUSTMENT FACTORS
# ============================================================
print("\nStep 2 & 3: Filter and Parse Adjustment Factors")
print("-" * 70)

def parse_factor(purpose):
    """
    Parse adjustment factor from PURPOSE string
    Returns: (action_type, factor) or (None, None)
    """
    if pd.isna(purpose):
        return None, None
    
    p = str(purpose).lower()
    
    # Split: "Split from FV 10 to FV 2" → factor = 10/2 = 5
    if "split" in p and "fv" in p:
        nums = list(map(int, re.findall(r"\d+", p)))
        if len(nums) >= 2:
            return "split", nums[0] / nums[1]
    
    # Bonus: "Bonus 1:1" → factor = (1+1)/1 = 2
    m = re.search(r"bonus.*?(\d+)\s*:\s*(\d+)", p)
    if m:
        a, b = map(int, m.groups())
        return "bonus", (a + b) / b
    
    return None, None

# Apply parsing
df[["action_type", "factor"]] = df["PURPOSE"].apply(
    lambda x: pd.Series(parse_factor(x))
)

# Filter to only valid adjustments
df_filtered = df.dropna(subset=["factor"]).copy()

print(f"Original rows: {len(df)}")
print(f"After filtering: {len(df_filtered)}")
print(f"  - Splits: {(df_filtered['action_type'] == 'split').sum()}")
print(f"  - Bonuses: {(df_filtered['action_type'] == 'bonus').sum()}")
print(f"Ignored: {len(df) - len(df_filtered)} rows (dividends, rights, etc.)")

# Convert EX-DATE
df_filtered["EX-DATE"] = pd.to_datetime(df_filtered["EX-DATE"], dayfirst=True, errors='coerce').dt.date

# Remove rows with invalid dates
df_filtered = df_filtered.dropna(subset=["EX-DATE"])

print(f"After date conversion: {len(df_filtered)} rows")

# ============================================================
# STEP 4: STORE IN DATABASE
# ============================================================
print("\nStep 4: Store in Database")
print("-" * 70)

db = SessionLocal()

try:
    # Clear and recreate table structure
    db.execute(text("DROP TABLE IF EXISTS corporate_actions CASCADE"))
    db.execute(text("""
        CREATE TABLE corporate_actions (
            symbol TEXT,
            ex_date DATE,
            action_type TEXT,
            factor DOUBLE PRECISION,
            purpose TEXT,
            PRIMARY KEY (symbol, ex_date, action_type)
        )
    """))
    db.commit()
    print("✅ Created corporate_actions table")
    
    # Insert data
    inserted = 0
    for _, row in df_filtered.iterrows():
        try:
            db.execute(text("""
                INSERT INTO corporate_actions (symbol, ex_date, action_type, factor, purpose)
                VALUES (:symbol, :ex_date, :action_type, :factor, :purpose)
                ON CONFLICT (symbol, ex_date, action_type) DO NOTHING
            """), {
                'symbol': str(row['SYMBOL']).strip(),
                'ex_date': row['EX-DATE'],
                'action_type': row['action_type'],
                'factor': float(row['factor']),
                'purpose': str(row['PURPOSE'])[:200]  # Truncate if too long
            })
            inserted += 1
            
            if inserted % 100 == 0:
                print(f"  Inserted {inserted} records...")
        except Exception as e:
            continue
    
    db.commit()
    print(f"✅ Inserted {inserted} corporate actions")
    
except Exception as e:
    print(f"❌ Database error: {e}")
    import traceback
    traceback.print_exc()
    db.rollback()
finally:
    db.close()

# ============================================================
# STEP 5 & 6: APPLY ADJUSTMENTS AND SAVE
# ============================================================
print("\nStep 5 & 6: Apply Adjustments")
print("-" * 70)

def adjust_prices(price_df, ca_df):
    """
    Apply backward adjustments to prices
    CRITICAL: Adjust BEFORE ex-date only
    """
    price_df = price_df.sort_values("trade_date").copy()
    
    # Ensure trade_date is date type for comparison
    if price_df['trade_date'].dtype != 'object':
        price_df['trade_date'] = pd.to_datetime(price_df['trade_date']).dt.date
    
    # Ensure numeric columns are float for operations
    price_df[["open", "high", "low", "close", "volume"]] = price_df[["open", "high", "low", "close", "volume"]].astype(float)
    
    for _, row in ca_df.iterrows():
        mask = price_df["trade_date"] < row["ex_date"]
        f = row["factor"]
        
        # Prices: divide by factor
        price_df.loc[mask, ["open", "high", "low", "close"]] /= f
        
        # Volume: multiply by factor
        price_df.loc[mask, "volume"] *= f
    
    return price_df

# Read raw equity data
print("Reading raw equity data...")
equity_file = Path("nse_data/processed/equities_clean/equity_ohlcv.parquet")

if not equity_file.exists():
    print(f"❌ Raw equity file not found: {equity_file}")
    sys.exit(1)

import duckdb

con = duckdb.connect(':memory:')
equity_df = con.execute(f"SELECT * FROM read_parquet('{equity_file}')").df()
con.close()

print(f"✅ Loaded {len(equity_df)} raw price records")
print(f"   Symbols: {equity_df['symbol'].nunique()}")
print(f"   Date range: {equity_df['trade_date'].min()} to {equity_df['trade_date'].max()}")

# Get corporate actions from database
db = SessionLocal()
ca_data = pd.read_sql(
    "SELECT symbol, ex_date, action_type, factor FROM corporate_actions ORDER BY symbol, ex_date",
    db.connection()
)
db.close()

print(f"✅ Loaded {len(ca_data)} corporate actions from database")

# Apply adjustments symbol by symbol
print("\nApplying adjustments...")

adjusted_dfs = []
symbols_with_ca = ca_data['symbol'].unique()

processed = 0
for symbol in symbols_with_ca:
    # Get price data for this symbol
    symbol_prices = equity_df[equity_df['symbol'] == symbol].copy()
    
    if len(symbol_prices) == 0:
        continue
    
    # Get CA data for this symbol
    symbol_ca = ca_data[ca_data['symbol'] == symbol]
    
    # Apply adjustments
    adjusted = adjust_prices(symbol_prices, symbol_ca)
    adjusted_dfs.append(adjusted)
    
    processed += 1
    if processed % 50 == 0:
        print(f"  Processed {processed}/{len(symbols_with_ca)} symbols...")

# Combine all adjusted data
if adjusted_dfs:
    adjusted_all = pd.concat(adjusted_dfs, ignore_index=True)
    print(f"✅ Adjusted {len(adjusted_all)} price records for {len(symbols_with_ca)} symbols")
else:
    print("⚠️  No adjustments applied")
    adjusted_all = pd.DataFrame()

# Add unadjusted symbols
symbols_without_ca = set(equity_df['symbol'].unique()) - set(symbols_with_ca)
if symbols_without_ca:
    unadjusted = equity_df[equity_df['symbol'].isin(symbols_without_ca)]
    adjusted_all = pd.concat([adjusted_all, unadjusted], ignore_index=True)
    print(f"✅ Added {len(symbols_without_ca)} symbols without corporate actions")

# Save to separate directory
output_dir = Path("nse_data/processed/equities_adjusted")
output_dir.mkdir(parents=True, exist_ok=True)

output_file = output_dir / "equity_ohlcv_adj.parquet"
adjusted_all.to_parquet(output_file, index=False)

print(f"✅ Saved adjusted prices to {output_file}")
print(f"   Total records: {len(adjusted_all)}")
print(f"   File size: {output_file.stat().st_size / (1024*1024):.2f} MB")

# ============================================================
# STEP 7: VALIDATE
# ============================================================
print("\nStep 7: Validation")
print("-" * 70)

test_symbols = ['RELIANCE', 'INFY', 'TCS']

for symbol in test_symbols:
    print(f"\n{symbol}:")
    
    # Check if symbol has corporate actions
    symbol_ca = ca_data[ca_data['symbol'] == symbol]
    
    if len(symbol_ca) > 0:
        print(f"  Corporate actions: {len(symbol_ca)}")
        for _, ca in symbol_ca.iterrows():
            print(f"    - {ca['action_type']}: factor={ca['factor']:.2f}, ex_date={ca['ex_date']}")
        
        # Check price continuity around ex-dates
        symbol_adj = adjusted_all[adjusted_all['symbol'] == symbol].sort_values('trade_date')
        
        if len(symbol_adj) > 0:
            print(f"  Price range: ₹{symbol_adj['close'].min():.2f} - ₹{symbol_adj['close'].max():.2f}")
            
            # Check for sudden drops (>50% in one day)
            symbol_adj['pct_change'] = symbol_adj['close'].pct_change() * 100
            large_drops = symbol_adj[symbol_adj['pct_change'] < -50]
            
            if len(large_drops) > 0:
                print(f"  ⚠️  Found {len(large_drops)} days with >50% drop - may need review")
            else:
                print(f"  ✅ No sudden large drops detected")
    else:
        print(f"  No corporate actions")

print("\n" + "=" * 70)
print("CORPORATE ACTIONS ADJUSTMENT COMPLETE!")
print("=" * 70)
print(f"\n✅ Processed {len(symbols_with_ca)} symbols with corporate actions")
print(f"✅ Saved adjusted prices to: {output_file}")
print(f"\n📝 Usage:")
print(f"   - Backtesting: Use {output_file}")
print(f"   - Live trading: Use raw prices from equities_clean/")
print("=" * 70)
