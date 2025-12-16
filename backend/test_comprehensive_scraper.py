"""
Test comprehensive financial extraction on a single company
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.screener_scraper import scrape_screener
import json

def clean_number(val):
    if not val or val == '-':
        return None
    val = str(val).replace(',', '').replace('%', '').replace('Rs.', '').replace('Cr.', '').strip()
    try:
        return float(val)
    except:
        return None

# Test with a well-known company
symbol = "RELIANCE"
print(f"Testing scraper with {symbol}...")
print("=" * 60)

try:
    scraped_data = scrape_screener(symbol)
    
    print("\n📋 OVERVIEW DATA:")
    print(json.dumps(scraped_data['overview'], indent=2))
    
    print("\n📊 AVAILABLE TABLES:")
    for table_name in scraped_data['tables'].keys():
        print(f"  - {table_name}")
    
    # Show Profit & Loss table
    if 'Profit & Loss' in scraped_data['tables']:
        print("\n💰 PROFIT & LOSS TABLE:")
        print(scraped_data['tables']['Profit & Loss'].to_string())
    
    # Show Balance Sheet table
    if 'Balance Sheet' in scraped_data['tables']:
        print("\n🏦 BALANCE SHEET TABLE:")
        print(scraped_data['tables']['Balance Sheet'].to_string())
    
    # Show Cash Flow table
    if 'Cash Flow' in scraped_data['tables']:
        print("\n💵 CASH FLOW TABLE:")
        print(scraped_data['tables']['Cash Flow'].to_string())
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
