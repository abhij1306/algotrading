"""
Add NIFTY50 and BANKNIFTY to companies table so backtest can work
"""

from app.database import SessionLocal, Company

db = SessionLocal()

try:
    # Add NIFTY50
    nifty = db.query(Company).filter(Company.symbol == 'NIFTY50').first()
    if not nifty:
        nifty = Company(
            symbol='NIFTY50',
            name='NIFTY 50 Index',
            sector='Index',
            is_active=True
        )
        db.add(nifty)
        print("✅ Added NIFTY50 to companies")
    else:
        print("ℹ️  NIFTY50 already exists")
    
    # Add BANKNIFTY
    banknifty = db.query(Company).filter(Company.symbol == 'BANKNIFTY').first()
    if not banknifty:
        banknifty = Company(
            symbol='BANKNIFTY',
            name='BANK NIFTY Index',
            sector='Index',
            is_active=True
        )
        db.add(banknifty)
        print("✅ Added BANKNIFTY to companies")
    else:
        print("ℹ️  BANKNIFTY already exists")
    
    db.commit()
    print("\n✅ Database updated successfully")
    
except Exception as e:
    print(f"❌ Error: {e}")
    db.rollback()
finally:
    db.close()
