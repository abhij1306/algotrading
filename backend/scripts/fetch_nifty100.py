
import requests
import pandas as pd
import json
import os
from io import StringIO

def fetch_indices():
    indices = {
        "NIFTY50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
        "NIFTY100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
    }
    
    data_dir = os.path.join(os.path.dirname(__file__), '../data')
    os.makedirs(data_dir, exist_ok=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    results = {}

    for name, url in indices.items():
        print(f"Downloading {name} list from {url}...")
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            df = pd.read_csv(StringIO(response.text))
            symbols = sorted(list(set(df['Symbol'].tolist())))
            
            output_file = os.path.join(data_dir, f"{name.lower()}_symbols.json")
            with open(output_file, 'w') as f:
                json.dump(symbols, f, indent=2)
            
            print(f"Saved {len(symbols)} symbols to {output_file}")
            results[name] = symbols
            
        except Exception as e:
            print(f"Error fetching {name}: {e}")
    
    return results

if __name__ == "__main__":
    fetch_indices()
