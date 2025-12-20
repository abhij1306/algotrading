"""
Full NSE Equity Ingestion Script - Robust Version
Ingests historical data from Parquet files into PostgreSQL with retries.
"""
import pandas as pd
from sqlalchemy import create_engine, MetaData, Table, text
from sqlalchemy.orm import sessionmaker
from pathlib import Path
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(ROOT_DIR))

from backend.app.database import engine, Company, HistoricalPrice, SessionLocal

def ingest_data():
    parquet_path = ROOT_DIR / 'nse_data' / 'processed' / 'equities_clean' / 'equity_ohlcv.parquet'
    if not parquet_path.exists():
        print(f"Error: Parquet file not found at {parquet_path}")
        return

    print(f"Reading Parquet file: {parquet_path}")
    df = pd.read_parquet(parquet_path)
    print(f"Loaded {len(df):,} rows and {df['symbol'].nunique()} symbols.")

    # Standardize columns
    df = df.rename(columns={'trade_date': 'date'})
    
    db = SessionLocal()
    
    # 1. Get existing companies
    print("Mapping companies...")
    existing_companies = {c.symbol: c.id for c in db.query(Company).all()}
    
    all_symbols = df['symbol'].unique()
    new_companies = [Company(symbol=sym, is_active=True) for sym in all_symbols if sym not in existing_companies]
    
    if new_companies:
        print(f"Adding {len(new_companies)} new companies...")
        db.bulk_save_objects(new_companies)
        db.commit()
        existing_companies = {c.symbol: c.id for c in db.query(Company).all()}

    # 2. Prepare data
    print("Preparing data for ingestion...")
    df['company_id'] = df['symbol'].map(existing_companies)
    # Filter rows that didn't map (shouldn't happen but good to be safe)
    df = df.dropna(subset=['company_id'])
    df['company_id'] = df['company_id'].astype(int)
    
    if 'adj_close' not in df.columns:
        df['adj_close'] = df['close']
        
    df_to_ingest = df[['company_id', 'date', 'open', 'high', 'low', 'close', 'volume', 'adj_close']]
    
    # 3. Bulk insert in chunks with retry logic
    chunk_size = 50000
    total_rows = len(df_to_ingest)
    
    print(f"Ingesting {total_rows:,} rows in chunks of {chunk_size:,}...")
    
    from sqlalchemy.dialects.postgresql import insert
    metadata = MetaData()
    historical_prices_table = Table('historical_prices', metadata, autoload_with=engine)
    
    total_inserted = 0
    start_time = time.time()
    
    for i in range(0, total_rows, chunk_size):
        chunk_df = df_to_ingest.iloc[i:i+chunk_size]
        chunk = chunk_df.to_dict('records')
        
        # Convert date objects for SQL
        for row in chunk:
            if hasattr(row['date'], 'to_pydatetime'):
                row['date'] = row['date'].to_pydatetime().date()
        
        # Retry logic for the chunk
        max_retries = 3
        for attempt in range(max_retries):
            try:
                stmt = insert(historical_prices_table).values(chunk)
                on_conflict_stmt = stmt.on_conflict_do_nothing(index_elements=['company_id', 'date'])
                
                with engine.begin() as conn:
                    result = conn.execute(on_conflict_stmt)
                    total_inserted += result.rowcount
                break # Success
            except Exception as e:
                print(f"Attempt {attempt+1} failed for chunk {i}: {e}")
                if attempt == max_retries - 1:
                    print(f"Max retries reached for chunk {i}. Individual row insertion fallback...")
                    # Fallback to single row insertion for this chunk to isolate the error
                    for row in chunk:
                        try:
                            stmt_single = insert(historical_prices_table).values(row)
                            stmt_single = stmt_single.on_conflict_do_nothing(index_elements=['company_id', 'date'])
                            with engine.begin() as conn:
                                res = conn.execute(stmt_single)
                                total_inserted += res.rowcount
                        except Exception as row_e:
                            print(f"Skipping corrupt row: {row_e} | Data: {row}")
                else:
                    time.sleep(2) # Wait before retry

        if (i % (chunk_size * 2)) == 0:
            elapsed = time.time() - start_time
            progress = (i + len(chunk)) / total_rows * 100
            print(f"Progress: {progress:.1f}% | Total Processed: {i + len(chunk):,} | Elapsed: {elapsed:.1f}s")

    print(f"\n✅ Ingestion complete. Approximately {total_inserted:,} new records added.")
    db.close()

if __name__ == "__main__":
    ingest_data()
