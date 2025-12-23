
from sqlalchemy import create_engine, text
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
    with engine.connect() as conn:
        # Add missing column
        try:
            conn.execute(text("ALTER TABLE portfolio_policies ADD COLUMN IF NOT EXISTS max_strategy_allocation_percent FLOAT DEFAULT 25.0;"))
            conn.commit()
            print("Added max_strategy_allocation_percent column")
        except Exception as e:
            print(f"Column add error: {e}")

if __name__ == "__main__":
    check()
