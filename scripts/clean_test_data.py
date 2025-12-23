import sys
from pathlib import Path
import os
from sqlalchemy import text

# Add parent directory to path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from backend.app.database import SessionLocal, engine

def clean_test_data():
    print("🧹 Starting Database Cleanup...")
    db = SessionLocal()
    try:
        # List of tables to clear (Order matters for foreign keys)
        # Transactional / User Data
        tables_to_clear = [
            "computed_risk_metrics",
            "portfolio_positions",
            "user_portfolios",
            
            "paper_trades",
            "paper_orders",
            "paper_positions",
            "paper_funds",
            
            "smart_trader_signals",
            "agent_audit_logs",
            "action_center"
        ]

        for table in tables_to_clear:
            print(f"   Deleting from: {table}...")
            # Use raw SQL for speed and simplicity in bulk delete
            db.execute(text(f"DELETE FROM {table}"))
        
        db.commit()
        print("✅ All test data cleared successfully!")
        print("   (Preserved master data: Companies, Prices, Financials, Config)")

    except Exception as e:
        print(f"❌ Error cleaning data: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    clean_test_data()
