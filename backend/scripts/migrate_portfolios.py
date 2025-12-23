import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.database import DATABASE_URL

def migrate():
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        print("Checking research_portfolios table...")
        
        # Helper to add column if not exists
        def add_column(col_name, col_type):
            try:
                conn.execute(text(f"ALTER TABLE research_portfolios ADD COLUMN {col_name} {col_type};"))
                print(f"Added column {col_name}")
            except Exception as e:
                # Likely already exists
                print(f"Column {col_name} might already exist or error: {e}")
                
        add_column("description", "VARCHAR(500)")
        add_column("benchmark", "VARCHAR(50) DEFAULT 'NIFTY 50'")
        add_column("initial_capital", "FLOAT DEFAULT 100000.0")
        
        conn.commit()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
