"""
Scrape financial data for companies that already have records in database
"""
import sys
sys.path.insert(0, '.')

from backend.app.database import SessionLocal, Company, FinancialStatement
from backend.app.screener_scraper import scrape_screener, extract_financials
from datetime import date
import time

print("="*80)
print("SCRAPER TEST - DATABASE COMPANIES")
print("="*80)

db = SessionLocal()

# Get companies that already have financial data
companies = db.query(Company).join(FinancialStatement).distinct().limit(10).all()

print(f"\n📊 Found {len(companies)} companies with existing financial data")
print(f"Companies: {', '.join([c.symbol for c in companies])}")

response = input("\nProceed with scraping? (y/n): ")
if response.lower() != 'y':
    print("Cancelled.")
    db.close()
    exit()

print("\n🔄 Starting scrape...")
print("-"*80)

success_count = 0
failed_count = 0

for i, company in enumerate(companies):
    symbol = company.symbol
    
    try:
        print(f"\n[{i+1}/{len(companies)}] Scraping {symbol}...")
        
        scraped = scrape_screener(symbol)
        financials = extract_financials(scraped)
        
        if not financials:
            failed_count += 1
            print(f"  ❌ No data extracted")
            time.sleep(2)
            continue
        
        # Display extracted data
        print(f"  Revenue: ₹{financials['revenue']} Cr")
        print(f"  Net Income: ₹{financials['net_income']} Cr")
        print(f"  EPS: ₹{financials['eps']}")
        print(f"  ROE: {financials['roe']}%")
        print(f"  Debt/Equity: {financials['debt_to_equity']}")
        print(f"  P/E: {financials['pe_ratio']}")
        
        # Update database
        fs = db.query(FinancialStatement).filter(
            FinancialStatement.company_id == company.id
        ).first()
        
        if fs:
            fs.revenue = financials['revenue']
            fs.net_income = financials['net_income']
            fs.eps = financials['eps']
            fs.roe = financials['roe']
            fs.debt_to_equity = financials['debt_to_equity']
            fs.pe_ratio = financials['pe_ratio']
            fs.source = 'screener'
            db.commit()
            print(f"  ✅ Updated in database")
        
        success_count += 1
        time.sleep(2)
        
    except Exception as e:
        failed_count += 1
        print(f"  ❌ Error: {str(e)[:100]}")
        time.sleep(2)

print(f"\n✅ Complete: {success_count} successful, {failed_count} failed")
db.close()
