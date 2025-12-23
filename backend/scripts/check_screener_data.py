import sys
import os

# Add backend directory to sys.path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from app.database import SessionLocal, Company, HistoricalPrice, FinancialStatement
from sqlalchemy import func, desc, and_

def check_data():
    db = SessionLocal()
    try:
        print("Checking Companies...")
        company_count = db.query(Company).count()
        print(f"Total Companies: {company_count}")
        
        active_companies = db.query(Company).filter(Company.is_active == True).count()
        print(f"Active Companies: {active_companies}")
        
        print("\nChecking Historical Prices...")
        price_count = db.query(HistoricalPrice).count()
        print(f"Total Historical Prices: {price_count}")
        
        if price_count > 0:
            latest_date = db.query(func.max(HistoricalPrice.date)).scalar()
            print(f"Latest Price Date: {latest_date}")
            
            # Check price count for latest date
            latest_price_count = db.query(HistoricalPrice).filter(HistoricalPrice.date == latest_date).count()
            print(f"Prices on Latest Date: {latest_price_count}")
            
        print("\nChecking Screener Query Logic...")
        # Reproduce query from screener.py
        latest_prices_subquery = db.query(
            HistoricalPrice.company_id,
            func.max(HistoricalPrice.date).label('latest_date')
        ).group_by(HistoricalPrice.company_id).subquery()
        
        companies_query = db.query(Company).join(
            latest_prices_subquery,
            Company.id == latest_prices_subquery.c.company_id
        ).join(
            HistoricalPrice,
            and_(
                HistoricalPrice.company_id == Company.id,
                HistoricalPrice.date == latest_prices_subquery.c.latest_date
            )
        ).filter(Company.is_active == True)
        
        query_count = companies_query.count()
        if query_count == 0:
            print("No results found in query.")
        else:
            print("Running feature computation check on first 5 items...")
            results = companies_query.limit(5).all()
            fetched_companies = results
            fetched_hist_prices = [] # We don't have hist prices in this query

            fetched_hist_prices = [r[1] for r in results]
            symbols = [c.symbol for c in fetched_companies]
            
            from app.data_repository import DataRepository
            repo = DataRepository(db)
            bulk_hist = repo.get_bulk_historical_prices(symbols, days=200)
            
            from app.indicators import compute_features
            
            for i, company in enumerate(fetched_companies):
                hist_price = fetched_hist_prices[i]
                hist = bulk_hist.get(company.symbol)
                if hist is not None and not hist.empty:
                    try:
                        features = compute_features(company.symbol, hist)
                        print(f"Computed features for {company.symbol}: OK")
                    except Exception as e:
                        print(f"Error computing features for {company.symbol}: {e}")
                else:
                    print(f"No history for {company.symbol}")

        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_data()
