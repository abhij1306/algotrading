#!/usr/bin/env python3
"""
Daily Health Check Script
Verifies database integrity and Fyers token validity

Run this daily via cron:
0 7 * * * /path/to/venv/bin/python /path/to/data_health_check.py
"""
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.data_layer.provider import DataProvider
from app.data_layer.exceptions import MissingTokenError


def check_database_integrity():
    """Verify database tables and key constraints"""
    db = SessionLocal()
    try:
        from app.database import Company, HistoricalPrice, FinancialStatement
        
        # Check table counts
        company_count = db.query(Company).count()
        price_count = db.query(HistoricalPrice).count()
        financial_count = db.query(FinancialStatement).count()
        
        print(f"✅ Database Tables:")
        print(f"   Companies: {company_count}")
        print(f"   Historical Prices: {price_count}")
        print(f"   Financial Statements: {financial_count}")
        
        # Check for orphaned records
        orphaned = db.execute("""
            SELECT COUNT(*) FROM historical_prices hp
            LEFT JOIN companies c ON hp.company_id = c.id
            WHERE c.id IS NULL
        """).scalar()
        
        if orphaned > 0:
            print(f"⚠️  Found {orphaned} orphaned price records")
            return False
        
        return True
    
    except Exception as e:
        print(f"❌ Database check failed: {e}")
        return False
    finally:
        db.close()


def check_fyers_token():
    """Verify Fyers access token is valid"""
    try:
        from app.fyers_direct import get_fyers_client
        
        client = get_fyers_client()
        profile = client.get_profile()
        
        print(f"✅ Fyers Token: Valid")
        print(f"   User: {profile.get('name', 'Unknown')}")
        return True
    
    except Exception as e:
        if "token" in str(e).lower() or "401" in str(e):
            print(f"❌ Fyers Token: EXPIRED or INVALID")
            print(f"   Action Required: Re-authenticate at /api/auth/fyers")
        else:
            print(f"⚠️  Fyers API: Unreachable ({e})")
        return False


def check_data_freshness():
    """Verify data is recent (within 2 days for daily data)"""
    db = SessionLocal()
    try:
        from app.database import HistoricalPrice
        
        latest = db.query(HistoricalPrice)\
            .order_by(HistoricalPrice.date.desc())\
            .first()
        
        if not latest:
            print("❌ No historical data found")
            return False
        
        days_old = (datetime.now().date() - latest.date).days
        
        if days_old > 2:
            print(f"⚠️  Data is {days_old} days old")
            print(f"   Action Required: Run update_bhavcopy.py")
            return False
        
        print(f"✅ Data Freshness: {latest.date} ({days_old} days old)")
        return True
    
    finally:
        db.close()


def main():
    """Run all health checks"""
    print("=" * 60)
    print(f"Daily Health Check - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    checks = [
        ("Database Integrity", check_database_integrity),
        ("Fyers Token", check_fyers_token),
        ("Data Freshness", check_data_freshness)
    ]
    
    results = []
    for name, check_fn in checks:
        print(f"\n{name}:")
        results.append(check_fn())
    
    print("\n" + "=" * 60)
    if all(results):
        print("✅ All checks passed")
        return 0
    else:
        print("⚠️  Some checks failed - review above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
