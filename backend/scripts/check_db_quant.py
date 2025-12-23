
from app.database import engine
from sqlalchemy import text
import sys

def check_db():
    try:
        with engine.connect() as conn:
            # Check all tables
            print("\nAll Tables:")
            res = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))
            tables = [r[0] for r in res.fetchall()]
            print(f"Tables in DB: {tables}")
            
            # Check user_stock_portfolios table
            if 'user_stock_portfolios' in tables:
                print("\nTable: user_stock_portfolios")
                res = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'user_stock_portfolios'"))
                columns = [r[0] for r in res.fetchall()]
                print(f"Columns: {columns}")
            
            # Check strategy_contracts table
            if 'strategy_contracts' in tables:
                print("\nTable: strategy_contracts")
                res = conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'strategy_contracts'"))
                columns = [r[0] for r in res.fetchall()]
                print(f"Columns: {columns}")
            
            # Check stock_universe table
            target_univ_table = 'stock_universe' if 'stock_universe' in tables else 'stock_universes' if 'stock_universes' in tables else None
            if target_univ_table:
                print(f"\nTable: {target_univ_table}")
                res = conn.execute(text(f"SELECT id FROM {target_univ_table}"))
                universes = [r[0] for r in res.fetchall()]
                print(f"Available universes: {universes}")
            else:
                print("\n❌ No universe table found!")
            
            # Check if any data exists
            res = conn.execute(text("SELECT count(*) FROM strategy_contracts"))
            count = res.scalar()
            print(f"Total rows: {count}")
            
            # Check lifecycle states
            if 'lifecycle_state' in columns:
                res = conn.execute(text("SELECT DISTINCT lifecycle_state FROM strategy_contracts"))
                states = [r[0] for r in res.fetchall()]
                print(f"Lifecycle states: {states}")
                
                # Seed if necessary
                if count > 0 and (None in states or len(states) == 0):
                    print("Seeding lifecycle_state for existing strategies...")
                    conn.execute(text("UPDATE strategy_contracts SET lifecycle_state = 'RESEARCH', state_since = NOW() WHERE lifecycle_state IS NULL"))
                    conn.commit()
                    print("Seed complete.")
                    
    except Exception as e:
        print(f"Error checking DB: {e}")

if __name__ == "__main__":
    check_db()
