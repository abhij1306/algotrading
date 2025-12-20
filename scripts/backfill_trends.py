
import sys
import os
from pathlib import Path
import pandas as pd
from sqlalchemy import desc

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent / 'backend'
sys.path.append(str(backend_dir))

from app.database import SessionLocal, Company, HistoricalPrice

def backfill_trends():
    db = SessionLocal()
    try:
        companies = db.query(Company).filter(Company.is_active == True).all()
        print(f"Found {len(companies)} active companies.")
        
        updated_count = 0
        
        for company in companies:
            # Get last 40 days of data to ensure we have enough for 30d trend
            prices = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == company.id
            ).order_by(desc(HistoricalPrice.date)).limit(40).all()
            
            if not prices or len(prices) < 2:
                continue
                
            # Convert to DataFrame for easy calc (prices are desc, so reverse/sort)
            prices.sort(key=lambda x: x.date)
            
            data = {
                'close': [p.close for p in prices],
                'date': [p.date for p in prices]
            }
            df = pd.DataFrame(data)
            
            # Calculate Trends
            if len(df) >= 6:
                trend_7d = df['close'].pct_change(5).iloc[-1] * 100
            else:
                trend_7d = 0.0
                
            if len(df) >= 22:
                trend_30d = df['close'].pct_change(21).iloc[-1] * 100
            else:
                trend_30d = 0.0
            
            # Update Latest Record
            latest_price_obj = prices[-1] # The last one in sorted list (latest date)
            
            latest_price_obj.trend_7d = float(trend_7d)
            latest_price_obj.trend_30d = float(trend_30d)
            
            updated_count += 1
            
            if updated_count % 50 == 0:
                print(f"Updated {updated_count} companies...")
                db.commit()
                
        db.commit()
        print(f"✅ Successfully backfilled trends for {updated_count} companies.")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    backfill_trends()
