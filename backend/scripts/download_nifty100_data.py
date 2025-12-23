
import sys
import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from fetch_intraday_data import fetch_and_store_symbol
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# List of Nifty 100 stocks
NIFTY100_SYMBOLS = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "AMBUJACEM", 
    "APOLLOHOSP", "ASIANPAINT", "AVENUE", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", 
    "BAJAJHOLDIN", "BANKBARODA", "BEL", "BHARATFORG", "BHARTIARTL", "BPCL", "BRITANNIA", "CHOLAFIN", 
    "COALINDIA", "COLPAL", "DLF", "DABUR", "DIVISLAB", "DRREDDY", "EICHERMOT", "GAIL", "GRASIM", 
    "HCLTECH", "HDFCBANK", "HDFCLIFE", "HDFCAMC", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", 
    "ICICIGI", "ICICIPRULI", "IDFCFIRSTB", "INDHOTEL", "INDUSINDBK", "INFY", "IOC", "IRFC", "ITC", 
    "JINDALSTEL", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LTIM", "LT", "LICI", "M&M", "MARICO", 
    "MARUTI", "MAXHEALTH", "MUTHOOTFIN", "NESTLEIND", "NTPC", "ONGC", "PIIND", "PIDILITIND", 
    "POWERGRID", "RELIANCE", "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS", 
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TITAN", 
    "TRENT", "TVSMOTOR", "ULTRACEMCO", "UPL", "VBL", "WIPRO", "ZOMATO"
]

def download_all_nifty100(timeframe=5, chunk_size=90, start_year=2024, end_year=None):
    if end_year is None:
        end_year = datetime.now().year
    
    # Process years in the order requested or logical order
    # Here we handle one year at a time
    year_start = datetime(start_year, 1, 1)
    year_end = datetime.now() if start_year == datetime.now().year else datetime(start_year, 12, 31)
    
    total_days_in_period = (year_end - year_start).days
    # For relative fetching in fetch_intraday_data, we need to know how many days ago year_end was
    days_ago_end = (datetime.now() - year_end).days
    total_days_back = (datetime.now() - year_start).days

    logger.info(f"\n{'='*20} STARTING DOWNLOAD FOR {start_year} {'='*20}")
    logger.info(f"Period: {year_start.date()} to {year_end.date()}")
    
    success_count = 0
    failed_symbols = []
    
    for i, symbol in enumerate(NIFTY100_SYMBOLS, 1):
        logger.info(f"\n[{i}/{len(NIFTY100_SYMBOLS)}] Year {start_year} | Processing {symbol}...")
        
        try:
            # We fetch in chunks of 90 days (standard for Fyers Intraday)
            # We start from days_ago_end and go back to total_days_back
            current_days_back = days_ago_end + chunk_size
            while current_days_back <= total_days_back + chunk_size:
                fetch_limit = min(current_days_back, total_days_back)
                logger.info(f"  Symbol: {symbol} | Fetching up to {fetch_limit} days back")
                
                try:
                    fetch_and_store_symbol(symbol, timeframe=timeframe, days_back=fetch_limit)
                except Exception as e:
                    logger.warning(f"  ⚠️ Fetch failed for {symbol} at {fetch_limit} days: {e}")
                
                if current_days_back >= total_days_back:
                    break
                current_days_back += chunk_size
                time.sleep(1.0) # Rate limit
                
            success_count += 1
        except Exception as e:
            logger.error(f"❌ Critical failure for {symbol} in year {start_year}: {e}")
            failed_symbols.append(symbol)

    logger.info(f"\nYEAR {start_year} COMPLETE. Success: {success_count}, Failed: {len(failed_symbols)}")

if __name__ == "__main__":
    # Queue: 2024 first, then 2023...2019
    queue = [2024, 2023, 2022, 2021, 2020, 2019]
    for year in queue:
        download_all_nifty100(start_year=year)
