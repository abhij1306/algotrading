#!/usr/bin/env python3
"""
Fetch 5-minute data for all NSE F&O stocks (3 months)
Downloads intraday data for all stocks in the F&O universe
"""

import sys
import json
from pathlib import Path

# Add parent directories to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / 'backend'))

from fetch_intraday_data import fetch_and_store_symbol
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_fno_universe():
    """Load F&O stock universe from JSON file"""
    fno_file = Path(__file__).resolve().parent / 'data' / 'nse_fno_universe.json'
    
    with open(fno_file, 'r') as f:
        symbols = json.load(f)
    
    # Remove duplicates and return unique symbols
    unique_symbols = list(set(symbols))
    return sorted(unique_symbols)


def fetch_fno_data(timeframe: int = 5, days_back: int = 90, delay: float = 1.0):
    """
    Fetch intraday data for all F&O stocks
    
    Args:
        timeframe: Timeframe in minutes (default: 5)
        days_back: Days of historical data (default: 90)
        delay: Delay between requests in seconds (default: 1.0)
    """
    symbols = load_fno_universe()
    
    logger.info(f"\n{'='*70}")
    logger.info(f"Starting F&O Universe Data Download")
    logger.info(f"{'='*70}")
    logger.info(f"Total Unique Stocks: {len(symbols)}")
    logger.info(f"Timeframe: {timeframe} minutes")
    logger.info(f"Days Back: {days_back}")
    logger.info(f"Estimated Time: ~{(len(symbols) * delay / 60):.1f} minutes")
    logger.info(f"{'='*70}\n")
   
    success_count = 0
    failed_symbols = []
    skipped_symbols = []
    
    for i, symbol in enumerate(symbols, 1):
        logger.info(f"\n[{i}/{len(symbols)}] Processing {symbol}...")
        
        try:
            fetch_and_store_symbol(symbol, timeframe=timeframe, days_back=days_back)
            success_count += 1
            logger.info(f"✅ {symbol} - Success ({success_count}/{len(symbols)})")
            
        except Exception as e:
            error_msg = str(e)
            
            # Check if it's a "no data" error vs actual failure
            if "No data" in error_msg or "Invalid input" in error_msg:
                skipped_symbols.append(symbol)
                logger.warning(f"⚠️  {symbol} - No data available")
            else:
                failed_symbols.append(symbol)
                logger.error(f"❌ {symbol} - Failed: {error_msg}")
        
        # Rate limiting delay to avoid overwhelming Fyers API
        if i < len(symbols):  # Don't delay after last symbol
            time.sleep(delay)
    
    # Final Summary
    logger.info(f"\n{'='*70}")
    logger.info(f"Download Complete!")
    logger.info(f"{'='*70}")
    logger.info(f"✅ Successful: {success_count}/{len(symbols)} ({(success_count/len(symbols)*100):.1f}%)")
    logger.info(f"⚠️  Skipped (No Data): {len(skipped_symbols)}")
    logger.info(f"❌ Failed: {len(failed_symbols)}")
    
    if skipped_symbols:
        logger.info(f"\nSkipped symbols (no data): {', '.join(skipped_symbols[:10])}")
        if len(skipped_symbols) > 10:
            logger.info(f"... and {len(skipped_symbols) - 10} more")
    
    if failed_symbols:
        logger.warning(f"\nFailed symbols: {', '.join(failed_symbols)}")
    
    logger.info(f"{'='*70}\n")


if __name__ == "__main__":
    try:
        print("\n" + "="*70)
        print("NSE F&O Universe - 5-Minute Data Download")
        print("="*70 + "\n")
        
        # Fetch data with 1 second delay between requests
        fetch_fno_data(timeframe=5, days_back=90, delay=1.0)
        
        print("\n✅ Download complete! Check database for F&O stock candles.\n")
        
    except KeyboardInterrupt:
        logger.warning("\n\n⚠️  Download interrupted by user. Partial data may be saved.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n❌ Script failed: {e}")
        sys.exit(1)
