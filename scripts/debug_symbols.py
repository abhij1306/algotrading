import sys
from pathlib import Path
from sqlalchemy import or_

# Add parent directory to path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from backend.app.database import SessionLocal, Company

def check_bo_symbols():
    db = SessionLocal()
    try:
        # Check for symbols ending in BO or containing -BO
        bo_symbols = db.query(Company).filter(
            or_(
                Company.symbol.like('%-BO'),
                Company.symbol.like('%.BO'),
                Company.symbol.like('%BO%') # Broad check to see what's going on
            )
        ).limit(50).all()
        
        print(f"Found {len(bo_symbols)} potential 'BO' symbols:")
        for c in bo_symbols:
            print(f" - {c.symbol} (Name: {c.name})")

        # Also check regular symbols to compare
        print("\nRegular symbols sample:")
        regular = db.query(Company).limit(5).all()
        for c in regular:
            print(f" - {c.symbol}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_bo_symbols()
