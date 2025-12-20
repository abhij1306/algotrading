import requests
import json

url = "http://localhost:8000/api/strategies/backtest"
payload = {
    "strategy_name": "ORB",
    "symbol": "TATASTEEL",
    "start_date": "2024-01-01",
    "end_date": "2024-12-20",
    "timeframe": "1D",
    "initial_capital": 100000,
    "params": {
        "opening_range_minutes": 15,
        "stop_loss_pct": 1.0,
        "take_profit_pct": 2.0,
        "max_positions_per_day": 3,
        "trade_type": "INTRADAY"
    }
}

print("Testing backtest API...")
print(f"Request: {json.dumps(payload, indent=2)}")

try:
    response = requests.post(url, json=payload, timeout=30)
    print(f"\nStatus Code: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
except Exception as e:
    print(f"Error: {e}")
