"""
Database Migration: Add Missing Constraints
Run this script to add foreign key and check constraints for data integrity
"""
from sqlalchemy import text
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

# Database connection string
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/smarttrader")

def run_migration():
    """Execute migration SQL"""
    print("🚀 Starting database migration...")
    
    # Parse connection string
    # Format: postgresql://user:password@host:port/database
    
    # Try using app's resilient env loader if basic load_dotenv failed
    if "postgres" in DB_URL and "password" in DB_URL:
        # This implies it used the default fallback string in os.getenv
        # Try loading explicitly from parent dirs
        print("⚠️  Default DB_URL detected. Attempting resilient load...")
        try:
            from app.utils.env_loader import load_dotenv as resilient_load
            # Resolve path to .env
            current_dir = os.path.dirname(os.path.abspath(__file__))
            backend_dir = os.path.dirname(current_dir) # c:\AlgoTrading\backend
            root_dir = os.path.dirname(backend_dir)    # c:\AlgoTrading
            
            env_path = os.path.join(backend_dir, ".env")
            if os.path.exists(env_path):
                resilient_load(env_path)
                print(f"   Loaded .env from: {env_path}")
            else:
                env_path_root = os.path.join(root_dir, ".env")
                if os.path.exists(env_path_root):
                    resilient_load(env_path_root)
                    print(f"   Loaded .env from: {env_path_root}")
        except ImportError:
            print("   Could not import env_loader")
    
    # Re-fetch after potential reload
    FINAL_DB_URL = os.getenv("DATABASE_URL", "")
    if not FINAL_DB_URL:
        FINAL_DB_URL = DB_URL # Fallback to initial
        
    conn_str = FINAL_DB_URL.replace("postgresql://", "")
    if "@" not in conn_str:
        print(f"❌ Invalid DB connection string format. Got length: {len(FINAL_DB_URL)}")
        return

    user_pass, host_db = conn_str.split("@")
    username, password = user_pass.split(":")
    host_port, database = host_db.split("/")
    host, port = host_port.split(":") if ":" in host_port else (host_port, "5432")
    
    print(f"ℹ️  Connecting to: User='{username}', Host='{host}', Port='{port}', DB='{database}'")
    print(f"ℹ️  Password length: {len(password)}")
    
    conn = None
    cursor = None
    
    try:
        # Connect to database
        conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=username,
            password=password
        )
        conn.autocommit = False
        cursor = conn.cursor()
        
        print("✅ Connected to database")
        
        # ========================================
        # Add CHECK Constraints
        # ========================================
        
        print("\n📋 Adding CHECK constraints...")
        
        # Portfolio policies percentage constraints
        try:
            cursor.execute("""
                ALTER TABLE portfolio_policies 
                ADD CONSTRAINT chk_cash_reserve 
                CHECK (cash_reserve_percent BETWEEN 0 AND 100);
            """)
            print("  ✅ Added chk_cash_reserve")
        except psycopg2.Error as e:
            print(f"  ⚠️  chk_cash_reserve already exists or error: {e}")
            if conn: conn.rollback()
        
        try:
            cursor.execute("""
                ALTER TABLE portfolio_policies 
                ADD CONSTRAINT chk_max_equity 
                CHECK (max_equity_exposure_percent BETWEEN 0 AND 100);
            """)
            print("  ✅ Added chk_max_equity")
        except psycopg2.Error as e:
            print(f"  ⚠️  chk_max_equity already exists or error: {e}")
            if conn: conn.rollback()
        
        try:
            cursor.execute("""
                ALTER TABLE portfolio_policies 
                ADD CONSTRAINT chk_daily_stop 
                CHECK (daily_stop_loss_percent < 0);
            """)
            print("  ✅ Added chk_daily_stop (must be negative)")
        except psycopg2.Error as e:
            print(f"  ⚠️  chk_daily_stop already exists or error: {e}")
            if conn: conn.rollback()
        
        # ========================================
        # Add Foreign Key Constraints
        # ========================================
        
        print("\n🔗 Adding FOREIGN KEY constraints...")
        
        # backtest_runs -> strategy_metadata
        try:
            cursor.execute("""
                ALTER TABLE backtest_runs
                ADD CONSTRAINT fk_backtest_strategy 
                FOREIGN KEY (strategy_id) 
                REFERENCES strategy_metadata(strategy_id) 
                ON DELETE RESTRICT;
            """)
            print("  ✅ Added fk_backtest_strategy")
        except psycopg2.Error as e:
            print(f"  ⚠️  fk_backtest_strategy already exists or error: {e}")
            if conn: conn.rollback()
        
        # portfolio_positions -> user_portfolios
        try:
            cursor.execute("""
                ALTER TABLE portfolio_positions
                ADD CONSTRAINT fk_position_portfolio 
                FOREIGN KEY (portfolio_id)
                REFERENCES user_portfolios(id) 
                ON DELETE CASCADE;
            """)
            print("  ✅ Added fk_position_portfolio (CASCADE delete)")
        except psycopg2.Error as e:
            print(f"  ⚠️  fk_position_portfolio already exists or error: {e}")
            if conn: conn.rollback()
        
        # research_portfolios -> portfolio_policies
        try:
            cursor.execute("""
                ALTER TABLE research_portfolios
                ADD CONSTRAINT fk_research_policy 
                FOREIGN KEY (policy_id)
                REFERENCES portfolio_policies(id) 
                ON DELETE SET NULL;
            """)
            print("  ✅ Added fk_research_policy")
        except psycopg2.Error as e:
            print(f"  ⚠️  fk_research_policy already exists or error: {e}")
            if conn: conn.rollback()
        
        # ========================================
        # Create ENUM Type for Lifecycle Status
        # ========================================
        
        print("\n🔧 Creating ENUM type for lifecycle_status...")
        
        try:
            cursor.execute("""
                DO $$ 
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lifecycle_enum') THEN
                        CREATE TYPE lifecycle_enum AS ENUM ('RESEARCH', 'PAPER', 'LIVE', 'RETIRED');
                    END IF;
                END $$;
            """)
            print("  ✅ Created lifecycle_enum type")
        except psycopg2.Error as e:
            print(f"  ⚠️  lifecycle_enum type creation failed: {e}")
            if conn: conn.rollback()
        
        # Migrate lifecycle_status column to ENUM (if needed)
        try:
            cursor.execute("""
                ALTER TABLE strategy_metadata 
                ALTER COLUMN lifecycle_status TYPE lifecycle_enum 
                USING lifecycle_status::lifecycle_enum;
            """)
            print("  ✅ Migrated lifecycle_status to ENUM")
        except psycopg2.Error as e:
            print(f"  ⚠️  lifecycle_status migration failed (may already be ENUM): {e}")
            if conn: conn.rollback()
        
        # ========================================
        # Add Indexes for Performance
        # ========================================
        
        print("\n📊 Adding recommended indexes...")
        
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_historical_symbol_date 
                ON historical_prices(symbol, date DESC);
            """)
            print("  ✅ Added idx_historical_symbol_date")
        except psycopg2.Error as e:
            print(f"  ⚠️  idx_historical_symbol_date creation failed: {e}")
            if conn: conn.rollback()
        
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_historical_sector 
                ON historical_prices(sector);
            """)
            print("  ✅ Added idx_historical_sector")
        except psycopg2.Error as e:
            print(f"  ⚠️  idx_historical_sector creation failed: {e}")
            if conn: conn.rollback()
        
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_backtest_strategy_date 
                ON backtest_runs(strategy_id, created_at DESC);
            """)
            print("  ✅ Added idx_backtest_strategy_date")
        except psycopg2.Error as e:
            print(f"  ⚠️  idx_backtest_strategy_date creation failed: {e}")
            if conn: conn.rollback()
        
        # Commit all changes
        if conn: conn.commit()
        print("\n✅ Migration completed successfully!")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        print("💡 Hint: Check your database credentials in .env file")
        if conn: conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            print("🔌 Database connection closed")

if __name__ == "__main__":
    print("=" * 60)
    print("SmartTrader 3.0 - Database Constraint Migration")
    print("=" * 60)
    run_migration()
    print("\n✔️  All done! Database constraints and indexes have been applied.")
