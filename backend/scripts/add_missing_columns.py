"""Fix historical_prices table schema"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import engine
from sqlalchemy import text

columns = [
    ("deliverable_qty", "BIGINT"),
    ("delivery_pct", "FLOAT"),
    ("macd", "FLOAT"),
    ("macd_signal", "FLOAT"),
    ("macd_histogram", "FLOAT"),
    ("stoch_k", "FLOAT"),
    ("stoch_d", "FLOAT"),
    ("bb_upper", "FLOAT"),
    ("bb_middle", "FLOAT"),
    ("bb_lower", "FLOAT"),
    ("adx", "FLOAT"),
    ("obv", "BIGINT"),
    ("high_20d", "FLOAT"),
    ("is_breakout", "BOOLEAN"),
    ("trend_7d", "FLOAT"),
    ("trend_30d", "FLOAT")
]

with engine.connect() as conn:
    for col_name, col_type in columns:
        try:
            sql = text(f"ALTER TABLE historical_prices ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
            conn.execute(sql)
            conn.commit()
            print(f"✅ {col_name}")
        except Exception as e:
            print(f"⚠️  {col_name}: {str(e)[:50]}")

print("\n✅ DONE - Refresh screener now!")
