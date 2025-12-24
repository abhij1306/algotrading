"""
Daily Database Update Script
Runs after market close (3:30 PM IST) to update all market data and financial information

This script should be scheduled to run daily at 4:00 PM IST
"""
import sys
import os
from datetime import datetime
import logging

# Setup logging
log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f'daily_update_{datetime.now().strftime("%Y%m%d")}.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Add backend to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, Company
from app.data_repository import DataRepository
from app.data_fetcher import fetch_historical_data

def update_eod_prices():
    """Update End-of-Day prices for all active stocks"""
    logger.info("=" * 70)
    logger.info("STEP 1: Updating EOD Prices from Fyers")
    logger.info("=" * 70)
    
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        companies = db.query(Company).filter(Company.is_active == True).all()
        total = len(companies)
        logger.info(f"Found {total} active companies")
        
        success = 0
        errors = 0
        
        for i, company in enumerate(companies, 1):
            try:
                logger.info(f"[{i}/{total}] Updating {company.symbol}...")
                
                # Fetch last 5 days (to catch any missed days)
                df = fetch_historical_data(company.symbol, days=5)
                
                if df is not None and not df.empty:
                    records = repo.save_historical_prices(company.symbol, df, source='fyers')
                    logger.info(f"  ✓ Added {records} new records")
                    success += 1
                else:
                    logger.warning(f"  ✗ No data received")
                    errors += 1
                    
            except Exception as e:
                logger.error(f"  ✗ Error: {str(e)}")
                errors += 1
        
        logger.info(f"\nEOD Update Complete: Success={success}, Errors={errors}")
        
    finally:
        db.close()

def update_indices():
    """Update index data (NIFTY50, BANKNIFTY, etc.)"""
    logger.info("\n" + "=" * 70)
    logger.info("STEP 2: Updating Index Data")
    logger.info("=" * 70)
    
    db = SessionLocal()
    repo = DataRepository(db)
    
    indices = [
        "NIFTY 50",
        "NIFTY BANK",
        "NIFTY IT",
        "NIFTY AUTO",
        "NIFTY PHARMA",
        "NIFTY FMCG",
        "NIFTY METAL",
        "NIFTY INFRA"
    ]
    
    try:
        for index_name in indices:
            try:
                logger.info(f"Updating {index_name}...")
                df = fetch_historical_data(index_name, days=5)
                
                if df is not None and not df.empty:
                    records = repo.save_historical_prices(index_name, df, source='fyers')
                    logger.info(f"  ✓ Added {records} records")
                else:
                    logger.warning(f"  ✗ No data received")
                    
            except Exception as e:
                logger.error(f"  ✗ Error: {str(e)}")
        
        logger.info("Index update complete")
        
    finally:
        db.close()

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
