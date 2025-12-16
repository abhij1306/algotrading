"""
NSE Index Data Validation Script

Validates the cleaned index OHLC parquet files using DuckDB.
"""

import duckdb

def validate_index_data():
    """Validate cleaned index data using DuckDB"""
    
    con = duckdb.connect()
    
    print("=" * 60)
    print("NSE Index Data Validation Report")
    print("=" * 60)
    
    # Index statistics
    print("\n📊 Index Statistics:")
    result = con.execute("""
        SELECT 
            index,
            MIN(trade_date) as earliest_date,
            MAX(trade_date) as latest_date,
            COUNT(*) as total_records
        FROM read_parquet('nse_data/processed/indices_clean/index=*/index_ohlc.parquet')
        GROUP BY index
        ORDER BY index
    """).fetchall()
    
    for row in result:
        print(f"\n  {row[0].upper()}:")
        print(f"    Date Range: {row[1]} to {row[2]}")
        print(f"    Records:    {row[3]:,}")
    
    # Overall statistics
    print("\n📈 Overall Statistics:")
    overall = con.execute("""
        SELECT 
            COUNT(*) as total_records,
            COUNT(DISTINCT index) as total_indices,
            MIN(trade_date) as earliest_date,
            MAX(trade_date) as latest_date
        FROM read_parquet('nse_data/processed/indices_clean/index=*/index_ohlc.parquet')
    """).fetchall()[0]
    
    print(f"  Total Records:  {overall[0]:,}")
    print(f"  Total Indices:  {overall[1]}")
    print(f"  Date Range:     {overall[2]} to {overall[3]}")
    
    print("\n" + "=" * 60)
    print("✅ Validation Complete!")
    print("=" * 60)
    
    con.close()


if __name__ == "__main__":
    validate_index_data()
