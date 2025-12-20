"""
Bulk Indicator Calculation Script - High Performance Version
Calculates and updates indicators for all historical prices using bulk batch updates.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import sys
import os
import time
from sqlalchemy import text, MetaData, Table, bindparam

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(ROOT_DIR))

from backend.app.database import engine, Company, SessionLocal
from backend.app.indicators import ema, rsi, atr

def calculate_all_indicators():
    db = SessionLocal()
    
    # 1. Get all symbols that have historical data
    print("Fetching symbols for indicator calculation...")
    query = text("SELECT DISTINCT company_id FROM historical_prices")
    company_ids = [row[0] for row in db.execute(query).all()]
    print(f"Found {len(company_ids)} companies to process.")
    
    metadata = MetaData()
    historical_prices_table = Table('historical_prices', metadata, autoload_with=engine)
    
    processed_count = 0
    start_time = time.time()
    
    # Prepare the update statement with bindparams for efficient executemany
    update_stmt = historical_prices_table.update().where(
        historical_prices_table.c.id == bindparam('_id')
    ).values({
        'ema_20': bindparam('ema_20'),
        'ema_34': bindparam('ema_34'),
        'ema_50': bindparam('ema_50'),
        'rsi': bindparam('rsi'),
        'atr': bindparam('atr'),
        'atr_pct': bindparam('atr_pct'),
        'high_20d': bindparam('high_20d'),
        'is_breakout': bindparam('is_breakout'),
        'trend_7d': bindparam('trend_7d'),
        'trend_30d': bindparam('trend_30d')
    })
    
    for comp_id in company_ids:
        # Fetch data for this company
        query_prices = text("SELECT id, date, open, high, low, close, volume FROM historical_prices WHERE company_id = :cid ORDER BY date")
        df = pd.read_sql(query_prices, db.connection(), params={"cid": comp_id})
        
        if len(df) < 5: # Need some data to calculate even basic trends
            continue
            
        # Standardize columns for indicator functions
        df.columns = [col.capitalize() for col in df.columns]
        
        # Calculate indicators
        # Use fillna(0) for trends to avoid errors
        df['ema_20'] = ema(df['Close'], 20)
        df['ema_34'] = ema(df['Close'], 34)
        df['ema_50'] = ema(df['Close'], 50)
        df['rsi'] = rsi(df['Close'], 14)
        df['atr'] = atr(df, 14)
        df['atr_pct'] = (df['atr'] / df['Close']) * 100
        
        # Trend metrics
        df['high_20d'] = df['Close'].rolling(20).max()
        df['is_breakout'] = df['Close'] >= df['high_20d']
        df['trend_7d'] = df['Close'].pct_change(5) * 100
        df['trend_30d'] = df['Close'].pct_change(21) * 100
        
        # Prepare updates
        # Indicators might be NaN for first N rows
        updates = []
        for _, row in df.iterrows():
            # Mandatory check: skip if all indicators are NaN
            if pd.isna(row['ema_20']) and pd.isna(row['trend_7d']):
                continue
                
            updates.append({
                '_id': int(row['Id']),
                'ema_20': float(row['ema_20']) if not pd.isna(row['ema_20']) else None,
                'ema_34': float(row['ema_34']) if not pd.isna(row['ema_34']) else None,
                'ema_50': float(row['ema_50']) if not pd.isna(row['ema_50']) else None,
                'rsi': float(row['rsi']) if not pd.isna(row['rsi']) else None,
                'atr': float(row['atr']) if not pd.isna(row['atr']) else None,
                'atr_pct': float(row['atr_pct']) if not pd.isna(row['atr_pct']) else None,
                'high_20d': float(row['high_20d']) if not pd.isna(row['high_20d']) else None,
                'is_breakout': bool(row['is_breakout']) if not pd.isna(row['is_breakout']) else False,
                'trend_7d': float(row['trend_7d']) if not pd.isna(row['trend_7d']) else 0.0,
                'trend_30d': float(row['trend_30d']) if not pd.isna(row['trend_30d']) else 0.0
            })
            
        # Bulk update in one go for the company
        if updates:
            try:
                with engine.begin() as conn:
                    conn.execute(update_stmt, updates)
            except Exception as e:
                print(f"Error updating company {comp_id}: {e}")
        
        processed_count += 1
        if processed_count % 50 == 0:
            elapsed = time.time() - start_time
            print(f"Processed {processed_count}/{len(company_ids)} companies. Elapsed: {elapsed:.1f}s")
    
    db.close()
    print("\n✅ Indicator calculation complete.")

if __name__ == "__main__":
    calculate_all_indicators()
