"""
Download Index Data (Nifty 50 & BankNifty) - 5min and Daily
From 2019 to present
"""
import sys
import os
from pathlib import Path
from datetime import datetime, date, timedelta
import time

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, get_or_create_company
from fetch_intraday_data import fetch_intraday_data, store_intraday_data
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def download_index_data():
    """Download Nifty 50 and BankNifty index data"""
    
    indices = ["NIFTY50-INDEX", "BANKNIFTY-INDEX"]
    
    # Years to download (2019 to 2024)
    years_to_download = [2024, 2023, 2022, 2021, 2020, 2019]
    
    db = SessionLocal()
    
    for idx_symbol in indices:
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing {idx_symbol}")
        logger.info(f"{'='*60}")
        
        # Get or create company
        company = get_or_create_company(db, symbol=idx_symbol, name=idx_symbol)
        
        for year in years_to_download:
            try:
                # Calculate date range for the year
                if year == 2024:
                    start_date = date(2024, 1, 1)
                    end_date = datetime.now().date()
                else:
                    start_date = date(year, 1, 1)
                    end_date = date(year, 12, 31)
                
                days_back = (end_date - start_date).days
                
                logger.info(f"\n📅 Downloading {idx_symbol} for {year}")
                logger.info(f"   Range: {start_date} to {end_date} ({days_back} days)")
                
                # Fetch 5-minute data
                df = fetch_intraday_data(idx_symbol, timeframe=5, days_back=days_back)
                
                if not df.empty:
                    count = store_intraday_data(db, company.id, df, timeframe=5)
                    logger.info(f"   ✅ Stored {count} new 5-min candles")
                else:
                    logger.warning(f"   ⚠️ No data received for {year}")
                
                # Rate limit
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"   ❌ Error downloading {idx_symbol} for {year}: {e}")
                time.sleep(5)  # Longer wait on error
                continue
    
    db.close()
    logger.info(f"\n{'='*60}")
    logger.info("Index data download complete!")
    logger.info(f"{'='*60}")

if __name__ == "__main__":
    download_index_data()
