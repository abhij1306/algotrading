
import requests
import json
import os
import sys

# Ensure backend directory is in path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

def fetch_nse_symbols():
    url = "https://public.fyers.in/sym_details/NSE_CM.csv"
    output_file = os.path.join(os.path.dirname(__file__), '../data/nse_companies.json')
    
    # Create directory if it doesn't exist
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    
    print(f"Downloading from {url}...")
    try:
        response = requests.get(url)
        response.raise_for_status()
        
        lines = response.text.split('\n')
        symbols = []
        
        print(f"Processing {len(lines)} lines...")
        
        for line in lines:
            parts = line.split(',')
            # Check if line has enough parts and is an Equity (EQ)
            # Based on inspection:
            # 1: Symbol Description (RELIANCE INDUSTRIES LTD)
            # 3: Instrument Type? (1 for EQ?)
            # 9: Symbol (NSE:RELIANCE-EQ)
            # 13: Short Symbol (RELIANCE)
            
            # Simple heuristic: Look for NSE:XXXX-EQ in column 9
            if len(parts) > 13:
                full_symbol = parts[9]
                if full_symbol.startswith("NSE:") and full_symbol.endswith("-EQ"):
                    symbol = parts[13]
                    # Filter out purely numeric symbols or weird ones if any
                    if symbol.replace('&','').isalnum():
                        symbols.append(symbol)
        
        # Remove duplicates and sort
        symbols = sorted(list(set(symbols)))
        
        print(f"Found {len(symbols)} equity symbols.")
        
        with open(output_file, 'w') as f:
            json.dump(symbols, f, indent=2)
            
        print(f"Saved to {output_file}")
        return symbols
        
    except Exception as e:
        print(f"Error fetching symbols: {e}")
        return []

if __name__ == "__main__":
    fetch_nse_symbols()
