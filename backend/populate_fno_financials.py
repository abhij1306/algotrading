"""
Populate financial data for all F&O companies
Targets the 299 NSE F&O stocks specifically
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, Company, FinancialStatement
from app.screener_scraper import scrape_screener, extract_financials
from datetime import date
import time

def populate_fno_financials(skip_existing=True):
    """
    Populate financial data for all F&O companies
    """
    db = SessionLocal()
    
    try:
        # Get all F&O companies
        companies = db.query(Company).filter(
            Company.is_fno == True,
            Company.is_active == True
        ).all()
        
        total = len(companies)
        
        print(f"🎯 Targeting F&O Companies Financial Data")
        print(f"📊 Found {total} F&O companies to process")
        print("=" * 70)
        
        success_count = 0
        skip_count = 0
        error_count = 0
        
        for idx, company in enumerate(companies, 1):
            print(f"\n[{idx}/{total}] {company.symbol} ({company.name or 'N/A'})...")
            
            # Check if data exists
            if skip_existing:
                existing = db.query(FinancialStatement).filter(
                    FinancialStatement.company_id == company.id,
                    FinancialStatement.source == 'screener'
                ).first()
                
                if existing:
                    print(f"  ⏭️  Skipping - data exists")
                    skip_count += 1
                    continue
            
            try:
                # Scrape data
                print(f"  🔍 Scraping screener.in...")
                scraped_data = scrape_screener(company.symbol)
                
                # Extract financials
                financials = extract_financials(scraped_data)
                
                if not financials:
                    print(f"  ❌ No financial data found")
                    error_count += 1
                    time.sleep(2)
                    continue
                
                # Update company market cap
                if financials.get('market_cap', 0) > 0:
                    company.market_cap = financials['market_cap']
                
                # Create financial statement with comprehensive data
                fs = FinancialStatement(
                    company_id=company.id,
                    period_end=date.today(),
                    period_type='annual',
                    source='screener',
                    # Profit & Loss
                    revenue=financials.get('revenue'),
                    operating_income=financials.get('operating_income'),
                    net_income=financials.get('net_income'),
                    ebitda=financials.get('ebitda'),
                    eps=financials.get('eps'),
                    # Balance Sheet
                    total_assets=financials.get('total_assets'),
                    total_liabilities=financials.get('total_liabilities'),
                    shareholders_equity=financials.get('shareholders_equity'),
                    total_debt=financials.get('total_debt'),
                    cash_and_equivalents=financials.get('cash_and_equivalents'),
                    # Cash Flows
                    operating_cash_flow=financials.get('operating_cash_flow'),
                    investing_cash_flow=financials.get('investing_cash_flow'),
                    financing_cash_flow=financials.get('financing_cash_flow'),
                    free_cash_flow=financials.get('free_cash_flow'),
                    # Ratios
                    pe_ratio=financials.get('pe_ratio'),
                    pb_ratio=financials.get('pb_ratio'),
                    roe=financials.get('roe'),
                    roa=financials.get('roa'),
                    debt_to_equity=financials.get('debt_to_equity')
                )
                
                db.add(fs)
                db.commit()
                
                print(f"  ✅ Saved financial data")
                print(f"     Revenue: ₹{financials.get('revenue', 0):.2f} Cr")
                print(f"     PE: {financials.get('pe_ratio', 0):.2f} | ROE: {financials.get('roe', 0):.2f}%")
                
                success_count += 1
                
            except Exception as e:
                print(f"  ❌ Error: {str(e)}")
                error_count += 1
                db.rollback()
            
            # Rate limiting - 2 seconds between requests
            time.sleep(2)
        
        print("\n" + "=" * 70)
        print(f"✅ Successfully processed: {success_count}")
        print(f"⏭️  Skipped (already exists): {skip_count}")
        print(f"❌ Errors: {error_count}")
        print(f"📊 Total F&O companies: {total}")
        print(f"💾 Total financial records in DB: {success_count + skip_count}")
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Populate F&O companies financial data')
    parser.add_argument('--force', action='store_true', help='Re-scrape even if data exists')
    
    args = parser.parse_args()
    
    print("🚀 Starting F&O Financial Data Population")
    print("⚠️  This will take ~10 minutes for 299 companies (2s rate limiting)")
    print()
    
    populate_fno_financials(skip_existing=not args.force)
