import requests
import json

url = "http://localhost:8000/api/backtest/run"
payload = {
    "strategy_name": "ORB",
    "symbol": "NIFTY50",  # Use a symbol likely to have data
    "start_date": "2024-01-01",
    "end_date": "2024-01-10",
    "timeframe": "5min",
    "initial_capital": 100000,
    "params": {
        "stopLoss": 1.5,
        "takeProfit": 3.0,
        "riskPerTrade": 1.0
    }
}

try:
    print(f"Sending request to {url}...")
    response = requests.post(url, json=payload)
    print(f"Status Code: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print("Success!")
        metrics = data.get('metrics', {})
        print(f"Metrics: {json.dumps(metrics, indent=2)}")
        trades = data.get('trades', [])
        print(f"Trades Count: {len(trades)}")
        if trades:
            print(f"First Trade: {trades[0]}")
    else:
        print("Error Response:")
        print(response.text)

except Exception as e:
    print(f"Exception: {e}")
