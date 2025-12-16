"""
NSE Data Validation Script

Validates the cleaned equity OHLCV parquet files using DuckDB.
Provides quick statistics on record count, date range, and data quality.
"""

import duckdb

def validate_equity_data():
    """Validate cleaned equity data using DuckDB"""
    
    con = duckdb.connect()
    
    print("=" * 60)
    print("NSE Equity Data Validation Report")
    print("=" * 60)
    
    # Basic statistics
    print("\n📊 Dataset Statistics:")
    result = con.execute("""
        SELECT 
            COUNT(*) as total_records,
            MIN(trade_date) as earliest_date,
            MAX(trade_date) as latest_date,
            COUNT(DISTINCT symbol) as unique_symbols,
            COUNT(DISTINCT trade_date) as trading_days
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
    """).fetchall()
    
    if result:
        stats = result[0]
        print(f"  Total Records:    {stats[0]:,}")
        print(f"  Date Range:       {stats[1]} to {stats[2]}")
        print(f"  Unique Symbols:   {stats[3]:,}")
        print(f"  Trading Days:     {stats[4]:,}")
    
    # Records per year
    print("\n📅 Records by Year:")
    year_stats = con.execute("""
        SELECT 
            year,
            COUNT(*) as records,
            COUNT(DISTINCT symbol) as symbols,
            COUNT(DISTINCT trade_date) as days
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
        GROUP BY year
        ORDER BY year
    """).fetchall()
    
    for row in year_stats:
        print(f"  {row[0]}: {row[1]:>10,} records | {row[2]:>5,} symbols | {row[3]:>3} days")
    
    # Data quality checks
    print("\n✅ Data Quality Checks:")
    
    # Check for nulls
    null_check = con.execute("""
        SELECT 
            SUM(CASE WHEN open IS NULL THEN 1 ELSE 0 END) as null_open,
            SUM(CASE WHEN high IS NULL THEN 1 ELSE 0 END) as null_high,
            SUM(CASE WHEN low IS NULL THEN 1 ELSE 0 END) as null_low,
            SUM(CASE WHEN close IS NULL THEN 1 ELSE 0 END) as null_close,
            SUM(CASE WHEN volume IS NULL THEN 1 ELSE 0 END) as null_volume
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
    """).fetchall()[0]
    
    print(f"  Null Values:")
    print(f"    Open:   {null_check[0]:,}")
    print(f"    High:   {null_check[1]:,}")
    print(f"    Low:    {null_check[2]:,}")
    print(f"    Close:  {null_check[3]:,}")
    print(f"    Volume: {null_check[4]:,}")
    
    # Check for invalid prices (high < low, etc.)
    invalid_prices = con.execute("""
        SELECT COUNT(*)
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
        WHERE high < low OR close > high OR close < low OR open > high OR open < low
    """).fetchall()[0][0]
    
    print(f"\n  Invalid Price Records: {invalid_prices:,}")
    
    # Top 10 most traded symbols
    print("\n🔝 Top 10 Most Traded Symbols (by record count):")
    top_symbols = con.execute("""
        SELECT 
            symbol,
            COUNT(*) as records,
            MIN(trade_date) as first_date,
            MAX(trade_date) as last_date
        FROM read_parquet('nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet')
        GROUP BY symbol
        ORDER BY records DESC
        LIMIT 10
    """).fetchall()
    
    for i, row in enumerate(top_symbols, 1):
        print(f"  {i:2}. {row[0]:<15} {row[1]:>6,} records ({row[2]} to {row[3]})")
    
    print("\n" + "=" * 60)
    print("✅ Validation Complete!")
    print("=" * 60)
    
    con.close()


if __name__ == "__main__":
    validate_equity_data()
