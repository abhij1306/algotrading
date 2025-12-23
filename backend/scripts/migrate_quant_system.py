"""
Database migrations for Institutional Quant System

Creates 4 new tables:
1. research_portfolios - Portfolio research configurations
2. strategy_lifecycle_log - Strategy state change audit trail  
3. policy_change_log - Portfolio policy change audit trail
4. governance_actions - Automated governance decisions

Updates existing tables:
- strategy_contracts: Add lifecycle_state, state_since, approved_at, approved_by
- stock_universe: Add status, locked, last_rebalance
"""

from sqlalchemy import create_engine, text
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:admin@localhost:5432/algotrading")

def run_migrations():
    """Execute database migrations for quant system"""
    engine = create_engine(DB_URL)
    
    migrations = [
        # 1. Create research_portfolios table
        """
        CREATE TABLE IF NOT EXISTS research_portfolios (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            strategy_ids TEXT[] NOT NULL,
            policy_method VARCHAR(50) NOT NULL,
            policy_settings JSONB,
            created_at TIMESTAMP DEFAULT NOW(),
            promoted_to_live BOOLEAN DEFAULT FALSE,
            promoted_at TIMESTAMP
        );
        """,
        
        # 2. Create strategy_lifecycle_log table
        """
        CREATE TABLE IF NOT EXISTS strategy_lifecycle_log (
            id SERIAL PRIMARY KEY,
            strategy_id VARCHAR(100) NOT NULL,
            timestamp TIMESTAMP DEFAULT NOW(),
            old_state VARCHAR(20),
            new_state VARCHAR(20) NOT NULL,
            reason TEXT,
            changed_by VARCHAR(50) DEFAULT 'SYSTEM'
        );
        """,
        
        # 3. Create policy_change_log table
        """
        CREATE TABLE IF NOT EXISTS policy_change_log (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT NOW(),
            changed_by VARCHAR(50) NOT NULL,
            field_name VARCHAR(100) NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT
        );
        """,
        
        # 4. Create governance_actions table
        """
        CREATE TABLE IF NOT EXISTS governance_actions (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT NOW(),
            action_type VARCHAR(50) NOT NULL,
            target_strategy VARCHAR(100),
            target_portfolio VARCHAR(100),
            reason TEXT NOT NULL,
            auto_generated BOOLEAN DEFAULT FALSE
        );
        """,
        
        # 5. Update strategy_contracts table
        """
        DO $$ 
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='strategy_contracts' AND column_name='lifecycle_state') THEN
                ALTER TABLE strategy_contracts 
                ADD COLUMN lifecycle_state VARCHAR(20) DEFAULT 'RESEARCH',
                ADD COLUMN state_since TIMESTAMP DEFAULT NOW(),
                ADD COLUMN approved_at TIMESTAMP,
                ADD COLUMN approved_by VARCHAR(50);
            END IF;
        END $$;
        """,
        
        # 6. Update stock_universe table
        """
        DO $$ 
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                          WHERE table_name='stock_universe' AND column_name='status') THEN
                ALTER TABLE stock_universe
                ADD COLUMN status VARCHAR(20) DEFAULT 'ACTIVE',
                ADD COLUMN locked BOOLEAN DEFAULT FALSE,
                ADD COLUMN last_rebalance TIMESTAMP;
            END IF;
        END $$;
        """
    ]
    
    with engine.connect() as conn:
        for i, migration in enumerate(migrations, 1):
            try:
                logger.info(f"Running migration {i}/{len(migrations)}...")
                conn.execute(text(migration))
                conn.commit()
                logger.info(f"✅ Migration {i} completed")
            except Exception as e:
                logger.error(f"❌ Migration {i} failed: {e}")
                conn.rollback()
                raise
    
    logger.info("All migrations completed successfully!")

if __name__ == "__main__":
    run_migrations()
