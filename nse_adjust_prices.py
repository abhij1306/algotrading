import re
import os
import duckdb
import pandas as pd
import psycopg2

RAW_PARQUET = "nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet"
OUT_ROOT = "nse_data/processed/equities_adjusted"
os.makedirs(OUT_ROOT, exist_ok=True)

PG_CONN = {
    "host": "localhost",
    "dbname": "algotrading",
    "user": "postgres",
    "password": "postgres",
    "port": 5432
}

# ------------------------------------------------
# Corporate action factor extraction
# ------------------------------------------------
def parse_factor(purpose):
    """Extract adjustment factor from corporate action purpose text"""
    p = purpose.lower()

    # Split: "Split from FV 10 to FV 2"
    m = re.search(r"fv\s*\d+\s*to\s*fv\s*(\d+)", p)
    if m:
        nums = re.findall(r"\d+", p)
        if len(nums) >= 2:
            old, new = int(nums[0]), int(nums[1])
            return old / new

    # Bonus: "Bonus 1:1", "Bonus Issue 2:1"
    m = re.search(r"bonus.*?(\d+)\s*:\s*(\d+)", p)
    if m:
        a, b = map(int, m.groups())
        return (a + b) / b

    return None


def load_corporate_actions():
    """Load corporate actions from Postgres"""
    conn = psycopg2.connect(**PG_CONN)
    df = pd.read_sql("""
        SELECT symbol, ex_date, purpose
        FROM corporate_actions
        ORDER BY ex_date
    """, conn)
    conn.close()

    df["factor"] = df["purpose"].apply(parse_factor)
    df = df.dropna(subset=["factor"])
    
    print(f"Loaded {len(df)} corporate actions with valid factors")
    return df


def adjust_symbol(df, ca_df):
    """Apply backward adjustment to a single symbol's price history"""
    df = df.sort_values("trade_date").copy()

    for _, row in ca_df.iterrows():
        mask = df["trade_date"] < row["ex_date"]
        factor = row["factor"]

        # Backward adjustment: prices before ex-date are divided by factor
        df.loc[mask, ["open", "high", "low", "close"]] /= factor
        # Volume is multiplied by factor to maintain consistency
        df.loc[mask, "volume"] *= factor

    return df


def run():
    """Main adjustment pipeline"""
    print("=" * 60)
    print("NSE Equity Price Adjustment Pipeline")
    print("=" * 60)
    
    # Load equity data
    print("\n1. Loading equity OHLCV data...")
    con = duckdb.connect()
    eq = con.execute(f"SELECT * FROM read_parquet('{RAW_PARQUET}')").df()
    print(f"   Loaded {len(eq):,} records for {eq['symbol'].nunique():,} symbols")

    # Load corporate actions
    print("\n2. Loading corporate actions...")
    ca = load_corporate_actions()

    # Apply adjustments
    print("\n3. Applying adjustments...")
    out = []
    symbols_adjusted = 0

    for symbol, g in eq.groupby("symbol"):
        ca_sym = ca[ca["symbol"] == symbol]
        if not ca_sym.empty:
            g = adjust_symbol(g, ca_sym)
            symbols_adjusted += 1
        out.append(g)

    print(f"   Adjusted {symbols_adjusted:,} symbols with corporate actions")

    # Consolidate and save
    print("\n4. Saving adjusted data...")
    final = pd.concat(out, ignore_index=True)
    final["year"] = pd.to_datetime(final["trade_date"]).dt.year

    for year, g in final.groupby("year"):
        out_dir = os.path.join(OUT_ROOT, f"year={year}")
        os.makedirs(out_dir, exist_ok=True)

        g.drop_duplicates(
            subset=["symbol", "trade_date"],
            inplace=True
        )

        out_file = os.path.join(out_dir, "equity_ohlcv_adj.parquet")
        g.to_parquet(
            out_file,
            engine="pyarrow",
            compression="snappy",
            index=False
        )
        
        print(f"   Saved {year}: {len(g):,} records")

    print("\n" + "=" * 60)
    print("✅ Adjustment Complete!")
    print("=" * 60)


if __name__ == "__main__":
    run()
