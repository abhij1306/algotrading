"""
Daily Database Update Script
Runs after market close (3:30 PM IST) to update all market data and financial information

This script should be scheduled to run daily at 4:00 PM IST
"""
import sys
import os
from datetime import datetime
import logging

# Add backend to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils.logger import setup_logging, get_logger
from app.database import SessionLocal, Company

# Setup logging
setup_logging("daily_update")
logger = get_logger("daily_update")
from app.data_repository import DataRepository
from app.data_fetcher import fetch_historical_data
from concurrent.futures import ThreadPoolExecutor, as_completed

def update_single_company(symbol: str):
    """Worker function to update a single company"""
    db = SessionLocal()
    repo = DataRepository(db)
    try:
        # Fetch last 5 days (to catch any missed days)
        df = fetch_historical_data(symbol, days=5)

        if df is not None and not df.empty:
            records = repo.save_historical_prices(symbol, df, source='fyers')
            return True, records
        else:
            return False, "No data received"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def update_eod_prices(max_workers=10):
    """Update End-of-Day prices for all active stocks in parallel"""
    logger.info("=" * 70)
    logger.info(f"STEP 1: Updating EOD Prices from Fyers (Parallel, workers={max_workers})")
    logger.info("=" * 70)

    db = SessionLocal()
    try:
        companies = db.query(Company).filter(Company.is_active == True).all()
        total = len(companies)
        symbols = [c.symbol for c in companies]
        logger.info(f"Found {total} active companies")

        success = 0
        errors = 0
        total_records = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_symbol = {executor.submit(update_single_company, sym): sym for sym in symbols}

            for i, future in enumerate(as_completed(future_to_symbol), 1):
                symbol = future_to_symbol[future]
                try:
                    is_ok, result = future.result()
                    if is_ok:
                        success += 1
                        total_records += result
                        if i % 50 == 0 or i == total:
                            logger.info(f"Progress: [{i}/{total}] updated. Total new records: {total_records}")
                    else:
                        errors += 1
                        logger.warning(f"  ✗ {symbol}: {result}")
                except Exception as e:
                    errors += 1
                    logger.error(f"  ✗ {symbol} generated an exception: {e}")

        logger.info(f"\nEOD Update Complete: Success={success}, Errors={errors}, Total Records={total_records}")

    finally:
        db.close()

def update_single_index(symbol: str):
    """Worker function to update a single index"""
    db = SessionLocal()
    repo = DataRepository(db)
    try:
        df = fetch_historical_data(symbol, days=5)
        if df is not None and not df.empty:
            records = repo.save_historical_prices(symbol, df, source='yfinance')
            return True, records
        else:
            return False, "No data received"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()

def update_indices(max_workers=5):
    """Update index data (NIFTY50, BANKNIFTY, etc.) in parallel"""
    logger.info("\n" + "=" * 70)
    logger.info(f"STEP 2: Updating Index Data (Parallel, workers={max_workers})")
    logger.info("=" * 70)

    # Use yfinance ticker symbols for Indian indices
    indices = [
        "^NSEI",      # NIFTY 50
        "^NSEBANK",   # BANK NIFTY
        "^NSEFI",     # FIN NIFTY
        "^CNX100",    # NIFTY 100
        "^CNX200",    # NIFTY 200
        "^CNXIT",     # NIFTY IT
        "^CNXAUTO",   # NIFTY AUTO
        "^CNXPHARMA", # NIFTY PHARMA
        "^CNXFMCG",   # NIFTY FMCG
        "^CNXMETAL",  # NIFTY METAL
        "^CNXENERGY", # NIFTY ENERGY
        "^CNXREALTY", # NIFTY REALTY
    ]

    success = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(update_single_index, sym): sym for sym in indices}

        for future in as_completed(future_to_index):
            symbol = future_to_index[future]
            try:
                is_ok, result = future.result()
                if is_ok:
                    success += 1
                    logger.info(f"  ✓ {symbol}: Added {result} records")
                else:
                    errors += 1
                    logger.warning(f"  ✗ {symbol}: {result}")
            except Exception as e:
                errors += 1
                logger.error(f"  ✗ {symbol} generated an exception: {e}")

    logger.info(f"Index update complete: Success={success}, Errors={errors}")

def precompute_indicators():
    """Recalculate technical indicators for all stocks"""
    logger.info("\n" + "=" * 70)
    logger.info("STEP 3: Precomputing Technical Indicators")
    logger.info("=" * 70)

    try:
        import subprocess
        script_path = os.path.join(os.path.dirname(__file__), 'precompute_indicators.py')
        result = subprocess.run([sys.executable, script_path], capture_output=True, text=True)

        if result.returncode == 0:
            logger.info("✓ Indicators precomputed successfully")
        else:
            logger.error(f"✗ Indicator computation failed: {result.stderr}")

    except Exception as e:
        logger.error(f"✗ Error: {str(e)}")

def update_financial_data(limit=50):
    """Update financial data for top stocks (incremental)"""
    logger.info("\n" + "=" * 70)
    logger.info(f"STEP 4: Updating Financial Data ({limit} companies)")
    logger.info("=" * 70)

    try:
        import subprocess
        script_path = os.path.join(os.path.dirname(__file__), 'populate_comprehensive_financials.py')
        result = subprocess.run(
            [sys.executable, script_path, '--limit', str(limit)],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            logger.info("✓ Financial data updated")
        else:
            logger.error(f"✗ Financial update failed: {result.stderr}")

    except Exception as e:
        logger.error(f"✗ Error: {str(e)}")

def main():
    """Main execution function"""
    start_time = datetime.now()
    logger.info("\n" + "=" * 70)
    logger.info("DAILY DATABASE UPDATE STARTED")
    logger.info(f"Time: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    try:
        # Step 1: Update EOD prices for all stocks
        update_eod_prices()

        # Step 2: Update indices
        update_indices()

        # Step 3: Precompute technical indicators
        precompute_indicators()

        # Financial data updates are manual (run separately when needed)
        # Uncomment below to enable daily financial updates (not recommended)
        # update_financial_data(limit=50)

    except Exception as e:
        logger.error(f"CRITICAL ERROR: {str(e)}")
        raise

    finally:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.info("\n" + "=" * 70)
        logger.info("DAILY UPDATE COMPLETE")
        logger.info(f"Duration: {duration:.2f} seconds ({duration/60:.1f} minutes)")
        logger.info(f"Log file: {log_file}")
        logger.info("=" * 70)

if __name__ == "__main__":
    main()
