"""Quick test of trending scanner module"""
import sys
sys.path.insert(0, 'c:/AlgoTrading/backend')

from app.database import SessionLocal
from app.trending_scanner import calculate_trending_stocks

db = SessionLocal()
try:
    print("Testing PRICE_SHOCKER filter...")
    results = calculate_trending_stocks(db, 'PRICE_SHOCKER', limit=5)
    print(f"Found {len(results)} price shockers")
    for r in results[:3]:
        print(f"  {r['symbol']}: {r['change_pct']:+.1f}% - {r['reasons']}")
finally:
    db.close()
