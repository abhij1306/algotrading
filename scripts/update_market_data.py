
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
import time
import pytz

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent / 'backend'
sys.path.append(str(backend_dir))

from app.database import SessionLocal, Company
from app.data_repository import DataRepository
from app.fyers_direct import get_fyers_quotes

# Setup Logging
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def update_market_data():
    """
    Fetch latest market data for all active companies and update technicals
    This script is designed to be run periodically (e.g. cron) or triggered manually.
    """
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        log("🚀 Starting Market Data Update...")
        
        # Get all active companies
        companies = db.query(Company).filter(Company.is_active == True).all()
        log(f"Found {len(companies)} active companies.")
        
        symbols = [c.symbol for c in companies]
        
        # Batch process symbols (Fyers limit is usually 50-100 per call)
        batch_size = 50
        updated_count = 0
        failed_count = 0
        
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            log(f"Processing batch {i//batch_size + 1}/{(len(symbols)//batch_size)+1} ({len(batch)} symbols)...")
            
            try:
                # 1. Fetch Live Quotes (Snapshot)
                # Note: For daily candles, we ideally want OHLC from history API.
                # But for 'auto update whenever price is updated', we might mean LIVE updates?
                # The user likely means End-of-Day or Intraday snapshot to update indicators.
                # Assuming this runs during/after market, we fetch quotes to update CURRENT price action.
                # However, calculate_features needs HISTORY.
                # So we should ideally fetch today's candle or treat 'quote' as today's candle so far.
                
                quotes = get_fyers_quotes(batch)
                
                if not quotes:
                    log("⚠️ No quotes received for batch.")
                    continue
                
                # 2. Update Database
                import pandas as pd
                
                for symbol, quote in quotes.items():
                    try:
                        # Construct a single-row DataFrame representing 'today'
                        # This allows us to use save_historical_prices which handles logic + indicators
                        
                        data = {
                            'Open': [float(quote.get('open_price', 0))],
                            'High': [float(quote.get('high_price', 0))],
                            'Low': [float(quote.get('low_price', 0))],
                            'Close': [float(quote.get('ltp', 0))],
                            'Volume': [int(quote.get('volume', 0))],
                            'Adj Close': [float(quote.get('ltp', 0))]
                        }
                        
                        # Use today's date
                        # Note: If running after midnight, this should be 'yesterday' if market closed?
                        # For now, we assume this runs ON the trading day.
                        today = datetime.now().date()
                        
                        df = pd.DataFrame(data, index=[pd.Timestamp(today)])
                        
                        # 3. Save & Calculate Indicators (Auto-Triggered)
                        # save_historical_prices calls compute_features implicitly
                        repo.save_historical_prices(symbol, df, source='fyers_live')
                        updated_count += 1
                        
                    except Exception as e:
                        failed_count += 1
                        # log(f"❌ Failed to update {symbol}: {e}") # Verbose
                        pass
                
                # Sleep briefly to avoid rate limits if needed
                time.sleep(0.5)
                
            except Exception as e:
                log(f"❌ Batch failed: {e}")
                
        log(f"✅ Update Completed. Updated: {updated_count}, Failed: {failed_count}")
        
    except Exception as e:
        log(f"❌ Critical Error: {str(e)}")
    finally:
        db.close()

if __name__ == "__main__":
    update_market_data()
