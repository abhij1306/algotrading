
import sys
from pathlib import Path
from sqlalchemy import create_engine, inspect

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file
from app.utils.env_loader import load_dotenv
import os

def list_tables():
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
    
    print("Existing Tables:")
    for table_name in inspector.get_table_names():
        print(f" - {table_name}")
        
    print("\nColumns in research_portfolios:")
    for col in inspector.get_columns("research_portfolios"):
        print(f" - {col['name']} ({col['type']})")

if __name__ == "__main__":
    list_tables()
