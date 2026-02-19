"""
Backtest V2 Migration Script
============================
Creates the enhanced backtest tables for V2.
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text

from app.config import get_settings


def migrate():
    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)

    with engine.connect() as conn:
        # Check if tables already exist
        result = conn.execute(text("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='backtest_runs_v2'
        """))

        if result.fetchone():
            print("Backtest V2 tables already exist. Skipping migration.")
            return

        print("Creating Backtest V2 tables...")

        # Create backtest_runs_v2
        conn.execute(text("""
            CREATE TABLE backtest_runs_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(50) UNIQUE NOT NULL,
                user_id VARCHAR(50),
                config JSON,
                asset_type VARCHAR(20) NOT NULL,
                strategy VARCHAR(50) NOT NULL,
                start_date DATE NOT NULL,
                end_date DATE NOT NULL,
                initial_capital FLOAT NOT NULL,
                final_capital FLOAT,
                total_return FLOAT,
                cagr FLOAT,
                annualized_volatility FLOAT,
                sharpe_ratio FLOAT,
                sortino_ratio FLOAT,
                max_drawdown FLOAT,
                max_drawdown_duration INTEGER,
                calmar_ratio FLOAT,
                var_95 FLOAT,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                win_rate FLOAT,
                profit_factor FLOAT,
                avg_trade_return FLOAT,
                avg_win FLOAT,
                avg_loss FLOAT,
                largest_win FLOAT,
                largest_loss FLOAT,
                avg_trade_duration FLOAT,
                status VARCHAR(20) DEFAULT 'running',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        """))

        # Create indexes
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_run_id ON backtest_runs_v2(run_id)
        """))
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_asset_type ON backtest_runs_v2(asset_type)
        """))
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_strategy ON backtest_runs_v2(strategy)
        """))
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_status ON backtest_runs_v2(status)
        """))
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_user_created ON backtest_runs_v2(user_id, created_at)
        """))
        conn.execute(text("""
            CREATE INDEX ix_backtest_v2_strategy_dates ON backtest_runs_v2(strategy, start_date, end_date)
        """))

        # Create equity curve table
        conn.execute(text("""
            CREATE TABLE backtest_equity_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(50) NOT NULL,
                date DATE NOT NULL,
                equity FLOAT NOT NULL,
                cash FLOAT NOT NULL,
                positions_value FLOAT DEFAULT 0,
                drawdown FLOAT DEFAULT 0,
                UNIQUE(run_id, date),
                FOREIGN KEY (run_id) REFERENCES backtest_runs_v2(run_id) ON DELETE CASCADE
            )
        """))

        conn.execute(text("""
            CREATE INDEX ix_equity_curve_run_date ON backtest_equity_points(run_id, date)
        """))

        # Create trades table
        conn.execute(text("""
            CREATE TABLE backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(50) NOT NULL,
                trade_id VARCHAR(50) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                entry_date DATE NOT NULL,
                exit_date DATE,
                entry_price FLOAT NOT NULL,
                exit_price FLOAT,
                quantity INTEGER NOT NULL,
                position_type VARCHAR(10) DEFAULT 'long',
                pnl FLOAT,
                return_pct FLOAT,
                duration_days INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (run_id) REFERENCES backtest_runs_v2(run_id) ON DELETE CASCADE
            )
        """))

        conn.execute(text("""
            CREATE INDEX ix_trades_run_symbol ON backtest_trades(run_id, symbol)
        """))
        conn.execute(text("""
            CREATE INDEX ix_trades_entry_date ON backtest_trades(run_id, entry_date)
        """))

        # Create monthly returns table
        conn.execute(text("""
            CREATE TABLE backtest_monthly_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id VARCHAR(50) NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                return_pct FLOAT NOT NULL,
                UNIQUE(run_id, year, month),
                FOREIGN KEY (run_id) REFERENCES backtest_runs_v2(run_id) ON DELETE CASCADE
            )
        """))

        # Create saved configs table
        conn.execute(text("""
            CREATE TABLE saved_backtest_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(50),
                name VARCHAR(100) NOT NULL,
                description TEXT,
                config JSON NOT NULL,
                asset_type VARCHAR(20) NOT NULL,
                strategy VARCHAR(50) NOT NULL,
                tags JSON DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        conn.execute(text("""
            CREATE INDEX ix_saved_config_user ON saved_backtest_configs(user_id, created_at)
        """))

        # Create comparisons table
        conn.execute(text("""
            CREATE TABLE backtest_comparisons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(50),
                name VARCHAR(100) NOT NULL,
                description TEXT,
                run_ids JSON NOT NULL,
                comparison_metrics JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))

        conn.commit()
        print("✅ Backtest V2 migration complete!")

if __name__ == "__main__":
    migrate()
