"""
Bulk Sector Population Script

This script populates missing sector and industry data for all companies in the database
by scraping from screener.in using the existing extract_sector_industry function.

Usage:
    python -m app.populate_sectors

Features:
- Rate limiting (10 requests/minute)
- Progress tracking
- Error handling and retry logic
- Can resume from last position
"""

import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .database import Company, SessionLocal
from .openrouter_classifier import classify_sector_with_ai

# Rate limiting: 10 requests per minute = 6 seconds per request
DELAY_BETWEEN_REQUESTS = 6

def populate_sectors(resume_from_id: int = 0, limit: int = None):
    """
    Populate missing sector/industry data for companies using OpenRouter AI
    
    Args:
        resume_from_id: Company ID to resume from (for crash recovery)
        limit: Maximum number of companies to process (None = all)
    """
    db = SessionLocal()
    
    try:
        # Get companies missing sector data
        query = db.query(Company).filter(
            (Company.sector == None) | (Company.sector == '') | (Company.sector == 'Unknown')
        )
        
        if resume_from_id > 0:
            query = query.filter(Company.id >= resume_from_id)
        
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"Found {total} companies missing sector data")
        print("Using OpenRouter AI for classification...")
        
        if total == 0:
            print("All companies have sector data!")
            return
        
        updated_count = 0
        failed_count = 0
        
        for i, company in enumerate(companies, 1):
            print(f"\n[{i}/{total}] Processing {company.symbol} - {company.name}...")
            
            try:
                # Use AI to classify sector
                sector_data = classify_sector_with_ai(company.name, company.symbol)
                
                if sector_data and sector_data.get('sector'):
                    company.sector = sector_data['sector']
                    company.industry = sector_data.get('industry', '')
                    db.commit()
                    
                    print(f"  ✅ Updated: Sector={company.sector}, Industry={company.industry}")
                    updated_count += 1
                else:
                    print(f"  ⚠️  AI could not classify {company.symbol}")
                    failed_count += 1
                
            except Exception as e:
                print(f"  ❌ Error processing {company.symbol}: {str(e)}")
                failed_count += 1
                db.rollback()
            
            # Rate limiting - wait before next request
            if i < total:
                print(f"  ⏳ Waiting {DELAY_BETWEEN_REQUESTS}s (rate limit)...")
                time.sleep(DELAY_BETWEEN_REQUESTS)
        
        print(f"\n" + "="*60)
        print(f"SUMMARY")
        print(f"="*60)
        print(f"Total processed: {total}")
        print(f"Successfully updated: {updated_count}")
        print(f"Failed: {failed_count}")
        print(f"Success rate: {(updated_count/total*100):.1f}%")
        
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        raise
    finally:
        db.close()

def get_sector_stats():
    """Get statistics about sector population"""
    db = SessionLocal()
    
    try:
        total = db.query(Company).count()
        with_sector = db.query(Company).filter(
            (Company.sector != None) & (Company.sector != '') & (Company.sector != 'Unknown')
        ).count()
        without_sector = total - with_sector
        
        print(f"\nSector Population Statistics:")
        print(f"  Total companies: {total}")
        print(f"  With sector data: {with_sector} ({with_sector/total*100:.1f}%)")
        print(f"  Missing sector data: {without_sector} ({without_sector/total*100:.1f}%)")
        
        return {
            'total': total,
            'with_sector': with_sector,
            'without_sector': without_sector
        }
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Populate sector data for companies')
    parser.add_argument('--resume-from', type=int, default=0, help='Company ID to resume from')
    parser.add_argument('--limit', type=int, default=None, help='Maximum companies to process')
    parser.add_argument('--stats', action='store_true', help='Show statistics only')
    
    args = parser.parse_args()
    
    if args.stats:
        get_sector_stats()
    else:
        print("Starting bulk sector population...")
        print("="*60)
        get_sector_stats()
        print("="*60)
        
        input("\nPress Enter to start scraping (or Ctrl+C to cancel)...")
        
        populate_sectors(
            resume_from_id=args.resume_from,
            limit=args.limit
        )
