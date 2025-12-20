from sqlalchemy import text
from pathlib import Path
import sys

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT_DIR))

from backend.app.database import engine

def migrate():
    with engine.begin() as conn:
        print("Altering volume column to BIGINT...")
        conn.execute(text("ALTER TABLE historical_prices ALTER COLUMN volume TYPE BIGINT"))
        print("Altering trades column to BIGINT...")
        conn.execute(text("ALTER TABLE historical_prices ALTER COLUMN trades TYPE BIGINT"))
        print("✅ Migration complete.")

if __name__ == "__main__":
    migrate()
