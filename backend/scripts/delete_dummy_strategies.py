import sys
import os
from datetime import datetime

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, StrategyContract

def delete_dummy_strategies():
    db = SessionLocal()
    
    # Delete my dummy strategies
    dummy_ids = ["MR-NIFTY-VIX-01", "SA-NIFTYIT-01", "MOM-BANKNIFTY-01", "OP-NIFTY-GAMMA-01"]
    
    print("Deleting dummy strategies...")
    for uid in dummy_ids:
        deleted = db.query(StrategyContract).filter(StrategyContract.strategy_id == uid).delete()
        if deleted:
            print(f"Deleted {uid}")
        else:
            print(f"Not found: {uid}")
    
    db.commit()
    db.close()
    print("Cleanup Complete.")
    
    # Show remaining  
    db = SessionLocal()
    remaining = db.query(StrategyContract).all()
    print(f"\nRemaining strategies ({len(remaining)}):")
    for s in remaining:
        print(f"  - {s.strategy_id} ({s.lifecycle_state})")
    db.close()

if __name__ == "__main__":
    delete_dummy_strategies()
