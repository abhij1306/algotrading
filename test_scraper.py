"""
Test screener scraper with HDFCBANK
"""
import sys
sys.path.insert(0, '.')

from backend.app.screener_scraper import scrape_screener, extract_financials

print("Testing Screener.in scraper with HDFCBANK...")
print("-" * 60)

try:
    result = scrape_screener('HDFCBANK')
    financials = extract_financials(result)
    
    if financials:
        print("\n✅ Scrape successful!")
        print(f"\nRevenue: ₹{financials['revenue']} Cr")
        print(f"Net Income: ₹{financials['net_income']} Cr")
        print(f"EPS: ₹{financials['eps']}")
        print(f"ROE: {financials['roe']}%")
        print(f"Debt/Equity: {financials['debt_to_equity']}")
        print(f"P/E Ratio: {financials['pe_ratio']}")
    else:
        print("\n❌ Failed to extract financials")
        
except Exception as e:
    print(f"\n❌ Error: {e}")
