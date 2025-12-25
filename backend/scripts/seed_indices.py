import sys
import os
from datetime import datetime
import traceback
import json

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, StockUniverse

def seed_missing_indices():
    db = SessionLocal()
    
    new_indices = [
        {
            "id": "NIFTY_MIDCAP_100",
            "description": "Nifty Midcap 100 Index",
            "rebalance_frequency": "SEMI_ANNUAL",
            "selection_rules": {"universe": "NIFTY_MIDCAP_100"}
        },
        {
            "id": "NIFTY_FIN_SERVICE",
            "description": "Nifty Financial Services Index",
            "rebalance_frequency": "SEMI_ANNUAL",
            "selection_rules": {"universe": "NIFTY_FIN_SERVICE"}
        },
        {
            "id": "NIFTY_SMALLCAP_100",
            "description": "Nifty Smallcap 100 Index",
            "rebalance_frequency": "SEMI_ANNUAL",
            "selection_rules": {"universe": "NIFTY_SMALLCAP_100"}
        },
        {
            "id": "NIFTY_AUTO",
            "description": "Nifty Auto Index",
            "rebalance_frequency": "QUARTERLY",
            "selection_rules": {"universe": "NIFTY_AUTO"},
            "symbols_by_date": {"2024-01-01": ["MARUTI", "M&M", "TATAMOTORS", "HEROMOTOCO", "EICHERMOT"]}
        },
        {
            "id": "NIFTY_FMCG",
            "description": "Nifty FMCG Index",
            "rebalance_frequency": "QUARTERLY",
            "selection_rules": {"universe": "NIFTY_FMCG"},
            "symbols_by_date": {"2024-01-01": ["ITC", "HUL", "NESTLEIND", "BRITANNIA", "TATACONSUM"]}
        }
    ]
    
    # Add common dummy data for others if missing
    for idx in new_indices:
        if "symbols_by_date" not in idx:
             idx["symbols_by_date"] = {"2024-01-01": [f"DUMMY_{idx['id']}_1", f"DUMMY_{idx['id']}_2"]}

    print("Seeding missing indices...")
    
    try:
        for idx in new_indices:
            existing = db.query(StockUniverse).filter(StockUniverse.id == idx["id"]).first()
            if not existing:
                try:
                    universe = StockUniverse(
                        id=idx["id"],
                        description=idx["description"],
                        rebalance_frequency=idx["rebalance_frequency"],
                        selection_rules=json.dumps(idx["selection_rules"]),
                        symbols_by_date=idx["symbols_by_date"],
                        created_at=datetime.utcnow()
                    )
                    db.add(universe)
                    db.commit() # Commit each one
                    print(f"Added: {idx['id']}")
                except Exception as e:
                    db.rollback()
                    print(f"Failed to add {idx['id']}: {e}")
                    # traceback.print_exc()
            else:
                print(f"Skipped (Exists): {idx['id']}")
                
    except Exception as e:
        print(f"Global Error: {e}")
    finally:
        db.close()
        print("Seeding Process Finished.")

if __name__ == "__main__":
    seed_missing_indices()
