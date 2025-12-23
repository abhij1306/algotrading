from sqlalchemy.orm import Session
from app.database import SessionLocal, Company, HistoricalPrice, engine
from app.data_repository import DataRepository
import pandas as pd
import sys

def recalculate_all():
    db = SessionLocal()
    repo = DataRepository(db)
    
    print("Fetching all companies...")
    companies = repo.get_all_companies()
    total = len(companies)
    
    print(f"Found {total} companies. Starting recalculation...")
    
    for i, company in enumerate(companies):
        try:
            # 1. Get historical data (last 200 days)
            df = repo.get_historical_prices(company.symbol, days=300)
            
            if df is not None and not df.empty and len(df) >= 30:
                 # 2. Force re-save (this triggers indicator calculation in repo.save_historical_prices logic)
                 # Note: repo.save_historical_prices logic calculates indicators for the *latest* record based on passed history.
                 # We need to simulate passing the dataframe to trigger the calculation.
                 # However, repo.save_historical_prices inserts NEW records.
                 # We need to manually invoke the calculation logic part of that function.
                 
                 from app.indicators import compute_features
                 features = compute_features(company.symbol, df)
                 
                 if features:
                     # Update latest record
                     latest_price = db.query(HistoricalPrice).filter(
                         HistoricalPrice.company_id == company.id
                     ).order_by(HistoricalPrice.date.desc()).first()
                     
                     if latest_price:
                         latest_price.macd = features.get('macd')
                         latest_price.macd_signal = features.get('macd_signal')
                         latest_price.adx = features.get('adx')
                         latest_price.stoch_k = features.get('stoch_k')
                         latest_price.stoch_d = features.get('stoch_d')
                         latest_price.bb_upper = features.get('bb_upper')
                         latest_price.bb_middle = features.get('bb_middle')
                         latest_price.bb_lower = features.get('bb_lower')
                         latest_price.obv = features.get('obv')
                         
                         # Also ensure basic trends are updated
                         latest_price.atr_14 = features.get('atr')
                         latest_price.rsi_14 = features.get('rsi')
                         latest_price.trend_7d = features.get('trend_7d')
                         latest_price.trend_30d = features.get('trend_30d')
                         
                         db.commit()
                         sys.stdout.write(f"\r[{i+1}/{total}] Updated {company.symbol}")
                         sys.stdout.flush()
        except Exception as e:
            print(f"\nFailed for {company.symbol}: {e}")
            db.rollback()
            
    print("\nRecalculation complete.")
    db.close()

if __name__ == "__main__":
    recalculate_all()
