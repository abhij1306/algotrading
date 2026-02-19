"""Add categorization columns to companies table"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy as sqla

from app.database import engine


def add_columns():
    with engine.connect() as conn:
        # Check if columns already exist
        inspector = sqla.inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('companies')]

        if 'broad_market' not in columns:
            conn.execute(sqla.text('ALTER TABLE companies ADD COLUMN broad_market VARCHAR(50)'))
            print("Added column: broad_market")
        else:
            print("Column already exists: broad_market")

        if 'sector_index' not in columns:
            conn.execute(sqla.text('ALTER TABLE companies ADD COLUMN sector_index VARCHAR(50)'))
            print("Added column: sector_index")
        else:
            print("Column already exists: sector_index")

        conn.commit()
        print("Done!")

if __name__ == "__main__":
    add_columns()
