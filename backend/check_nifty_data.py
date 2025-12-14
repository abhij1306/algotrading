#!/usr/bin/env python3
"""
Check how much NIFTY intraday data we have in the database
"""

import sys
from pathlib import Path

# Add parent directories to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / 'backend'))

from backend.app.database import SessionLocal, IntradayCandle, Company
from sqlalchemy import func
from datetime import datetime

def check_nifty_data():
    db = SessionLocal()
    
    try:
        # Get NIFTY company
        nifty = db.query(Company).filter(Company.symbol == "NIFTY50-INDEX").first()
        
        if not nifty:
            print("❌ NIFTY50-INDEX not found in database")
            return
        
        print(f"✅ Company: {nifty.symbol} (ID: {nifty.id})")
        
        # Get candle count by timeframe
        candle_count = db.query(func.count(IntradayCandle.id)).filter(
            IntradayCandle.company_id == nifty.id,
            IntradayCandle.timeframe == 5
        ).scalar()
        
        print(f"\n📊 Total 5-minute candles: {candle_count}")
        
        # Get date range
        min_date = db.query(func.min(IntradayCandle.timestamp)).filter(
            IntradayCandle.company_id == nifty.id,
            IntradayCandle.timeframe == 5
        ).scalar()
        
        max_date = db.query(func.max(IntradayCandle.timestamp)).filter(
            IntradayCandle.company_id == nifty.id,
            IntradayCandle.timeframe == 5
        ).scalar()
        
        if min_date and max_date:
            days_diff = (max_date - min_date).days
            print(f"\n📅 Date Range:")
            print(f"   Start: {min_date}")
            print(f"   End:   {max_date}")
            print(f"   Days:  {days_diff} days")
            
            # Calculate expected vs actual candles
            # Market hours: 9:15 AM to 3:30 PM IST = 6 hours 15 minutes = 75 five-minute candles per day
            expected_candles = days_diff * 75
            print(f"\n💡 Expected candles (approx): {expected_candles}")
            print(f"   Actual candles: {candle_count}")
            print(f"   Coverage: {(candle_count / max(expected_candles, 1)) * 100:.1f}%")
        
    finally:
        db.close()


if __name__ == "__main__":
    print("\n" + "="*60)
    print("NIFTY 5-Minute Data Summary")
    print("="*60 + "\n")
    check_nifty_data()
    print("\n" + "="*60 + "\n")
