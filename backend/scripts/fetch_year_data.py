#!/usr/bin/env python3
"""
Fetch 1 year of NIFTY 5-min data from Fyers in chunks
Breaks the request into 90-day chunks to avoid API limitations
"""

import sys
from pathlib import Path

# Add parent directories to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / 'backend'))

from datetime import datetime, timedelta
from fetch_intraday_data import fetch_and_store_symbol
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fetch_year_in_chunks(symbol: str, timeframe: int = 5, chunk_size: int = 90):
    """
    Fetch 1 year of data in chunks to avoid API limitations
    
    Args:
        symbol: Symbol to fetch (e.g., "NIFTY50-INDEX")
        timeframe: Timeframe in minutes (default: 5)
        chunk_size: Days per chunk (default: 90)
    """
    total_days = 365
    chunks = (total_days // chunk_size) + 1
    
    logger.info(f"Fetching {total_days} days of {timeframe}-min data for {symbol} in {chunks} chunks")
    
    total_candles = 0
    
    for i in range(chunks):
        chunk_start = i * chunk_size
        chunk_end = min((i + 1) * chunk_size, total_days)
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Chunk {i+1}/{chunks}: Fetching days {chunk_start} to {chunk_end} ago")
        logger.info(f"{'='*60}")
        
        try:
            fetch_and_store_symbol(symbol, timeframe=timeframe, days_back=chunk_end)
            logger.info(f"✅ Chunk {i+1} completed successfully")
        except Exception as e:
            logger.error(f"❌ Chunk {i+1} failed: {e}")
            # Continue with next chunk even if one fails
            continue
    
    logger.info(f"\n{'='*60}")
    logger.info(f"Data fetch complete for {symbol}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    try:
        print("\nFetching 1 year of 5-minute NIFTY data from Fyers...\n")
        fetch_year_in_chunks("NIFTY50-INDEX", timeframe=5, chunk_size=90)
        print("\n✅ Complete! Check the database for NIFTY50-INDEX 5-minute candles.\n")
        
    except Exception as e:
        logger.error(f"Script failed: {e}")
        sys.exit(1)
