
from sqlalchemy import create_engine, inspect
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file
from app.utils.env_loader import load_dotenv
import os

def check():
    env_path = get_env_file()
    load_dotenv(env_path)
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = os.getenv('DB_PORT', '5432')
    DB_NAME = os.getenv('DB_NAME', 'algotrading')
    DB_USER = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    
    engine = create_engine(DATABASE_URL)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"Tables: {tables}")
    
    if "portfolio_policies" in tables:
        print("portfolio_policies EXISTS")
        cols = [c['name'] for c in inspector.get_columns("portfolio_policies")]
        print(f"Columns: {cols}")
    else:
        print("portfolio_policies MISSING")

if __name__ == "__main__":
    check()
