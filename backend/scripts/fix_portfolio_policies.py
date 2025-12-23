
from sqlalchemy import create_engine, text
import os
import sys

# Add parent directory to path to import app modules if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import DATABASE_URL

def fix_schema():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        try:
            print("Checking portfolio_policies table schema...")
            # Check if column exists
            result = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'portfolio_policies' AND column_name = 'max_strategy_allocation_percent';"))
            if result.rowcount == 0:
                print("Column 'max_strategy_allocation_percent' missing. Adding it...")
                conn.execute(text("ALTER TABLE portfolio_policies ADD COLUMN max_strategy_allocation_percent FLOAT DEFAULT 25.0;"))
                conn.commit()
                print("Column added successfully.")
            else:
                print("Column 'max_strategy_allocation_percent' already exists.")
                
        except Exception as e:
            print(f"Error checking/updating schema: {e}")

if __name__ == "__main__":
    fix_schema()
