
import sys
import os
import pandas as pd
from datetime import datetime, date
import time

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal
from app.data_repository import DataRepository
from app.nse_data_reader import NSEDataReader

def main():
    print("Starting bulk import from local NSE Parquet data (DuckDB)...")
    
    try:
        reader = NSEDataReader()
        # Verify data availability
        date_range = reader.get_date_range()
        if not date_range:
            print("No data found in NSE Parquet file.")
            return
            
        print(f"Data available from {date_range[0]} to {date_range[1]}")
        
        # Get all distinct symbols from the dataset
        # We use a direct query since get_symbols_for_date only returns for one date
        query = f"SELECT DISTINCT symbol FROM read_parquet('{reader.data_dir}/equity_ohlcv.parquet')"
        symbols_df = reader.con.execute(query).df()
        symbols = symbols_df['symbol'].tolist()
        
        print(f"Found {len(symbols)} symbols in local dataset.")
        
        db = SessionLocal()
        repo = DataRepository(db)
        
        total_imported = 0
        start_time = time.time()
        
        # Batch size handling if needed, but save_historical_prices commits per call.
        
        for idx, symbol in enumerate(symbols):
            try:
                # Fetch full history for symbol
                # We use string dates since reader expects them
                str_min = date_range[0].strftime("%Y-%m-%d") if isinstance(date_range[0], (date, datetime)) else str(date_range[0])
                str_max = date_range[1].strftime("%Y-%m-%d") if isinstance(date_range[1], (date, datetime)) else str(date_range[1])
                
                df = reader.get_historical_data(symbol, str_min, str_max)
                
                if df is None or df.empty:
                    continue
                
                # Normalize columns for DataRepository
                # NSEDataReader returns: DATE, SYMBOL, OPEN, HIGH, LOW, CLOSE, VOLUME
                # DataRepository expects: Open, High, Low, Close, Volume (index=Date)
                
                df = df.rename(columns={
                    'DATE': 'Date',
                    'OPEN': 'Open',
                    'HIGH': 'High',
                    'LOW': 'Low',
                    'CLOSE': 'Close',
                    'VOLUME': 'Volume'
                })
                
                if 'Date' in df.columns:
                    df['Date'] = pd.to_datetime(df['Date'])
                    df = df.set_index('Date')
                
                # Save to DB
                # 'df' must have Open, High, Low, Close, Volume columns and DatetimeIndex
                count = repo.save_historical_prices(symbol, df, source='nse_local')
                total_imported += count
                
                if idx % 10 == 0:
                    print(f"[{idx+1}/{len(symbols)}] Imported {symbol}: {count} records.")
                    
            except Exception as e:
                print(f"Error processing {symbol}: {e}")
                
        elapsed = time.time() - start_time
        print(f"Bulk Import Complete. Processed {len(symbols)} symbols, {total_imported} records in {elapsed:.2f}s")
        
    except Exception as e:
        print(f"Fatal Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
