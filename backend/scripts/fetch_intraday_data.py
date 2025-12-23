"""
Fyers Intraday Data Fetcher
Fetches and stores intraday OHLCV data for backtesting strategies
"""

import sys
import os
from pathlib import Path

# Add parent directories to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
sys.path.append(str(Path(__file__).resolve().parent.parent / 'backend'))

from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy.exc import IntegrityError

# Import Fyers client from proper location
sys.path.append(str(Path(__file__).resolve().parent.parent / 'Fyers'))
from fyers_client import get_historical_data, load_fyers, validate_token

from backend.app.database import SessionLocal, IntradayCandle, Company, get_or_create_company
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fetch_intraday_data(symbol: str, timeframe: int, days_back: int = 30):
    """
    Fetch intraday historical data from Fyers API
    
    Args:
        symbol: NSE symbol (e.g., "RELIANCE")
        timeframe: Timeframe in minutes (1, 5, 15, 30, 60)
        days_back: Number of days of historical data to fetch
    
    Returns:
        DataFrame with OHLCV data
    """
    # Validate token first
    if not validate_token():
        raise Exception("Fyers token is invalid. Please run fyers_login.py first.")
    
    # Format symbol for Fyers API
    # Check if it's an index or equity
    if symbol.upper().endswith('-INDEX') or symbol.upper().endswith('INDEX'):
        fyers_symbol = f"NSE:{symbol}"
    else:
        fyers_symbol = f"NSE:{symbol}-EQ"
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    # Fyers timeframe mapping
    tf_map = {
        1: "1",
        5: "5",
        15: "15",
        30: "30",
        60: "60"
    }
    
    if timeframe not in tf_map:
        raise ValueError(f"Invalid timeframe: {timeframe}. Must be one of {list(tf_map.keys())}")
    
    logger.info(f"Fetching {timeframe}-min data for {symbol} from {start_date.date()} to {end_date.date()}")
    
    # Fetch data from Fyers
    response = get_historical_data(
        symbol=fyers_symbol,
        timeframe=tf_map[timeframe],
        range_from=start_date.strftime("%Y-%m-%d"),
        range_to=end_date.strftime("%Y-%m-%d")
    )
    
    if response.get('s') != 'ok':
        raise Exception(f"Fyers API error: {response.get('message', 'Unknown error')}")
    
    # Parse response into DataFrame
    candles = response.get('candles', [])
    if not candles:
        logger.warning(f"No data received for {symbol}")
        return pd.DataFrame()
    
    df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
    
    logger.info(f"Fetched {len(df)} candles for {symbol}")
    return df


def store_intraday_data(db, company_id: int, df: pd.DataFrame, timeframe: int):
    """
    Store intraday data in database
    
    Args:
        db: Database session
        company_id: Company ID
        df: DataFrame with OHLCV data
        timeframe: Timeframe in minutes
    """
    if df.empty:
        return 0
    
    stored_count = 0
    skipped_count = 0
    
    for _, row in df.iterrows():
        try:
            candle = IntradayCandle(
                company_id=company_id,
                timestamp=row['timestamp'],
                timeframe=timeframe,
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                volume=int(row['volume']),
                source='fyers'
            )
            db.add(candle)
            db.commit()
            stored_count += 1
        except IntegrityError:
            # Candle already exists
            db.rollback()
            skipped_count += 1
        except Exception as e:
            db.rollback()
            logger.error(f"Error storing candle: {e}")
    
    logger.info(f"Stored: {stored_count}, Skipped (duplicates): {skipped_count}")
    return stored_count


def fetch_and_store_symbol(symbol: str, timeframe: int = 5, days_back: int = 30):
    """
    Fetch and store intraday data for a symbol
    
    Args:
        symbol: NSE symbol
        timeframe: Timeframe in minutes (default: 5)
        days_back: Days of historical data (default: 30)
    """
    db = SessionLocal()
    
    try:
        # Get or create company
        company = get_or_create_company(db, symbol=symbol, name=symbol)
        logger.info(f"Processing {symbol} (Company ID: {company.id})")
        
        # Fetch data
        df = fetch_intraday_data(symbol, timeframe, days_back)
        
        # Store data
        if not df.empty:
            count = store_intraday_data(db, company.id, df, timeframe)
            logger.info(f"✅ Successfully processed {symbol}: {count} new candles")
        else:
            logger.warning(f"⚠️ No data to store for {symbol}")
    
    except Exception as e:
        logger.error(f"❌ Error processing {symbol}: {e}")
        raise
    finally:
        db.close()


def fetch_multiple_symbols(symbols: list, timeframe: int = 5, days_back: int = 30):
    """
    Fetch and store data for multiple symbols
    
    Args:
        symbols: List of NSE symbols
        timeframe: Timeframe in minutes
        days_back: Days of historical data
    """
    logger.info(f"Starting batch fetch for {len(symbols)} symbols")
    
    success_count = 0
    failed_symbols = []
    
    for symbol in symbols:
        try:
            fetch_and_store_symbol(symbol, timeframe, days_back)
            success_count += 1
        except Exception as e:
            failed_symbols.append(symbol)
            logger.error(f"Failed to process {symbol}: {e}")
        
        # Small delay to avoid rate limiting
        import time
        time.sleep(0.5)
    
    logger.info(f"\n{'='*50}")
    logger.info(f"Batch Complete: {success_count}/{len(symbols)} successful")
    if failed_symbols:
        logger.warning(f"Failed symbols: {', '.join(failed_symbols)}")
    logger.info(f"{'='*50}\n")


if __name__ == "__main__":
    # Fetch NIFTY50 Index data for backtesting
    try:
        print("Starting data fetch for NIFTY50-INDEX...")
        # Single symbol fetch
        fetch_and_store_symbol("NIFTY50-INDEX", timeframe=5, days_back=60)
        
    except Exception as e:
        logger.error(f"Script failed: {e}")
        sys.exit(1)
