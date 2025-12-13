"""
Migration script: SQLite to PostgreSQL
Transfers all data from SQLite screener.db to PostgreSQL
"""

import sys
sys.path.insert(0, '.')

from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from backend.app.database import Base, Company, HistoricalPrice, FinancialStatement, QuarterlyResult, DataUpdateLog
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv('database/.env')

def migrate_data():
    """Migrate data from SQLite to PostgreSQL"""
    
    print("="*80)
    print("SQLite to PostgreSQL Migration")
    print("="*80)
    
    # SQLite connection
    sqlite_path = Path('backend/data/screener.db')
    if not sqlite_path.exists():
        print(f"\n❌ SQLite database not found at: {sqlite_path}")
        print("Nothing to migrate. Exiting.")
        return
    
    sqlite_url = f"sqlite:///{sqlite_path}"
    sqlite_engine = create_engine(sqlite_url, echo=False)
    SqliteSession = sessionmaker(bind=sqlite_engine)
    
    # PostgreSQL connection
    pg_host = os.getenv('DB_HOST', 'localhost')
    pg_port = os.getenv('DB_PORT', '5432')
    pg_db = os.getenv('DB_NAME', 'algotrading')
    pg_user = os.getenv('DB_USER', 'postgres')
    pg_password = os.getenv('DB_PASSWORD', '')
    
    postgres_url = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_db}"
    
    print("\n📋 Connection Info:")
    print(f"  SQLite: {sqlite_path}")
    print(f"  PostgreSQL: {pg_host}:{pg_port}/{pg_db}")
    
    try:
        postgres_engine = create_engine(postgres_url, echo=False)
        print("\n✅ Connected to PostgreSQL")
    except Exception as e:
        print(f"\n❌ Failed to connect to PostgreSQL: {str(e)}")
        print("\nPlease ensure:")
        print("1. PostgreSQL is running")
        print("2. Database 'algotrading' exists")
        print("3. Credentials in database/.env are correct")
        return
    
    # Create PostgreSQL schema
    print("\n📊 Creating PostgreSQL schema...")
    Base.metadata.create_all(bind=postgres_engine)
    print("✅ Schema created")
    
    # Create sessions
    sqlite_session = SqliteSession()
    PostgresSession = sessionmaker(bind=postgres_engine)
    postgres_session = PostgresSession()
    
    # Migration stats
    stats = {}
    
    # Migrate each table
    tables = [
        (Company, 'companies'),
        (HistoricalPrice, 'historical_prices'),
        (FinancialStatement, 'financial_statements'),
        (QuarterlyResult, 'quarterly_results'),
        (DataUpdateLog, 'data_update_logs'),
    ]
    
    print("\n📦 Migrating data...")
    print("-"*80)
    
    for model, table_name in tables:
        try:
            # Count records in SQLite
            sqlite_count = sqlite_session.query(model).count()
            
            if sqlite_count == 0:
                print(f"  {table_name}: 0 records (skipped)")
                stats[table_name] = {'sqlite': 0, 'postgres': 0, 'status': 'empty'}
                continue
            
            # Fetch all records from SQLite
            records = sqlite_session.query(model).all()
            
            # Insert into PostgreSQL
            for record in records:
                # Create new instance with same data
                postgres_session.merge(record)
            
            postgres_session.commit()
            
            # Verify count in PostgreSQL
            postgres_count = postgres_session.query(model).count()
            
            status = '✅' if sqlite_count == postgres_count else '⚠️'
            print(f"  {table_name}: {sqlite_count} → {postgres_count} {status}")
            
            stats[table_name] = {
                'sqlite': sqlite_count,
                'postgres': postgres_count,
                'status': 'success' if sqlite_count == postgres_count else 'mismatch'
            }
            
        except Exception as e:
            print(f"  {table_name}: ❌ Error - {str(e)}")
            stats[table_name] = {'status': 'failed', 'error': str(e)}
            postgres_session.rollback()
    
    # Close sessions
    sqlite_session.close()
    postgres_session.close()
    
    # Summary
    print("\n" + "="*80)
    print("Migration Summary")
    print("="*80)
    
    total_sqlite = sum(s.get('sqlite', 0) for s in stats.values())
    total_postgres = sum(s.get('postgres', 0) for s in stats.values())
    
    print(f"\nTotal records migrated: {total_postgres:,}")
    print(f"Tables processed: {len(stats)}")
    
    success = all(s.get('status') in ['success', 'empty'] for s in stats.values())
    
    if success and total_postgres > 0:
        print("\n✅ Migration completed successfully!")
        print("\nNext steps:")
        print("1. Update backend/app/database.py to use PostgreSQL")
        print("2. Test the application")
        print("3. After verification, you can delete backend/data/screener.db")
    elif total_postgres == 0:
        print("\n⚠️ No data to migrate (SQLite database is empty)")
        print("PostgreSQL schema created and ready to use.")
    else:
        print("\n⚠️ Migration completed with warnings. Please review the stats above.")
    
    return stats

if __name__ == "__main__":
    migrate_data()
