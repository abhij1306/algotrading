
import requests
import json
import sys

BASE_URL = "http://localhost:8000/api"

def verify():
    print("--- 1. Creating Portfolio Policy ---")
    policy_payload = {
        "name": "Verification Policy",
        "cash_reserve_percent": 20.0,
        "daily_stop_loss_percent": 2.0,
        "max_equity_exposure_percent": 80.0,
        "max_strategy_allocation_percent": 25.0
    }
    res = requests.post(f"{BASE_URL}/quant/portfolio-policy", json=policy_payload)
    if res.status_code not in [200, 201]:
        print(f"Policy Creation Failed: {res.text}")
        return
    policy = res.json()
    policy_id = policy['id']
    print(f"Policy Created: {policy_id}")
    
    print("\n--- 2. Running Simulation (Backtest) ---")
    sim_payload = {
        "policy_id": policy_id,
        "strategy_ids": ["TREND_FOLLOWING_V1"],
        "start_date": "2024-01-01",
        "end_date": "2024-01-31"
    }
    try:
        res = requests.post(f"{BASE_URL}/quant/run-simulation", json=sim_payload)
        if res.status_code == 200:
            data = res.json()
            metrics = data.get("metrics", {})
            print(f"Simulation Success!")
            print(f"Total Return: {metrics.get('total_return_pct', metrics.get('total_return'))}")
            print(f"Max Drawdown: {metrics.get('max_drawdown')}%")
            print(f"Trades Checked: {metrics.get('total_trades', 'N/A')}")
        else:
            print(f"Simulation Failed: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"Simulation Error: {e}")

    print("\n--- 3. Checking Live Monitor ---")
    try:
        res = requests.get(f"{BASE_URL}/quant/monitor/live")
        if res.status_code == 200:
            portfolios = res.json()
            print(f"Active Live Portfolios: {len(portfolios)}")
            for p in portfolios:
                print(f" - {p['portfolio_name']}: Equity={p['total_equity']}")
        else:
            print(f"Monitor Failed: {res.text}")
    except Exception as e:
        print(f"Monitor Error: {e}")

if __name__ == "__main__":
    verify()
