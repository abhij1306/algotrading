import sys
import os
from sqlalchemy import text

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, engine

def fix_schema():
    print("Checking schema...")
    with engine.connect() as conn:
        try:
            # Check if column exists
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='stock_universes' AND column_name='symbols_by_date'"
            ))
            if result.fetchone():
                print("Column 'symbols_by_date' already exists.")
            else:
                print("Column 'symbols_by_date' missing. Adding it...")
                conn.execute(text("ALTER TABLE stock_universes ADD COLUMN symbols_by_date JSON"))
                conn.commit()
                print("Column added successfully.")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    fix_schema()
