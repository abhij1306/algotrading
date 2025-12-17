"""
Yahoo Finance Financial Data Fetcher
Fetches quarterly and annual financial statements for companies missing financial data
Stores in PostgreSQL financial_statements table
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, date
import time
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app.database import SessionLocal, Company, FinancialStatement
from sqlalchemy import func

# ============== CONFIGURATION ==============
SLEEP_SECONDS = 2.0  # Rate limiting (slower for financial data)
START_YEAR = 2024  # Fetch from 2024 onwards
START_MONTH = 4    # Start from April 2024 (Q1 FY2024-25)

# ============== HELPER FUNCTIONS ==============

def get_companies_missing_financials(db):
    """Get list of companies that don't have any financial data"""
    # Get all active companies
    all_companies = db.query(Company).filter(Company.is_active == True).all()
    
    # Get companies with financials
    companies_with_financials = db.query(
        func.distinct(FinancialStatement.company_id)
    ).all()
    companies_with_financials_ids = {c[0] for c in companies_with_financials}
    
    # Filter to get companies without financials
    missing = [c for c in all_companies if c.id not in companies_with_financials_ids]
    
    return missing


def nse_to_yahoo_symbol(nse_symbol: str) -> str:
    """Convert NSE symbol to Yahoo format"""
    return f"{nse_symbol}.NS"


def fetch_yahoo_financials(yahoo_symbol: str):
    """
    Fetch financial data from Yahoo Finance
    
    Returns:
        dict with 'quarterly' and 'annual' DataFrames for:
        - income_stmt
        - balance_sheet
        - cash_flow
    """
    try:
        ticker = yf.Ticker(yahoo_symbol)
        
        # Fetch financial statements
        financials = {
            'quarterly': {
                'income_stmt': ticker.quarterly_income_stmt,
                'balance_sheet': ticker.quarterly_balance_sheet,
                'cash_flow': ticker.quarterly_cashflow
            },
            'annual': {
                'income_stmt': ticker.income_stmt,
                'balance_sheet': ticker.balance_sheet,
                'cash_flow': ticker.cashflow
            }
        }
        
        return financials
        
    except Exception as e:
        print(f"    ❌ Error: {str(e)}")
        return None


