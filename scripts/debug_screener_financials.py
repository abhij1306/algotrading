import requests
import json
import sys

def test_screener_financials():
    url = "http://localhost:8000/api/screener?view=financial&limit=1000"
    try:
        print(f"Fetching from {url}...")
        res = requests.get(url)
        res.raise_for_status()
        data = res.json()
        
        items = data.get('data', [])
        total = data.get('meta', {}).get('total', 'unknown')
        
        if not items:
            print("No data returned from screener.")
            return
            
        print(f"Got {len(items)} items (Total meta: {total}).")
        first = items[0]
        print("First item keys:", first.keys())
        
        required_fields = ['revenue', 'eps', 'pe_ratio', 'roe']
        missing = [f for f in required_fields if f not in first]
        
        if missing:
            print(f"❌ FAILED: Missing financial fields: {missing}")
            print("First item:", json.dumps(first, indent=2))
            sys.exit(1)
        else:
            print("✅ SUCCESS: Financial fields found.")
            print("Sample Data:", json.dumps({k: first[k] for k in required_fields}, indent=2))
            
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_screener_financials()
