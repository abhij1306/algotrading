import sys
import os
import pandas as pd
from datetime import datetime

# Setup path to include backend
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app'))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal, Company, HistoricalPrice
from app.data_fetcher import fetch_fyers_quotes

def check_symbols():
    db = SessionLocal()
    
    # Symbols to check (from user screenshot + known good ones)
    target_symbols = ['ZOMATO', 'TATAMOTORS', 'UNITEDBNK', 'TRICOM', 'RELIANCE', 'SBIN']
    
    print(f"{'SYMBOL':<15} | {'DB STATUS':<10} | {'DB PRICE':<10} | {'LIVE QUOTE':<15} | {'LIVE VOL':<10} | {'MSG'}")
    print("-" * 80)
    
    try:
        # 1. Fetch Live Quotes Batch
        live_data = fetch_fyers_quotes(target_symbols)
        
        for sym in target_symbols:
            # Check DB
            company = db.query(Company).filter(Company.symbol == sym).first()
            db_status = "FOUND" if company else "MISSING"
            db_price = "N/A"
            
            if company:
                # Get latest hist price
                latest = db.query(HistoricalPrice).filter(HistoricalPrice.company_id == company.id).order_by(HistoricalPrice.date.desc()).first()
                if latest:
                    db_price = f"{latest.close:.2f}"
                else:
                    db_price = "NO_DATA"
            
            # Check Live
            live_price = "N/A"
            live_vol = "N/A"
            msg = ""
            
            if sym in live_data:
                qt = live_data[sym]
                live_price = qt.get('ltp', 0)
                live_vol = qt.get('volume', 0)
                if live_price == 0:
                    msg = "Live Quote is 0 (Invalid?)"
            else:
                msg = "No Quote Returned"
                
            print(f"{sym:<15} | {db_status:<10} | {db_price:<10} | {live_price:<15} | {live_vol:<10} | {msg}")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    check_symbols()