def parse_financial_data(financials, company_id, symbol, db):
    """
    Parse Yahoo Finance financial data and save to database
    
    Args:
        financials: dict from fetch_yahoo_financials()
        company_id: Company ID in database
        symbol: NSE symbol
        db: Database session
    
    Returns:
        Number of records saved
    """
    if not financials:
        return 0
    
    records_saved = 0
    
    # Process quarterly and annual data
    for period_type in ['quarterly', 'annual']:
        income_stmt = financials[period_type]['income_stmt']
        balance_sheet = financials[period_type]['balance_sheet']
        cash_flow = financials[period_type]['cash_flow']
        
        # Skip if no data
        if income_stmt is None or income_stmt.empty:
            continue
        
        # Iterate through each period (columns in DataFrame)
        for period_date in income_stmt.columns:
            # Filter to April 2024 onwards only
            if period_date.year < START_YEAR or (period_date.year == START_YEAR and period_date.month < START_MONTH):
                continue
            
            try:
                # Extract financial metrics
                financial_data = {
                    'company_id': company_id,
                    'period_end': period_date.date(),
                    'period_type': period_type,
                    'fiscal_year': period_date.year,
                    'quarter': ((period_date.month - 1) // 3 + 1) if period_type == 'quarterly' else None,
                    'source': 'yahoo_finance'
                }
                
                # Income Statement
                if not income_stmt.empty:
                    financial_data['revenue'] = income_stmt.loc['Total Revenue', period_date] if 'Total Revenue' in income_stmt.index else None
                    financial_data['operating_income'] = income_stmt.loc['Operating Income', period_date] if 'Operating Income' in income_stmt.index else None
                    financial_data['net_income'] = income_stmt.loc['Net Income', period_date] if 'Net Income' in income_stmt.index else None
                    financial_data['ebitda'] = income_stmt.loc['EBITDA', period_date] if 'EBITDA' in income_stmt.index else None
                
                # Balance Sheet
                if not balance_sheet.empty:
                    financial_data['total_assets'] = balance_sheet.loc['Total Assets', period_date] if 'Total Assets' in balance_sheet.index else None
                    financial_data['total_liabilities'] = balance_sheet.loc['Total Liabilities Net Minority Interest', period_date] if 'Total Liabilities Net Minority Interest' in balance_sheet.index else None
                    financial_data['shareholders_equity'] = balance_sheet.loc['Stockholders Equity', period_date] if 'Stockholders Equity' in balance_sheet.index else None
                    financial_data['total_debt'] = balance_sheet.loc['Total Debt', period_date] if 'Total Debt' in balance_sheet.index else None
                    financial_data['cash_and_equivalents'] = balance_sheet.loc['Cash And Cash Equivalents', period_date] if 'Cash And Cash Equivalents' in balance_sheet.index else None
                
                # Cash Flow
                if not cash_flow.empty:
                    financial_data['operating_cash_flow'] = cash_flow.loc['Operating Cash Flow', period_date] if 'Operating Cash Flow' in cash_flow.index else None
                    financial_data['free_cash_flow'] = cash_flow.loc['Free Cash Flow', period_date] if 'Free Cash Flow' in cash_flow.index else None
                
                # Calculate ratios if data available
                if financial_data.get('total_assets') and financial_data.get('total_liabilities'):
                    equity = financial_data.get('shareholders_equity', 0)
                    if equity and equity != 0:
                        financial_data['debt_to_equity'] = financial_data['total_liabilities'] / equity
                
                if financial_data.get('net_income') and financial_data.get('shareholders_equity'):
                    if financial_data['shareholders_equity'] != 0:
                        financial_data['roe'] = financial_data['net_income'] / financial_data['shareholders_equity']
                
                if financial_data.get('net_income') and financial_data.get('total_assets'):
                    if financial_data['total_assets'] != 0:
                        financial_data['roa'] = financial_data['net_income'] / financial_data['total_assets']
                
                # Check if record already exists
                existing = db.query(FinancialStatement).filter(
                    FinancialStatement.company_id == company_id,
                    FinancialStatement.period_end == financial_data['period_end'],
                    FinancialStatement.period_type == period_type
                ).first()
                
                if existing:
                    # Update existing record
                    for key, value in financial_data.items():
                        if hasattr(existing, key):
                            setattr(existing, key, value)
                    existing.updated_at = datetime.utcnow()
                else:
                    # Create new record
                    statement = FinancialStatement(**financial_data)
                    db.add(statement)
                
                records_saved += 1
                
            except Exception as e:
                print(f"      ⚠️  Error parsing period {period_date}: {e}")
                continue
    
    # Commit all records for this company
    try:
        db.commit()
        return records_saved
    except Exception as e:
        db.rollback()
        print(f"    ❌ Database error: {e}")
        return 0


# ============== MAIN FETCHER ==============

def fetch_financials_for_missing_companies(limit: int = None):
    """
    Fetch financial data for companies missing financials
    
    Args:
        limit: Optional limit on number of companies to process (for testing)
    """
    print("\n" + "="*60)
    print("FETCHING FINANCIAL DATA FROM YAHOO FINANCE")
    print("="*60)
    
    db = SessionLocal()
    try:
        # Get companies missing financials
        missing_companies = get_companies_missing_financials(db)
        
        if limit:
            missing_companies = missing_companies[:limit]
        
        total = len(missing_companies)
        print(f"\n📊 Found {total} companies missing financial data")
        print(f"📅 Fetching data from {START_YEAR} onwards")
        print(f"⏱️  Rate limit: {SLEEP_SECONDS}s per symbol")
        
        if limit:
            print(f"🔬 TEST MODE: Processing only {limit} companies")
        
        estimated_time = total * SLEEP_SECONDS / 60
        print(f"⏰ Estimated time: ~{estimated_time:.0f} minutes\n")
        
        success_count = 0
        error_count = 0
        no_data_count = 0
        total_records = 0
        
        for i, company in enumerate(missing_companies, 1):
            symbol = company.symbol
            yahoo_symbol = nse_to_yahoo_symbol(symbol)
            
            print(f"[{i}/{total}] {symbol:15s} ({yahoo_symbol})", end=" ... ")
            
            # Fetch financials
            financials = fetch_yahoo_financials(yahoo_symbol)
            
            if financials:
                # Parse and save to database
                records = parse_financial_data(financials, company.id, symbol, db)
                
                if records > 0:
                    print(f"✓ {records} periods saved")
                    success_count += 1
                    total_records += records
                else:
                    print(f"⚠️  No data for {START_YEAR}+")
                    no_data_count += 1
            else:
                error_count += 1
            
            # Rate limiting
            if i < total:
                time.sleep(SLEEP_SECONDS)
        
        print(f"\n{'='*60}")
        print(f"FINANCIAL DATA FETCH COMPLETE")
        print(f"{'='*60}")
        print(f"✓ Success: {success_count} companies")
        print(f"⚠️  No data: {no_data_count} companies")
        print(f"❌ Errors: {error_count} companies")
        print(f"📊 Total records saved: {total_records}")
        
    finally:
        db.close()


# ============== MAIN ==============

def main():
    """Main execution function"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Fetch financial data from Yahoo Finance for companies missing financials')
    parser.add_argument('--limit', type=int, help='Limit number of companies to process (for testing)')
    parser.add_argument('--year', type=int, help=f'Start year (default: 2024)')
    
    args = parser.parse_args()
    
    # Update start year if provided
    global START_YEAR
    if args.year:
        START_YEAR = args.year
    
    start_time = datetime.now()
    
    fetch_financials_for_missing_companies(limit=args.limit)
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds() / 60
    
    print(f"\n{'='*60}")
    print(f"TOTAL TIME: {duration:.1f} minutes")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
