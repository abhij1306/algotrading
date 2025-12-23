
import sys
import os
from pathlib import Path
from sqlalchemy import create_engine

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.database import Base, get_env_file
from app.utils.env_loader import load_dotenv

# Load Env
env_path = get_env_file()
load_dotenv(env_path)

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'algotrading')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

def update_schema():
    print(f"Updating Database Schema: {DB_NAME} at {DB_HOST}...")
    try:
        engine = create_engine(DATABASE_URL)
        print("Connected. Inspecting tables...")
        
        # specific tables to create
        target_tables = ['strategy_metadata', 'live_portfolio_states']
        
        # Filter metadata
        subset_metadata = Base.metadata
        # We can't easily deep copy metadata, but we can call create_all with 'tables' arg
        
        tables_to_create = [Base.metadata.tables[t] for t in target_tables if t in Base.metadata.tables]
        
        print(f"Creating tables: {target_tables}")
        Base.metadata.create_all(bind=engine, tables=tables_to_create)
        
        print("[SUCCESS] Schema updated. Targeted tables created.")
    except Exception as e:
        print(f"[ERROR] Schema update failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    update_schema()
