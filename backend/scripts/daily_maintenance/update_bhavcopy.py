#!/usr/bin/env python3
"""
Update Bhavcopy (EOD Data)
Downloads end-of-day data for all active companies

Run this daily after market close:
0 18 * * 1-5 /path/to/venv/bin/python /path/to/update_bhavcopy.py
"""
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal, Company
from app.data_fetcher import fetch_historical_data


def update_all_companies(limit: int = None):
    """
    Update EOD data for all active companies
    
    Args:
        limit: Max number of companies to update (for testing)
    """
    db = SessionLocal()
    try:
        # Get active companies
        query = db.query(Company).filter(Company.is_active == True)
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"Updating {total} companies...")
        
        success = 0
        failed = 0
        skipped = 0
        
        for i, company in enumerate(companies, 1):
            try:
                print(f"[{i}/{total}] {company.symbol}...", end=" ", flush=True)
                
                # Fetch last 7 days (will auto-skip if already updated)
                df = fetch_historical_data(company.symbol, days=7)
                
                if df is not None and not df.empty:
                    print(f"✅ ({len(df)} records)")
                    success += 1
                else:
                    print("⚠️  No new data")
                    skipped += 1
            
            except Exception as e:
                print(f"❌ {str(e)[:50]}")
                failed += 1
        
        print("\n" + "=" * 60)
        print(f"Update Complete:")
        print(f"  Success: {success}")
        print(f"  Skipped: {skipped}")
        print(f"  Failed: {failed}")
        print("=" * 60)
    
    finally:
        db.close()


def update_indices():
    """Update key indices (NIFTY50, BANKNIFTY, etc.)"""
    indices = ["NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY AUTO"]
    
    print("\nUpdating Indices...")
    for index in indices:
        try:
            print(f"  {index}...", end=" ", flush=True)
            df = fetch_historical_data(index, days=7)
            if df is not None and not df.empty:
                print(f"✅ ({len(df)} records)")
            else:
                print("⚠️  No data")
        except Exception as e:
            print(f"❌ {e}")


def main():
    """Main execution"""
    print("=" * 60)
    print(f"Bhavcopy Update - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # Update indices first
    update_indices()
    
    # Update all companies
    update_all_companies()
    
    print("\n✅ Bhavcopy update complete")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Limit number of companies (for testing)")
    args = parser.parse_args()
    
    if args.limit:
        update_all_companies(limit=args.limit)
    else:
        main()
