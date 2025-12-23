import sys
import os
from datetime import datetime

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, StrategyContract, engine

def seed_strategies():
    db = SessionLocal()
    
    
    # Cleanup unwanted strategies
    unwanted_ids = ["TF-GOLD-MCX-01", "TF-GLD-042", "MM-BTC-003", "OP-SPX-005", "FX-EM-088", "MR-VIX-001", "SA-QQQ-019"]
    for uid in unwanted_ids:
        db.query(StrategyContract).filter(StrategyContract.strategy_id == uid).delete()
    
    strategies = [
        {
            "strategy_id": "MR-NIFTY-VIX-01",
            "allowed_universes": ["UNIV-NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "VOLATILITY",
            "description": "Nifty VIX Mean Reversion. Captures overextensions in India VIX relative to Nifty 50.",
            "when_loses": "Sustained unidirectional trends or volatility compression periods.",
            "parameters": {"lookback": 20, "z_score": 2.0},
            "lifecycle_state": "LIVE",
            "approved_by": "System"
        },
        {
            "strategy_id": "SA-NIFTYIT-01",
            "allowed_universes": ["UNIV-NIFTY-IT"],
            "timeframe": "15MIN",
            "holding_period": "INTRADAY",
            "regime": "RANGE",
            "description": "IT Sector StatArb. Pairs trading within Nifty IT constituents.",
            "when_loses": "Sector-wide shock (e.g., US Tech crash) affecting all correlations.",
            "parameters": {"pair_window": 60, "entry_std": 2.5},
            "lifecycle_state": "LIVE",
            "approved_by": "System"
        },
        {
            "strategy_id": "MOM-BANKNIFTY-01",
            "allowed_universes": ["UNIV-BANKNIFTY"],
            "timeframe": "5MIN",
            "holding_period": "INTRADAY",
            "regime": "TREND",
            "description": "Bank Nifty Momentum Breakout. Captures opening range breakouts.",
            "when_loses": "False breakouts in range-bound markets.",
            "parameters": {"orb_period_min": 30},
            "lifecycle_state": "LIVE",
            "approved_by": "System"
        },
        {
            "strategy_id": "OP-NIFTY-GAMMA-01",
            "allowed_universes": ["UNIV-NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "VOLATILITY",
            "description": "Nifty Short Gamma. Systematic selling of OTM puts/calls to harvest premium.",
            "when_loses": "Black Swan events or rapid IV expansion.",
            "parameters": {"delta": 0.15, "dte": 7},
            "lifecycle_state": "LIVE",
            "approved_by": "System"
        }
    ]



    print("Seeding Strategy Contracts...")
    for s_data in strategies:
        existing = db.query(StrategyContract).filter(StrategyContract.strategy_id == s_data["strategy_id"]).first()
        if not existing:
            strat = StrategyContract(
                strategy_id=s_data["strategy_id"],
                allowed_universes=s_data["allowed_universes"],
                timeframe=s_data["timeframe"],
                holding_period=s_data["holding_period"],
                regime=s_data["regime"],
                description=s_data["description"],
                when_loses=s_data["when_loses"],
                parameters=s_data["parameters"],
                lifecycle_state=s_data["lifecycle_state"],
                approved_by=s_data["approved_by"],
                approved_at=datetime.utcnow()
            )
            db.add(strat)
            print(f"Added {s_data['strategy_id']}")
        else:
            print(f"Skipped {s_data['strategy_id']} (Exists)")
    
    db.commit()
    db.close()
    print("Seeding Complete.")

if __name__ == "__main__":
    seed_strategies()
