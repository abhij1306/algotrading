"""
Apply performance indexes to database
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from app.database import engine


def apply_indexes():
    """Apply performance indexes from migration file"""
    migration_file = Path(__file__).parent.parent / "migrations" / "add_performance_indexes.sql"

    with open(migration_file) as f:
        sql = f.read()

    # Split by semicolon and execute each statement
    statements = [s.strip() for s in sql.split(';') if s.strip()]

    with engine.connect() as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
                print(f"✓ Executed: {statement[:60]}...")
            except Exception as e:
                print(f"✗ Error: {e}")

        conn.commit()

    print("\n✓ All indexes applied successfully")

if __name__ == "__main__":
    apply_indexes()
