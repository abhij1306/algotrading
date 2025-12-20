import pandas as pd
from pathlib import Path
import sys
import os

# Get project root - assuming script is in scripts/
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

from backend.app.database import engine, Company, SessionLocal
from sqlalchemy import Table, MetaData
from sqlalchemy.dialects.postgresql import insert

def debug_chunk():
    # Use absolute path from ROOT_DIR
    parquet_path = ROOT_DIR / 'nse_data' / 'processed' / 'equities_clean' / 'equity_ohlcv.parquet'
    print(f"Loading Parquet from: {parquet_path}")
    
    df = pd.read_parquet(parquet_path)
    df = df.rename(columns={'trade_date': 'date'})
    
    db = SessionLocal()
    existing_companies = {c.symbol: c.id for c in db.query(Company).all()}
    db.close()
    
    df['company_id'] = df['symbol'].map(existing_companies)
    if 'adj_close' not in df.columns:
        df['adj_close'] = df['close']
    
    df = df.dropna(subset=['company_id'])
    df['company_id'] = df['company_id'].astype(int)
    
    df_to_ingest = df[['company_id', 'date', 'open', 'high', 'low', 'close', 'volume', 'adj_close']]
    
    # Failing at 1,500,000
    chunk_start = 1500000
    chunk_size = 50000
    chunk_end = chunk_start + chunk_size
    
    chunk = df_to_ingest.iloc[chunk_start:chunk_end]
    print(f"Checking chunk {chunk_start} to {chunk_end}")
    
    metadata = MetaData()
    historical_prices_table = Table('historical_prices', metadata, autoload_with=engine)
    
    print("Testing rows one by one to find the exact failure...")
    for i, (_, row) in enumerate(chunk.iterrows()):
        row_dict = row.to_dict()
        if hasattr(row_dict['date'], 'to_pydatetime'):
            row_dict['date'] = row_dict['date'].to_pydatetime().date()
            
        try:
            stmt = insert(historical_prices_table).values(row_dict)
            stmt = stmt.on_conflict_do_nothing(index_elements=['company_id', 'date'])
            with engine.begin() as conn:
                conn.execute(stmt)
        except Exception as e:
            print(f"!!! Row {chunk_start + i} FAILED: {e}")
            print(f"Data: {row_dict}")
            # Check for specific issues
            if pd.isna(row_dict['open']) or pd.isna(row_dict['close']):
                 print("  Reason: NaN in price")
            break

if __name__ == "__main__":
    debug_chunk()
