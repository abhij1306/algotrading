
import sys
from pathlib import Path
from sqlalchemy import create_engine, text

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file
from app.utils.env_loader import load_dotenv
import os

def align_schema():
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
        print("Aligning 'research_portfolios' schema...")
        
        # 1. Add missing columns if they don't exist
        # We use IF NOT EXISTS logic via checking information_schema logic or just try/except
        
        sqls = [
            "ALTER TABLE research_portfolios ADD COLUMN IF NOT EXISTS policy_id VARCHAR(50);",
            "ALTER TABLE research_portfolios ADD COLUMN IF NOT EXISTS composition JSON DEFAULT '[]';",
            "ALTER TABLE research_portfolios ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'RESEARCH';",
            "ALTER TABLE research_portfolios ADD COLUMN IF NOT EXISTS description TEXT;",
            "ALTER TABLE research_portfolios ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;"
        ]
        
        for s in sqls:
            try:
                conn.execute(text(s))
                print(f"Executed: {s}")
            except Exception as e:
                print(f"Error executing {s}: {e}")
                
        # 2. Migrate data
        # If promoted_to_live is true, set status = LIVE
        try:
            conn.execute(text("UPDATE research_portfolios SET status = 'LIVE' WHERE promoted_to_live = TRUE;"))
            print("Migrated status.")
        except Exception as e:
            print(f"Migration error (promoted_to_live might not exist): {e}")

        conn.commit()
        print("Schema Alignment Complete.")

if __name__ == "__main__":
    align_schema()
