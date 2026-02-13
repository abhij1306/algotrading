"""
Load Index Universe Data
Loads index constituents into index_membership table.
"""
import sys
import os
from pathlib import Path
from datetime import datetime
import pandas as pd

# Add backend to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import SessionLocal
from app.models.index_membership import IndexMembership
from app.constants.indices import STOCK_INDICES

def load_index_membership():
    db = SessionLocal()
    try:
        # 1. Path to clean snapshots (if available)
        csv_file = Path("nse_data/index_universe/processed/clean_universe/nifty50_clean_snapshots.csv")

        if csv_file.exists():
            print(f"📖 Reading {csv_file}...")
            df = pd.read_csv(csv_file)
            df['date'] = pd.to_datetime(df['date'])

            # Implementation of interval detection logic for historical tracking
            # (As described in the blueprint)
            # ...
            print("✅ CSV loading logic not fully implemented yet, falling back to current indices.")

        # 2. Fallback to STOCK_INDICES constant for current constituents
        print(f"📥 Loading current constituents from STOCK_INDICES...")

        for index_id, info in STOCK_INDICES.items():
            if index_id == "ALL": continue

            symbols = info.get("symbols", [])
            if not symbols:
                continue

            print(f"   Processing {index_id} ({len(symbols)} symbols)...")

            # Mark existing ones as ended or just refresh if simpler for now
            # For a production loader, we should handle start/end dates properly.
            # Here we'll ensure they are active.

            for symbol in symbols:
                existing = db.query(IndexMembership).filter(
                    IndexMembership.index_name == index_id,
                    IndexMembership.symbol == symbol,
                    IndexMembership.end_date.is_(None)
                ).first()

                if not existing:
                    membership = IndexMembership(
                        index_name=index_id,
                        symbol=symbol,
                        start_date=datetime(2020, 1, 1).date(),
                        end_date=None
                    )
                    db.add(membership)

        db.commit()
        print("✅ Index membership table updated.")

    except Exception as e:
        print(f"❌ Error loading index membership: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    load_index_membership()
