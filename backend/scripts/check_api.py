import requests

def verify_sectors():
    try:
        print("Checking /api/market/sectors...")
        r = requests.get("http://localhost:8000/api/market/sectors")
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            sectors = data.get('sectors', [])
            print(f"Found {len(sectors)} sectors. Sample: {sectors[:5]}")
        else:
            print(f"Error: {r.text}")
            
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    verify_sectors()
