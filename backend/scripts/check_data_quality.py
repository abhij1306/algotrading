"""
Data Quality Check Script
Run periodic checks to ensure data integrity
"""
from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
import pandas as pd

load_dotenv()

DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/smarttrader")
engine = create_engine(DB_URL)

def check_null_indicators():
    """Check for NULL values in indicator columns"""
    print("🔍 Checking for NULL indicators...")
    
    query = text("""
        SELECT 
            COUNT(*) as total_rows,
            SUM(CASE WHEN rsi_14 IS NULL THEN 1 ELSE 0 END) as null_rsi,
            SUM(CASE WHEN ema_20 IS NULL THEN 1 ELSE 0 END) as null_ema20,
            SUM(CASE WHEN macd IS NULL THEN 1 ELSE 0 END) as null_macd,
            SUM(CASE WHEN adx IS NULL THEN 1 ELSE 0 END) as null_adx
        FROM historical_prices
        WHERE date >= CURRENT_DATE - INTERVAL '90 days';
    """)
    
    with engine.connect() as conn:
        result = conn.execute(query).fetchone()
        
    total, null_rsi, null_ema20, null_macd, null_adx = result
    
    print(f"  Total rows (last 90 days): {total}")
    print(f"  NULL RSI: {null_rsi} ({null_rsi/total*100:.2f}%)")
    print(f"  NULL EMA20: {null_ema20} ({null_ema20/total*100:.2f}%)")
    print(f"  NULL MACD: {null_macd} ({null_macd/total*100:.2f}%)")
    print(f"  NULL ADX: {null_adx} ({null_adx/total*100:.2f}%)")
    
    if null_rsi > total * 0.1:
        print("  ⚠️  WARNING: More than 10% of rows have NULL RSI")
    else:
        print("  ✅ NULL indicator check passed")


def check_missing_dates():
    """Check for gaps in historical data"""
    print("\n🔍 Checking for missing trading dates...")
    
    query = text("""
        SELECT 
            symbol,
            COUNT(DISTINCT date) as days_count
        FROM historical_prices
        WHERE date >= CURRENT_DATE - INTERVAL '90 days'
        GROUP BY symbol
        HAVING COUNT(DISTINCT date) < 60
        ORDER BY days_count ASC
        LIMIT 10;
    """)
    
    with engine.connect() as conn:
        result = conn.execute(query).fetchall()
    
    if result:
        print("  ⚠️  Symbols with insufficient data (< 60 days in last 90):")
        for row in result:
            print(f"    - {row[0]}: {row[1]} days")
    else:
        print("  ✅ All symbols have sufficient data")


def check_price_anomalies():
    """Check for suspicious price movements"""
    print("\n🔍 Checking for price anomalies...")
    
    query = text("""
        SELECT 
            symbol,
            date,
            close,
            LAG(close) OVER (PARTITION BY symbol ORDER BY date) as prev_close,
            ((close - LAG(close) OVER (PARTITION BY symbol ORDER BY date)) / 
             LAG(close) OVER (PARTITION BY symbol ORDER BY date) * 100) as pct_change
        FROM historical_prices
        WHERE date >= CURRENT_DATE - INTERVAL '30 days'
    """)
    
    with engine.connect() as conn:
        df = pd.read_sql(query, conn)
    
    # Find extreme moves (>20%)
    anomalies = df[abs(df['pct_change']) > 20]
    
    if len(anomalies) > 0:
        print(f"  ⚠️  Found {len(anomalies)} extreme price movements (>20%)")
        print(anomalies[['symbol', 'date', 'prev_close', 'close', 'pct_change']].head())
    else:
        print("  ✅ No extreme price anomalies found")


def check_orphaned_records():
    """Check for orphaned records (FK violations if constraints not added)"""
    print("\n🔍 Checking for potential orphaned records...")
    
    # Check backtest_runs without strategy_metadata
    query = text("""
        SELECT COUNT(*) 
        FROM backtest_runs br
        LEFT JOIN strategy_metadata sm ON br.strategy_id = sm.strategy_id
        WHERE sm.strategy_id IS NULL;
    """)
    
    try:
        with engine.connect() as conn:
            count = conn.execute(query).scalar()
        
        if count > 0:
            print(f"  ⚠️  Found {count} backtest runs with invalid strategy_id")
        else:
            print("  ✅ No orphaned backtest runs")
    except Exception as e:
        print(f"  ⚠️  Could not check (tables may not exist): {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("SmartTrader 3.0 - Data Quality Check")
    print("=" * 60)
    
    try:
        check_null_indicators()
        check_missing_dates()
        check_price_anomalies()
        check_orphaned_records()
        
        print("\n✅ Data quality check complete!")
    except Exception as e:
        print(f"\n❌ Data quality check failed: {e}")
