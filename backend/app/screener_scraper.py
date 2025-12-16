"""
Screener.in scraper for financial data
"""
import requests
import pandas as pd
from bs4 import BeautifulSoup
import time
from typing import Dict, Optional

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

BASE_URL = "https://www.screener.in/company/{}/consolidated/"

def fetch_page(symbol: str):
    url = BASE_URL.format(symbol.upper())
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")

def extract_company_header(soup):
    data = {}

    # Company Name
    name = soup.find("h1")
    data["company_name"] = name.text.strip() if name else None

    # Top ratios
    for li in soup.select("ul#top-ratios li"):
        label = li.find("span", class_="name")
        value = li.find("span", class_="number")
        if label and value:
            data[label.text.strip()] = value.text.strip()

    return data

def extract_key_value_table(section):
    """
    Converts label-value HTML blocks into dict
    """
    data = {}
    rows = section.select("li")
    for row in rows:
        label = row.find("span", class_="name")
        value = row.find("span", class_="number")
        if label and value:
            data[label.text.strip()] = value.text.strip()
    return data

def extract_html_tables(soup):
    tables = {}
    for section in soup.select("section"):
        header = section.find("h2")
        table = section.find("table")

        if header and table:
            try:
                df = pd.read_html(str(table))[0]
                tables[header.text.strip()] = df
            except Exception:
                continue
    return tables

def extract_pros_cons(soup):
    data = {"pros": [], "cons": []}

    pros = soup.select("div.pros ul li")
    cons = soup.select("div.cons ul li")

    data["pros"] = [p.text.strip() for p in pros]
    data["cons"] = [c.text.strip() for c in cons]

    return data

def scrape_screener(symbol):
    soup = fetch_page(symbol)

    output = {
        "overview": extract_company_header(soup),
        "tables": extract_html_tables(soup),
        "pros_cons": extract_pros_cons(soup)
    }

    return output

def extract_financials(scraped_data: Dict) -> Optional[Dict]:
    """
    Extract comprehensive financial metrics from scraped data for database insertion
    Extracts from: Profit & Loss, Balance Sheet, Cash Flows, Ratios tables
    """
    try:
        overview = scraped_data.get("overview", {})
        tables = scraped_data.get("tables", {})
        
        # Helper to clean numeric values
        def clean_number(val):
            if not val or val == '-':
                return None
            # Remove commas, %, Rs., Cr., etc.
            val = str(val).replace(',', '').replace('%', '').replace('Rs.', '').replace('Cr.', '').strip()
            try:
                return float(val)
            except:
                return None
        
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
                    financials['operating_income'] = financials['ebitda']
                
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
                
                # Total Debt / Borrowings
                debt_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Borrowings|Total Debt', case=False, na=False)]
                if not debt_row.empty:
                    financials['total_debt'] = clean_number(debt_row.iloc[0][latest_col])
                
                # Cash and Equivalents
                cash_row = bs_df[bs_df.iloc[:, 0].astype(str).str.contains('Cash|Equivalents', case=False, na=False)]
                if not cash_row.empty:
                    financials['cash_and_equivalents'] = clean_number(cash_row.iloc[0][latest_col])
        
        # === CASH FLOWS TABLE ===
        if 'Cash Flows' in tables:
            cf_df = tables['Cash Flows']
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
        financials['pe_ratio'] = clean_number(overview.get('Stock P/E'))
        financials['pb_ratio'] = clean_number(overview.get('Price to Book'))
        financials['roe'] = clean_number(overview.get('ROE'))
        financials['roa'] = clean_number(overview.get('ROA'))
        financials['debt_to_equity'] = clean_number(overview.get('Debt to equity'))
        financials['market_cap'] = clean_number(overview.get('Market Cap'))
        
        # Calculate Free Cash Flow if not available
        if not financials.get('free_cash_flow'):
            ocf = financials.get('operating_cash_flow')
            icf = financials.get('investing_cash_flow')
            if ocf and icf:
                financials['free_cash_flow'] = ocf + icf
        
        # Fallback to overview for basic metrics if not found in tables
        if not financials.get('revenue'):
            financials['revenue'] = clean_number(overview.get('Sales'))
        if not financials.get('net_income'):
            financials['net_income'] = clean_number(overview.get('Net Profit'))
        if not financials.get('eps'):
            financials['eps'] = clean_number(overview.get('EPS in Rs'))
        
        # Add source
        financials['source'] = 'screener'
        
        return financials
        
    except Exception as e:
        print(f"Error extracting financials: {e}")
        import traceback
        traceback.print_exc()
        return None


def extract_sector_industry(soup) -> Optional[Dict]:
    """
    Extract sector and industry information from screener.in page
    Returns dict with 'sector' and 'industry' keys
    """
    try:
        # Screener.in typically shows sector info in company details section
        # Look for elements containing sector/industry information
        
        # Method 1: Check for company details section
        company_details = soup.find('div', id='company-details') or soup.find('section', id='company-info')
        if company_details:
            # Look for lines containing "Sector:" or "Industry:"
            text_content = company_details.get_text()
            sector = None
            industry = None
            
            for line in text_content.split('\\n'):
                line = line.strip()
                if 'Sector:' in line or 'sector:' in line.lower():
                    sector = line.split(':', 1)[1].strip() if ':' in line else None
                if 'Industry:' in line or 'industry:' in line.lower():
                    industry = line.split(':', 1)[1].strip() if ':' in line else None
            
            if sector or industry:
                return {
                    'sector': sector,
                    'industry': industry
                }
        
        # Method 2: Check meta tags or structured data
        # Screener might include sector in meta or schema.org data
        schema_org = soup.find('script', type='application/ld+json')
        if schema_org:
            try:
                import json
                schema_data = json.loads(schema_org.string)
                if isinstance(schema_data, dict):
                    sector = schema_data.get('industry') or schema_data.get('sector')
                    if sector:
                        return {
                            'sector': sector,
                            'industry': sector  # Use same for both if only one available
                        }
            except:
                pass
        
        # Method 3: Look for breadcrumb or category links
        # Example: Home > Stocks > IT Software > Company Name
        breadcrumbs = soup.select('nav.breadcrumb a, .breadcrumb a')
        if len(breadcrumbs) >= 2:
            # Usually the second-to-last breadcrumb is the sector/industry
            sector_text = breadcrumbs[-2].get_text(strip=True) if len(breadcrumbs) > 1 else None
            if sector_text and sector_text.lower() not in ['home', 'stocks', 'companies']:
                return {
                    'sector': sector_text,
                    'industry': sector_text
                }
        
        return None
        
    except Exception as e:
        print(f"Error extracting sector/industry: {e}")
        return None
