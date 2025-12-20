"""
Ingest intraday 5-minute candle data for NIFTY50 and BANKNIFTY into the database.
"""

import pandas as pd
from datetime import datetime
from app.database import SessionLocal, Company, IntradayCandle
from sqlalchemy import and_

def ingest_intraday_csv(symbol: str, csv_path: str):
    """Load intraday CSV data into database"""
    db = SessionLocal()
    
    try:
        # Get or create company
        company = db.query(Company).filter(Company.symbol == symbol).first()
        if not company:
            company = Company(
                symbol=symbol,
                name=f"{symbol} Index",
                sector="Index",
                is_active=True
            )
            db.add(company)
            db.commit()
            db.refresh(company)
        
        print(f"Loading {symbol} from {csv_path}...")
        
        # Read CSV
        df = pd.read_csv(csv_path)
        print(f"Found {len(df)} candles")
        
        # Expected columns: timestamp, open, high, low, close, volume
        # Convert timestamp to datetime
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # Check if data already exists
        existing_count = db.query(IntradayCandle).filter(
            and_(
                IntradayCandle.company_id == company.id,
                IntradayCandle.timeframe == 5
            )
        ).count()
        
        if existing_count > 0:
            print(f"Found {existing_count} existing candles. Deleting...")
            db.query(IntradayCandle).filter(
                and_(
                    IntradayCandle.company_id == company.id,
                    IntradayCandle.timeframe == 5
                )
            ).delete()
            db.commit()
        
        # Batch insert
        batch_size = 1000
        total_inserted = 0
        
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i:i+batch_size]
            candles = []
            
            for _, row in batch.iterrows():
                candle = IntradayCandle(
                    company_id=company.id,
                    timestamp=row['timestamp'],
                    timeframe=5,
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=int(row['volume']) if pd.notna(row['volume']) else 0
                )
                candles.append(candle)
            
            db.bulk_save_objects(candles)
            db.commit()
            total_inserted += len(candles)
            
            if total_inserted % 10000 == 0:
                print(f"Inserted {total_inserted}/{len(df)} candles...")
        
        print(f"✅ Successfully inserted {total_inserted} candles for {symbol}")
        
    except Exception as e:
        print(f"❌ Error ingesting {symbol}: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    print("=" * 60)
    print("INTRADAY DATA INGESTION")
    print("=" * 60)
    
    # Ingest NIFTY50
    ingest_intraday_csv(
        symbol="NIFTY50",
        csv_path="nse_data/raw/intraday/NIFTY50_5min_complete.csv"
    )
    
    print()
    
    # Ingest BANKNIFTY
    ingest_intraday_csv(
        symbol="BANKNIFTY",
        csv_path="nse_data/raw/intraday/BANKNIFTY_5min_complete.csv"
    )
    
    print()
    print("=" * 60)
    print("INGESTION COMPLETE")
    print("=" * 60)
