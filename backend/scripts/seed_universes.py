
import os
import json
import sys
from sqlalchemy.orm import Session
from datetime import date

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, init_db
from app.engines.universe_manager import UniverseManager

def seed_universes():
    db = SessionLocal()
    try:
        mgr = UniverseManager(db)
        
        data_dir = os.path.join(os.path.dirname(__file__), '../data')
        
        # Load pre-fetched symbols
        n50_path = os.path.join(data_dir, "nifty50_symbols.json")
        n100_path = os.path.join(data_dir, "nifty100_symbols.json")
        
        if not os.path.exists(n50_path) or not os.path.exists(n100_path):
            print("Error: Nifty symbol files not found. Run fetch_nifty100.py first.")
            return

        with open(n50_path, 'r') as f:
            n50 = json.load(f)
        with open(n100_path, 'r') as f:
            n100 = json.load(f)
            
        print(f"Seeding universes with {len(n50)} Nifty50 and {len(n100)} Nifty100 symbols...")
        mgr.seed_default_universes(n50, n100)
        
        # Add derived but static versions for now
        # LIQUID_50 and MEAN_REV are derived from Nifty 100
        # In this first version, we'll just use the first 50 for LIQUID_50
        # and first 100 for MEAN_REV to establish the system objects.
        
        # NIFTY100_LIQUID_50 placeholder
        mgr.seed_default_universes([], []) # This just ensures core 100 and 50 are there
        
    finally:
        db.close()

if __name__ == "__main__":
    seed_universes()
