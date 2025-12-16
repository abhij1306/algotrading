"""
Bulk populate financial data from screener.in for all companies
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, Company, FinancialStatement
from app.screener_scraper import scrape_screener, extract_financials
from datetime import date
import time

def populate_all_financials(limit=None, skip_existing=True):
    """
    Scrape and populate financial data for all companies
    
    Args:
        limit: Maximum number of companies to process (None for all)
        skip_existing: Skip companies that already have financial data
    """
    db = SessionLocal()
    
    try:
        # Get all active companies
        query = db.query(Company).filter(Company.is_active == True)
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"Found {total} companies to process")
        print("=" * 60)
        
        success_count = 0
        skip_count = 0
        error_count = 0
        
        for idx, company in enumerate(companies, 1):
            print(f"\n[{idx}/{total}] Processing {company.symbol} ({company.name})...")
            
            # Check if financial data already exists
            if skip_existing:
                existing = db.query(FinancialStatement).filter(
                    FinancialStatement.company_id == company.id,
                    FinancialStatement.source == 'screener'
                ).first()
                
                if existing:
                    print(f"  ⏭️  Skipping - already has financial data")
                    skip_count += 1
                    continue
            
            try:
                # Scrape data from screener.in
                print(f"  🔍 Scraping screener.in...")
                scraped_data = scrape_screener(company.symbol)
                
                # Extract financial metrics
                financials = extract_financials(scraped_data)
                
                if not financials:
                    print(f"  ❌ No financial data found")
                    error_count += 1
                    time.sleep(2)  # Rate limiting
                    continue
                
                # Update company market cap if available
                if financials.get('market_cap', 0) > 0:
                    company.market_cap = financials['market_cap']
                
                # Create or update financial statement
                existing = db.query(FinancialStatement).filter(
                    FinancialStatement.company_id == company.id,
                    FinancialStatement.source == 'screener'
                ).first()
                
                if existing:
                    # Update existing record
                    existing.revenue = financials.get('revenue')
                    existing.net_income = financials.get('net_income')
                    existing.eps = financials.get('eps')
                    existing.pe_ratio = financials.get('pe_ratio')
                    existing.roe = financials.get('roe')
                    existing.debt_to_equity = financials.get('debt_to_equity')
                    existing.period_end = date.today()
                    print(f"  ✅ Updated financial data")
                else:
                    # Create new record
                    fs = FinancialStatement(
                        company_id=company.id,
                        period_end=date.today(),
                        period_type='annual',
                        revenue=financials.get('revenue'),
                        net_income=financials.get('net_income'),
                        eps=financials.get('eps'),
                        pe_ratio=financials.get('pe_ratio'),
                        roe=financials.get('roe'),
                        debt_to_equity=financials.get('debt_to_equity'),
                        source='screener'
                    )
                    db.add(fs)
                    print(f"  ✅ Created financial data")
                
                db.commit()
                success_count += 1
                
                # Display key metrics
                print(f"     Revenue: ₹{financials.get('revenue', 0):.2f} Cr")
                print(f"     PE Ratio: {financials.get('pe_ratio', 0):.2f}")
                print(f"     ROE: {financials.get('roe', 0):.2f}%")
                print(f"     Debt/Equity: {financials.get('debt_to_equity', 0):.2f}")
                
            except Exception as e:
                print(f"  ❌ Error: {str(e)}")
                error_count += 1
                db.rollback()
            
            # Rate limiting - be respectful to screener.in
            time.sleep(2)
        
        print("\n" + "=" * 60)
        print(f"✅ Successfully processed: {success_count}")
        print(f"⏭️  Skipped (already exists): {skip_count}")
        print(f"❌ Errors: {error_count}")
        print(f"📊 Total: {total}")
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Populate financial data from screener.in')
    parser.add_argument('--limit', type=int, help='Limit number of companies to process')
    parser.add_argument('--force', action='store_true', help='Re-scrape even if data exists')
    
    args = parser.parse_args()
    
    print("🚀 Starting financial data population from screener.in")
    print("⚠️  This will take a while due to rate limiting (2s per company)")
    print()
    
    populate_all_financials(
        limit=args.limit,
        skip_existing=not args.force
    )
