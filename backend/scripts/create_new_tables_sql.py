
import os
import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file

def create_tables():
    # Load env
    env_path = get_env_file()
    load_dotenv(env_path)
    
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = os.getenv('DB_PORT', '5432')
    DB_NAME = os.getenv('DB_NAME', 'algotrading')
    DB_USER = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    
    print(f"Connecting via SQLAlchemy: {DATABASE_URL.replace(DB_PASSWORD, '***')}")
    
    DDL_STATEMENTS = [
        """
        CREATE TABLE IF NOT EXISTS strategy_metadata (
            strategy_id VARCHAR(50) PRIMARY KEY,
            display_name VARCHAR(100),
            description TEXT,
            regime_notes TEXT,
            risk_profile JSON,
            lifecycle_status VARCHAR(20) DEFAULT 'RESEARCH',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS live_portfolio_states (
            id SERIAL PRIMARY KEY,
            portfolio_id INTEGER NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_equity FLOAT,
            cash_balance FLOAT,
            deployed_capital FLOAT,
            current_drawdown_pct FLOAT,
            is_breached BOOLEAN DEFAULT FALSE,
            breach_details VARCHAR(255),
            strategy_performance JSON DEFAULT '{}',
            FOREIGN KEY (portfolio_id) REFERENCES research_portfolios(id) ON DELETE CASCADE
        );
        """
    ]

    try:
        engine = create_engine(DATABASE_URL)
        with engine.connect() as conn:
            for sql in DDL_STATEMENTS:
                print(f"Executing DDL...")
                conn.execute(text(sql))
                conn.commit()
        print("[SUCCESS] New tables created via SQLAlchemy Raw SQL.")
    except Exception as e:
        print(f"[ERROR] SQL Execution Failed: {e}")

if __name__ == "__main__":
    create_tables()
