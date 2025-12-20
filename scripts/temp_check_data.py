from backend.app.database import SessionLocal, Company, HistoricalPrice
import json

def check_data():
    db = SessionLocal()
    symbols = ['RELIANCE', 'TCS', 'SBIN']
    results = {}
    
    for sym in symbols:
        c = db.query(Company).filter(Company.symbol == sym).first()
        if c:
            count = db.query(HistoricalPrice).filter(HistoricalPrice.company_id == c.id).count()
            results[sym] = {"count": count, "found": True}
        else:
            results[sym] = {"count": 0, "found": False}
    
    # Check total symbols count
    total_symbols = db.query(Company).count()
    results["_total_symbols"] = total_symbols
    
    print(json.dumps(results, indent=2))
    db.close()

if __name__ == "__main__":
    check_data()
