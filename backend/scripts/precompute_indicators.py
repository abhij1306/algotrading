"""
Pre-compute technical indicators for all stocks and store in database
This eliminates N+1 query problem in the screener
"""

from app.database import SessionLocal, Company, HistoricalPrice
from app.data_repository import DataRepository
from app.indicators import compute_features
import pandas as pd
from datetime import datetime
from sqlalchemy import and_, func

print("=" * 60)
print("PRE-COMPUTING TECHNICAL INDICATORS")
print("=" * 60)

db = SessionLocal()
repo = DataRepository(db)

try:
    # Get all active companies
    companies = db.query(Company).filter(Company.is_active == True).all()
    print(f"\nFound {len(companies)} active companies")
    
    total_updated = 0
    errors = 0
    
    for i, company in enumerate(companies, 1):
        try:
            # Get historical data
            hist = repo.get_historical_prices(company.symbol, days=200)
            
            if hist is None or hist.empty:
                continue
            
            # Compute features using indicators.compute_features
            features = compute_features(company.symbol, hist)
            
            if features is None:
                continue
            
            # Get latest date
            latest_date = hist.index.max()
            
            # Update the latest historical_price record with computed indicators
            latest_price = db.query(HistoricalPrice).filter(
                and_(
                    HistoricalPrice.company_id == company.id,
                    HistoricalPrice.date == latest_date
                )
            ).first()
            
            if latest_price:
                # Update all technical indicators
                latest_price.ema_20 = features.get('ema20')
                latest_price.ema_50 = features.get('ema50')
                latest_price.rsi = features.get('rsi')
                latest_price.atr_14 = features.get('atr')
                latest_price.atr_pct = features.get('atr_pct')
                latest_price.volume_percentile = features.get('vol_percentile')
                latest_price.macd = features.get('macd')
                latest_price.macd_signal = features.get('macd_signal')
                latest_price.adx = features.get('adx')
                latest_price.stoch_k = features.get('stoch_k')
                latest_price.stoch_d = features.get('stoch_d')
                latest_price.bb_upper = features.get('bb_upper')
                latest_price.bb_middle = features.get('bb_middle')
                latest_price.bb_lower = features.get('bb_lower')
                
                total_updated += 1
                
                if total_updated % 100 == 0:
                    db.commit()
                    print(f"Progress: {i}/{len(companies)} - Updated {total_updated} stocks")
            
        except Exception as e:
            errors += 1
            if errors < 10:
                print(f"  ⚠️  Error processing {company.symbol}: {e}")
    
    # Final commit
    db.commit()
    
    print("\n" + "=" * 60)
    print(f"✅ Indicators pre-computed for {total_updated} stocks")
    print(f"❌ Errors: {errors}")
    print("=" * 60)
    
except Exception as e:
    print(f"❌ Fatal error: {e}")
    db.rollback()
finally:
    db.close()
