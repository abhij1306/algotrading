"""
Adjusted Price Validation Script

Validates adjusted equity prices using DuckDB.
Checks for price continuity around corporate action dates.
"""

import duckdb

def validate_adjusted_prices():
    """Validate adjusted equity prices"""
    
    con = duckdb.connect()
    
    print("=" * 60)
    print("Adjusted Equity Price Validation")
    print("=" * 60)
    
    # Basic statistics
    print("\n📊 Dataset Statistics:")
    result = con.execute("""
        SELECT 
            COUNT(*) as total_records,
            MIN(trade_date) as earliest_date,
            MAX(trade_date) as latest_date,
            COUNT(DISTINCT symbol) as unique_symbols
        FROM read_parquet('nse_data/processed/equities_adjusted/year=*/equity_ohlcv_adj.parquet')
    """).fetchall()
    
    if result:
        stats = result[0]
        print(f"  Total Records:    {stats[0]:,}")
        print(f"  Date Range:       {stats[1]} to {stats[2]}")
        print(f"  Unique Symbols:   {stats[3]:,}")
    
    # Sample: Check RELIANCE price continuity
    print("\n📈 Sample: RELIANCE Price History (Last 100 Days):")
    reliance = con.execute("""
        SELECT trade_date, close
        FROM read_parquet('nse_data/processed/equities_adjusted/year=*/equity_ohlcv_adj.parquet')
        WHERE symbol = 'RELIANCE'
        ORDER BY trade_date DESC
        LIMIT 100
    """).fetchall()
    
    if reliance:
        print(f"  Latest Close: ₹{reliance[0][1]:.2f} on {reliance[0][0]}")
        print(f"  100 Days Ago: ₹{reliance[-1][1]:.2f} on {reliance[-1][0]}")
    
    # Check for suspicious price drops (potential unadjusted splits)
    print("\n⚠️  Checking for Suspicious Price Drops (>50% in one day):")
    drops = con.execute("""
        WITH daily_changes AS (
            SELECT 
                symbol,
                trade_date,
                close,
                LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) as prev_close,
                (close - LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date)) / 
                LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) * 100 as pct_change
            FROM read_parquet('nse_data/processed/equities_adjusted/year=*/equity_ohlcv_adj.parquet')
        )
        SELECT symbol, trade_date, prev_close, close, pct_change
        FROM daily_changes
        WHERE pct_change < -50
        ORDER BY pct_change
        LIMIT 10
    """).fetchall()
    
    if drops:
        print(f"  Found {len(drops)} instances:")
        for row in drops[:5]:
            print(f"    {row[0]}: {row[1]} - {row[4]:.1f}% drop (₹{row[2]:.2f} → ₹{row[3]:.2f})")
    else:
        print("  ✓ No suspicious drops found (good sign!)")
    
    # Compare raw vs adjusted for a sample symbol
    print("\n🔍 Raw vs Adjusted Comparison (Sample: TCS):")
    
    raw = con.execute("""
        SELECT AVG(close) as avg_close
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
        WHERE symbol = 'TCS'
    """).fetchall()
    
    adj = con.execute("""
        SELECT AVG(close) as avg_close
        FROM read_parquet('nse_data/processed/equities_adjusted/year=*/equity_ohlcv_adj.parquet')
        WHERE symbol = 'TCS'
    """).fetchall()
    
    if raw and adj:
        print(f"  Raw Average:      ₹{raw[0][0]:.2f}")
        print(f"  Adjusted Average: ₹{adj[0][0]:.2f}")
        print(f"  Difference:       {((adj[0][0] - raw[0][0]) / raw[0][0] * 100):.2f}%")
    
    print("\n" + "=" * 60)
    print("✅ Validation Complete!")
    print("=" * 60)
    print("\n💡 Tip: Run this query to check any specific symbol:")
    print("   SELECT * FROM read_parquet('...') WHERE symbol = 'YOURSTOCK' ORDER BY trade_date")
    
    con.close()


if __name__ == "__main__":
    validate_adjusted_prices()
