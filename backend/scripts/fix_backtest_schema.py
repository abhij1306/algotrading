
import os
import sys
from sqlalchemy import text

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import engine, Base

def fix_schema():
    print("Verifying and fixing schema for backtesting models...")
    
    # Force create all tables defined in Base
    Base.metadata.create_all(bind=engine)
    
    with engine.connect() as conn:
        # Check if created_at exists in stock_universes
        try:
            conn.execute(text("SELECT created_at FROM stock_universes LIMIT 1"))
            print("stock_universes table is healthy.")
        except Exception:
            print("Adding missing columns to stock_universes...")
            conn.execute(text("ALTER TABLE stock_universes ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
            conn.commit()

        # Check other new tables
        tables = ["user_stock_portfolios", "backtest_runs", "backtest_daily_results", "portfolio_daily_results"]
        for table in tables:
            try:
                conn.execute(text(f"SELECT * FROM {table} LIMIT 1"))
                print(f"Table {table} exists.")
            except Exception:
                print(f"Table {table} might be missing or corrupted. Re-checking...")
    
    print("Schema fix complete.")

if __name__ == "__main__":
    fix_schema()
