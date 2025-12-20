
import sys
import os
from pathlib import Path
from sqlalchemy import text

# Add backend directory to path
backend_dir = Path(__file__).resolve().parent.parent / 'backend'
sys.path.append(str(backend_dir))

from app.database import engine

def add_columns():
    with engine.connect() as conn:
        print("Checking/Adding trend_7d column...")
        try:
            conn.execute(text("ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS trend_7d FLOAT"))
            print("✅ Added trend_7d")
        except Exception as e:
            print(f"⚠️ Error adding trend_7d: {e}")

        print("Checking/Adding trend_30d column...")
        try:
            conn.execute(text("ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS trend_30d FLOAT"))
            print("✅ Added trend_30d")
        except Exception as e:
            print(f"⚠️ Error adding trend_30d: {e}")
            
        conn.commit()
    print("Migration completed.")

if __name__ == "__main__":
    add_columns()
