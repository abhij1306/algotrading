#!/usr/bin/env python3
"""
Recalculate Technical Indicators
Batch updates RSI, ATR, EMA for all stocks overnight

Run this daily at night:
0 1 * * * /path/to/venv/bin/python /path/to/recalculate_indicators.py
"""
import sys
import os
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.database import SessionLocal, Company, HistoricalPrice
from app.indicators import (
    calculate_ema, calculate_rsi, calculate_atr,
    calculate_macd, calculate_bbands, calculate_stoch,
    calculate_adx, calculate_obv
)
from sqlalchemy import func


def calculate_indicators_for_company(db, company_id: int, symbol: str):
    """
    Recalculate all indicators for a single company
    Updates the most recent 100 days to ensure accuracy
    """
    # Fetch last 100 days of data
    prices = db.query(HistoricalPrice)\
        .filter(HistoricalPrice.company_id == company_id)\
        .order_by(HistoricalPrice.date.desc())\
        .limit(100)\
        .all()
    
    if len(prices) < 20:  # Need at least 20 days for indicators
        return False
    
    # Reverse to chronological order
    prices = list(reversed(prices))
    
    # Convert to DataFrame
    df = pd.DataFrame([{
        'date': p.date,
        'open': p.open,
        'high': p.high,
        'low': p.low,
        'close': p.close,
        'volume': p.volume
    } for p in prices])
    
    # Calculate indicators
    df['ema_20'] = calculate_ema(df['close'], period=20)
    df['ema_34'] = calculate_ema(df['close'], period=34)
    df['ema_50'] = calculate_ema(df['close'], period=50)
    df['rsi'] = calculate_rsi(df['close'], period=14)
    df['atr'] = calculate_atr(df['high'], df['low'], df['close'], period=14)
    df['atr_pct'] = (df['atr'] / df['close']) * 100
    
    # Advanced Indicators
    df['macd'], df['macd_signal'], df['macd_hist'] = calculate_macd(df['close'])
    df['bb_upper'], df['bb_middle'], df['bb_lower'] = calculate_bbands(df['close'])
    df['stoch_k'], df['stoch_d'] = calculate_stoch(df['high'], df['low'], df['close'])
    df['adx'] = calculate_adx(df['high'], df['low'], df['close'])
    df['obv'] = calculate_obv(df['close'], df['volume'])
    
    # Update database (only last 30 days to avoid excessive writes)
    updated = 0
    for i in range(max(0, len(prices) - 30), len(prices)):
        price_record = prices[i]
        row = df.iloc[i]
        
        price_record.ema_20 = float(row['ema_20']) if not pd.isna(row['ema_20']) else None
        price_record.ema_34 = float(row['ema_34']) if not pd.isna(row['ema_34']) else None
        price_record.ema_50 = float(row['ema_50']) if not pd.isna(row['ema_50']) else None
        price_record.rsi = float(row['rsi']) if not pd.isna(row['rsi']) else None
        price_record.atr = float(row['atr']) if not pd.isna(row['atr']) else None
        price_record.atr_pct = float(row['atr_pct']) if not pd.isna(row['atr_pct']) else None
        
        # Save Advanced Indicators
        price_record.macd = float(row['macd']) if not pd.isna(row['macd']) else None
        price_record.macd_signal = float(row['macd_signal']) if not pd.isna(row['macd_signal']) else None
        price_record.macd_histogram = float(row['macd_hist']) if not pd.isna(row['macd_hist']) else None
        price_record.stoch_k = float(row['stoch_k']) if not pd.isna(row['stoch_k']) else None
        price_record.stoch_d = float(row['stoch_d']) if not pd.isna(row['stoch_d']) else None
        price_record.bb_upper = float(row['bb_upper']) if not pd.isna(row['bb_upper']) else None
        price_record.bb_middle = float(row['bb_middle']) if not pd.isna(row['bb_middle']) else None
        price_record.bb_lower = float(row['bb_lower']) if not pd.isna(row['bb_lower']) else None
        price_record.adx = float(row['adx']) if not pd.isna(row['adx']) else None
        price_record.obv = int(row['obv']) if not pd.isna(row['obv']) else None
        
        updated += 1
    
    db.commit()
    return updated


def main(limit: int = None):
    """
    Recalculate indicators for all companies
    
    Args:
        limit: Max companies to process (for testing)
    """
    db = SessionLocal()
    try:
        print("=" * 60)
        print(f"Indicator Recalculation - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        
        # Get companies with recent data
        query = db.query(Company)\
            .filter(Company.is_active == True)\
            .join(HistoricalPrice)\
            .group_by(Company.id)\
            .having(func.count(HistoricalPrice.id) >= 20)
        
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"\nProcessing {total} companies...\n")
        
        success = 0
        failed = 0
        
        for i, company in enumerate(companies, 1):
            try:
                print(f"[{i}/{total}] {company.symbol}...", end=" ", flush=True)
                
                updated = calculate_indicators_for_company(db, company.id, company.symbol)
                
                if updated:
                    print(f"✅ ({updated} records)")
                    success += 1
                else:
                    print("⚠️  Insufficient data")
            
            except Exception as e:
                print(f"❌ {str(e)[:50]}")
                failed += 1
                db.rollback()
        
        print("\n" + "=" * 60)
        print(f"Indicator Update Complete:")
        print(f"  Success: {success}")
        print(f"  Failed: {failed}")
        print("=" * 60)
    
    finally:
        db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Limit companies (for testing)")
    args = parser.parse_args()
    
    main(limit=args.limit)
