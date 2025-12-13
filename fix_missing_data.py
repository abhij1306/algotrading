"""
Simple fix for INFOSYS and DATAPATTNS
"""
import sys
sys.path.insert(0, '.')

from backend.app.database import SessionLocal, Company, FinancialStatement
from backend.app.screener_scraper import scrape_screener, extract_financials

db = SessionLocal()

# INFOSYS (use INFY on screener)
print("Fixing INFOSYS...")
company = db.query(Company).filter(Company.symbol == 'INFOSYS').first()
if company:
    try:
        scraped = scrape_screener('INFY')
        financials = extract_financials(scraped)
        
        if financials and financials['revenue'] > 0:
            company.market_cap = financials['market_cap']
            
            fs = db.query(FinancialStatement).filter(
                FinancialStatement.company_id == company.id
            ).first()
            
            if fs:
                fs.eps = financials['eps']
                fs.pe_ratio = financials['pe_ratio']
                db.commit()
                print(f"✅ INFOSYS: MCap={financials['market_cap']}, EPS={financials['eps']}, P/E={financials['pe_ratio']}")
    except Exception as e:
        print(f"❌ INFOSYS error: {e}")
        db.rollback()

# DATAPATTNS
print("\nFixing DATAPATTNS...")
company = db.query(Company).filter(Company.symbol == 'DATAPATTNS').first()
if company:
    try:
        scraped = scrape_screener('DATAPATTNS')
        financials = extract_financials(scraped)
        
        if financials and financials['revenue'] > 0:
            company.market_cap = financials['market_cap']
            
            fs = db.query(FinancialStatement).filter(
                FinancialStatement.company_id == company.id
            ).first()
            
            if fs:
                fs.revenue = financials['revenue']
                fs.net_income = financials['net_income']
                fs.eps = financials['eps']
                fs.roe = financials['roe']
                fs.pe_ratio = financials['pe_ratio']
                db.commit()
                print(f"✅ DATAPATTNS: MCap={financials['market_cap']}, Rev={financials['revenue']}, EPS={financials['eps']}")
    except Exception as e:
        print(f"❌ DATAPATTNS error: {e}")
        db.rollback()

db.close()
print("\n✅ Done!")
