"""
Test Dashboard Endpoints
"""
import requests
import json

def test_dashboard():
    """
    Run simple smoke tests against local market dashboard endpoints and print parsed response details.
    
    Sends GET requests to /api/market/market-overview and /api/market/top-gainers (limit=5, index=NIFTY50). For each endpoint prints the HTTP status; if the status is 200, parses JSON and prints key fields: market status and indices (name, price, source) for market-overview; is_live, count, and each top-gainer's symbol, price, change_pct, and source for top-gainers. Any exceptions raised during requests are caught and printed.
    """
    base_url = "http://localhost:8000/api/market"

    print("🔍 Testing /market-overview...")
    try:
        resp = requests.get(f"{base_url}/market-overview")
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"Market Status: {data.get('market_status', {}).get('market_status')}")
            print(f"Indices keys: {list(data.get('indices', {}).keys())}")
            for idx, val in data.get('indices', {}).items():
                print(f" - {idx}: {val.get('price')} (Source: {val.get('source')})")
    except Exception as e:
        print(f"❌ Failed: {e}")

    print("\n🔍 Testing /top-gainers...")
    try:
        resp = requests.get(f"{base_url}/top-gainers?limit=5&index=NIFTY50")
        print(f"Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"Is Live: {data.get('is_live')}")
            print(f"Count: {data.get('count')}")
            for g in data.get('data', []):
                print(f" - {g['symbol']}: {g['price']} ({g['change_pct']:.2f}%) [Source: {g['source']}]")
    except Exception as e:
        print(f"❌ Failed: {e}")

if __name__ == "__main__":
    test_dashboard()