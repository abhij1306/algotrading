
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import os
import sys

# Add parent directory to path to import app modules if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import DATABASE_URL

def fix_schema():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        try:
            # Check if column exists
            print("Checking backtest_runs table schema...")
            result = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'backtest_runs' AND column_name = 'summary_metrics';"))
            if result.rowcount == 0:
                print("Column 'summary_metrics' missing. Adding it...")
                conn.execute(text("ALTER TABLE backtest_runs ADD COLUMN summary_metrics JSON;"))
                conn.commit()
                print("Column added successfully.")
            else:
                print("Column 'summary_metrics' already exists.")
                
        except Exception as e:
            print(f"Error checking/updating schema: {e}")

if __name__ == "__main__":
    fix_schema()
