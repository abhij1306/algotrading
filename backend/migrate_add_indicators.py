"""
Migration script to add technical indicator columns to historical_prices table
and populate them with calculated values
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent))

from app.database import SessionLocal, Company, HistoricalPrice
from app.data_repository import DataRepository
from app.indicators import compute_features
from sqlalchemy import text
import pandas as pd

def add_indicator_columns():
    """Add new columns to historical_prices table"""
    db = SessionLocal()
    
    print("Adding technical indicator columns to historical_prices table...")
    
    # SQL to add columns (PostgreSQL syntax)
    columns_to_add = [
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS ema_20 FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS ema_34 FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS ema_50 FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS rsi FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS atr FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS atr_pct FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS avg_volume FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS volume_percentile FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS high_20d FLOAT",
        "ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS is_breakout BOOLEAN",
    ]
    
    try:
        for sql in columns_to_add:
            db.execute(text(sql))
        db.commit()
        print("✓ Columns added successfully")
    except Exception as e:
        print(f"Error adding columns: {e}")
        db.rollback()
    finally:
        db.close()


def populate_indicators():
    """Calculate and populate indicators for all companies"""
    db = SessionLocal()
    repo = DataRepository(db)
    
    try:
        # Get all active companies
        companies = db.query(Company).filter(Company.is_active == True).all()
        print(f"\nPopulating indicators for {len(companies)} companies...")
        
        success_count = 0
        error_count = 0
        
        for idx, company in enumerate(companies, 1):
            try:
                # Progress indicator
                if idx % 100 == 0:
                    print(f"  Processed {idx}/{len(companies)} companies...")
                
                # Get historical data (last 200 days)
                hist = repo.get_historical_prices(company.symbol, days=200)
                
                if hist is None or hist.empty or len(hist) < 20:
                    error_count += 1
                    continue
                
                # Calculate features
                features = compute_features(company.symbol, hist)
                
                if features is None:
                    error_count += 1
                    continue
                
                # Get the latest price record for this company
                latest_price = db.query(HistoricalPrice).filter(
                    HistoricalPrice.company_id == company.id
                ).order_by(HistoricalPrice.date.desc()).first()
                
                if latest_price:
                    # Update with calculated indicators
                    latest_price.ema_20 = features.get('ema20')
                    latest_price.ema_34 = features.get('ema34')
                    latest_price.ema_50 = features.get('ema50')
                    latest_price.rsi = features.get('rsi')
                    latest_price.atr = features.get('atr')
                    latest_price.atr_pct = features.get('atr_pct')
                    latest_price.avg_volume = features.get('adv20')
                    latest_price.volume_percentile = features.get('vol_percentile')
                    latest_price.high_20d = features.get('20d_high')
                    latest_price.is_breakout = features.get('is_20d_breakout', False)
                    
                    success_count += 1
                
                # Commit every 100 companies
                if success_count % 100 == 0:
                    db.commit()
                    
            except Exception as e:
                print(f"\nError processing {company.symbol}: {e}")
                error_count += 1
                continue
        
        # Final commit
        db.commit()
        
        print(f"\n✓ Migration complete!")
        print(f"  Success: {success_count} companies")
        print(f"  Errors: {error_count} companies")
        
    except Exception as e:
        print(f"Migration failed: {e}")
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    print("=" * 60)
    print("Technical Indicators Migration")
    print("=" * 60)
    
    # Step 1: Add columns
    add_indicator_columns()
    
    # Step 2: Populate indicators
    populate_indicators()
    
    print("\n✓ All done! Technical indicators are now stored in the database.")
    print("  The screener and AI Copilot will now use pre-calculated values.")
