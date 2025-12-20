"""
Data Integrity Validation Script
Checks for symbol coverage, continuous date ranges, and major symbols.
"""
from backend.app.database import SessionLocal, Company, HistoricalPrice
from datetime import timedelta, date
import json

def validate():
    db = SessionLocal()
    
    # 1. Symbol Count Validation
    total_symbols = db.query(Company).count()
    print(f"Total Symbols in Database: {total_symbols}")
    
    # 2. Major Symbols check
    major_symbols = ['RELIANCE', 'TCS', 'SBIN', 'INFY', 'HDFCBANK']
    missing_major = []
    
    for sym in major_symbols:
        c = db.query(Company).filter(Company.symbol == sym).first()
        if not c:
            missing_major.append(sym)
        else:
            count = db.query(HistoricalPrice).filter(HistoricalPrice.company_id == c.id).count()
            print(f"  {sym}: {count} records")
            if count < 2000: # Assuming ~8-9 years of data should be > 2000 trading days
                print(f"    ⚠️ Warning: {sym} has low record count ({count})")

    # 3. Gap check for top symbols
    print("\nChecking for gaps in major symbols...")
    for sym in major_symbols:
        c = db.query(Company).filter(Company.symbol == sym).first()
        if c:
            prices = db.query(HistoricalPrice.date)\
                .filter(HistoricalPrice.company_id == c.id)\
                .order_by(HistoricalPrice.date).all()
            
            if len(prices) > 1:
                gaps = []
                for i in range(1, len(prices)):
                    diff = (prices[i].date - prices[i-1].date).days
                    if diff > 4: # More than a weekend/holiday gap
                        gaps.append({
                            "after": str(prices[i-1].date),
                            "before": str(prices[i].date),
                            "gap_days": diff
                        })
                
                if gaps:
                    print(f"  {sym}: Found {len(gaps)} significant gaps")
                    # print(json.dumps(gaps[:3], indent=2)) # Show first 3 gaps
                else:
                    print(f"  {sym}: ✅ No significant gaps found")

    # Final report
    report = {
        "symbol_count": total_symbols,
        "symbol_count_valid": total_symbols >= 2400,
        "missing_major_symbols": missing_major,
        "status": "PASS" if total_symbols >= 2400 and not missing_major else "FAIL"
    }
    
    with open('data_quality_report.json', 'w') as f:
        json.dump(report, f, indent=2)
    
    print("\n" + "="*40)
    print(f"FINAL STATUS: {report['status']}")
    print("="*40)
    
    db.close()

if __name__ == "__main__":
    validate()
