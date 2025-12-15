
from datetime import datetime
from .database import SessionLocal
from .data_repository import DataRepository
from .data_fetcher import fetch_historical_data
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_daily_market_update():
    """
    Update historical data for all companies.
    Scheduled to run daily after market hours (e.g., 16:00).
    """
    logger.info(f"Starting daily market data update at {datetime.now()}")
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        companies = repo.get_all_companies()
        total = len(companies)
        updated_count = 0
        
        for i, company in enumerate(companies):
            try:
                # Fetch last 5 days just to fill gaps efficiently or use 365 for robustness
                # fetch_historical_data handles incremental updates if data exists
                fetch_historical_data(company.symbol, days=5)
                updated_count += 1
            except Exception as e:
                logger.error(f"Failed to update {company.symbol}: {e}")
                
        logger.info(f"Daily update complete. Updated {updated_count}/{total} companies.")
        
    except Exception as e:
        logger.error(f"Critical error in daily update: {e}")
    finally:
        db.close()
