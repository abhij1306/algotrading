"""
Enhanced financial data extraction from screener.in
Extracts comprehensive data from P&L, Balance Sheet, and Cash Flow tables
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, Company, FinancialStatement
from app.screener_scraper import scrape_screener
from datetime import date
import time

def clean_number(val):
    """Clean and convert string to float"""
    if not val or val == '-':
        return None
    # Remove commas, %, Rs., Cr., etc.
    val = str(val).replace(',', '').replace('%', '').replace('Rs.', '').replace('Cr.', '').strip()
    try:
        return float(val)
    except:
        return None

def extract_comprehensive_financials(scraped_data):
    """
    Extract comprehensive financial data from all available tables
    """
    overview = scraped_data.get("overview", {})
    tables = scraped_data.get("tables", {})
    
    financials = {}
    
    # === PROFIT & LOSS TABLE ===
    if 'Profit & Loss' in tables:
        pl_df = tables['Profit & Loss']
        if not pl_df.empty and len(pl_df.columns) > 1:
            latest_col = pl_df.columns[-1]
            
            # Revenue
            sales_row = pl_df[pl_df.iloc[:, 0].astype(str).str.contains('Sales|Revenue', case=False, na=False)]
            if not sales_row.empty:
                financials['revenue'] = clean_number(sales_row.iloc[0][latest_col])
            
            # Operating Income / EBITDA
            ebitda_row = pl_df[pl_df.iloc[:, 0].astype(str).str.contains('Operating Profit|EBITDA', case=False, na=False)]
            if not ebitda_row.empty:
                financials['ebitda'] = clean_number(ebitda_row.iloc[0][latest_col])
            
            # Net Profit
            profit_row = pl_df[pl_df.iloc[:, 0].astype(str).str.contains('Net Profit', case=False, na=False)]
            if not profit_row.empty:
                financials['net_income'] = clean_number(profit_row.iloc[0][latest_col])
            
            # EPS
            eps_row = pl_df[pl_df.iloc[:, 0].astype(str).str.contains('EPS in Rs', case=False, na=False)]
            if not eps_row.empty:
                financials['eps'] = clean_number(eps_row.iloc[0][latest_col])
    
    # === BALANCE SHEET TABLE ===
    if 'Balance Sheet' in tables:
        bs_df = tables['Balance Sheet']
        if not bs_df.empty and len(bs_df.columns) > 1:
            latest_col = bs_df.columns[-1]
            
            # Total Assets
            assets_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Total Assets', case=False, na=False)]
            if not assets_row.empty:
                financials['total_assets'] = clean_number(assets_row.iloc[0][latest_col])
            
            # Total Liabilities
            liab_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Total Liabilities', case=False, na=False)]
            if not liab_row.empty:
                financials['total_liabilities'] = clean_number(liab_row.iloc[0][latest_col])
            
            # Shareholders Equity
            equity_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Shareholders.*Equity|Equity Capital', case=False, na=False)]
            if not equity_row.empty:
                financials['shareholders_equity'] = clean_number(equity_row.iloc[0][latest_col])
            
            # Total Debt
            debt_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Borrowings|Total Debt', case=False, na=False)]
            if not debt_row.empty:
                financials['total_debt'] = clean_number(debt_row.iloc[0][latest_col])
            
            # Cash and Equivalents
            cash_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Cash|Equivalents', case=False, na=False)]
            if not cash_row.empty:
                financials['cash_and_equivalents'] = clean_number(cash_row.iloc[0][latest_col])
    
    # === CASH FLOW TABLE ===
    if 'Cash Flow' in tables:
        cf_df = tables['Cash Flow']
        if not cf_df.empty and len(cf_df.columns) > 1:
            latest_col = cf_df.columns[-1]
            
            # Operating Cash Flow
            ocf_row = cf_df[cf_df.iloc[:, 0].astype(str).str.contains('Cash from Operating', case=False, na=False)]
            if not ocf_row.empty:
                financials['operating_cash_flow'] = clean_number(ocf_row.iloc[0][latest_col])
            
            # Investing Cash Flow
            icf_row = cf_df[cf_df.iloc[:, 0].astype(str).str.contains('Cash from Investing', case=False, na=False)]
            if not icf_row.empty:
                financials['investing_cash_flow'] = clean_number(icf_row.iloc[0][latest_col])
            
            # Financing Cash Flow
            fcf_row = cf_df[cf_df.iloc[:, 0].astype(str).str.contains('Cash from Financing', case=False, na=False)]
            if not fcf_row.empty:
                financials['financing_cash_flow'] = clean_number(fcf_row.iloc[0][latest_col])
            
            # Free Cash Flow (if available)
            free_cf_row = cf_df[cf_df.iloc[:, 0].astype(str).str.contains('Free Cash Flow', case=False, na=False)]
            if not free_cf_row.empty:
                financials['free_cash_flow'] = clean_number(free_cf_row.iloc[0][latest_col])
    
    # === RATIOS FROM OVERVIEW ===
    financials['pe_ratio'] = clean_number(overview.get('Stock P/E', 0))
    financials['pb_ratio'] = clean_number(overview.get('Price to Book', 0))
    financials['roe'] = clean_number(overview.get('ROE', 0))
    financials['roa'] = clean_number(overview.get('ROA', 0))
    financials['debt_to_equity'] = clean_number(overview.get('Debt to equity', 0))
    financials['market_cap'] = clean_number(overview.get('Market Cap', 0))
    
    # Calculate Free Cash Flow if not available
    if not financials.get('free_cash_flow') and financials.get('operating_cash_flow') and financials.get('investing_cash_flow'):
        financials['free_cash_flow'] = financials['operating_cash_flow'] + financials['investing_cash_flow']
    
    return financials

def populate_comprehensive_financials(limit=None, skip_existing=True):
    """
    Populate comprehensive financial data for all companies
    """
    db = SessionLocal()
    
    try:
        query = db.query(Company).filter(Company.is_active == True)
        if limit:
            query = query.limit(limit)
        
        companies = query.all()
        total = len(companies)
        
        print(f"🚀 Starting comprehensive financial data extraction")
        print(f"📊 Processing {total} companies")
        print("=" * 70)
        
        success_count = 0
        skip_count = 0
        error_count = 0
        
        for idx, company in enumerate(companies, 1):
            print(f"\n[{idx}/{total}] {company.symbol} ({company.name})...")
            
            # Check if data exists
            if skip_existing:
                existing = db.query(FinancialStatement).filter(
                    FinancialStatement.company_id == company.id,
                    FinancialStatement.source == 'screener_comprehensive'
                ).first()
                
                if existing:
                    print(f"  ⏭️  Skipping - comprehensive data exists")
                    skip_count += 1
                    continue
            
            try:
                # Scrape data
                print(f"  🔍 Scraping screener.in...")
                scraped_data = scrape_screener(company.symbol)
                
                # Extract comprehensive financials
                financials = extract_comprehensive_financials(scraped_data)
                
                if not financials or not financials.get('revenue'):
                    print(f"  ❌ No financial data found")
                    error_count += 1
                    time.sleep(2)
                    continue
                
                # Update company market cap
                if financials.get('market_cap'):
                    company.market_cap = financials['market_cap']
                
                # Create or update financial statement
                fs = FinancialStatement(
                    company_id=company.id,
                    period_end=date.today(),
                    period_type='annual',
                    source='screener_comprehensive',
                    # P&L
                    revenue=financials.get('revenue'),
                    operating_income=financials.get('ebitda'),
                    net_income=financials.get('net_income'),
                    ebitda=financials.get('ebitda'),
                    eps=financials.get('eps'),
                    # Balance Sheet
                    total_assets=financials.get('total_assets'),
                    total_liabilities=financials.get('total_liabilities'),
                    shareholders_equity=financials.get('shareholders_equity'),
                    total_debt=financials.get('total_debt'),
                    cash_and_equivalents=financials.get('cash_and_equivalents'),
                    # Cash Flow
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
                
                print(f"  ✅ Saved comprehensive financial data")
                print(f"     Revenue: ₹{financials.get('revenue', 0):.2f} Cr")
                print(f"     Total Assets: ₹{financials.get('total_assets', 0):.2f} Cr")
                print(f"     Operating CF: ₹{financials.get('operating_cash_flow', 0):.2f} Cr")
                print(f"     PE: {financials.get('pe_ratio', 0):.2f} | ROE: {financials.get('roe', 0):.2f}%")
                
                success_count += 1
                
            except Exception as e:
                print(f"  ❌ Error: {str(e)}")
                error_count += 1
                db.rollback()
            
            # Rate limiting
            time.sleep(2)
        
        print("\n" + "=" * 70)
        print(f"✅ Successfully processed: {success_count}")
        print(f"⏭️  Skipped (already exists): {skip_count}")
        print(f"❌ Errors: {error_count}")
        print(f"📊 Total: {total}")
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Populate comprehensive financial data')
    parser.add_argument('--limit', type=int, help='Limit number of companies')
    parser.add_argument('--force', action='store_true', help='Re-scrape even if data exists')
    
    args = parser.parse_args()
    
    populate_comprehensive_financials(
        limit=args.limit,
        skip_existing=not args.force
    )
