
import sys
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file, StrategyContract, StockUniverse, Base
from app.utils.env_loader import load_dotenv
import os
import json

def seed_config():
    env_path = get_env_file()
    load_dotenv(env_path)
    
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = os.getenv('DB_PORT', '5432')
    DB_NAME = os.getenv('DB_NAME', 'algotrading')
    DB_USER = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # 1. Seed Universe
        univ_id = "NIFTY50_CORE"
        univ = db.query(StockUniverse).filter_by(id=univ_id).first()
        
        # Simple list of reliable symbols that likely have valid NSE/DB data
        symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "HINDUNILVR", "L&T"]
        
        if not univ:
            univ = StockUniverse(
                id=univ_id,
                description="Core Nifty 50 Bluechip Stocks",
                symbols_by_date={"2023-01-01": symbols},
                rebalance_frequency="NONE",
                selection_rules="Top 10 Market Cap"
            )
            db.add(univ)
            print(f"Created Universe: {univ_id}")
        else:
            # Update symbols just in case
            univ.symbols_by_date = {"2023-01-01": symbols}
            print(f"Updated Universe: {univ_id}")
            
        # 2. Seed Contracts
        contracts = [
            {
                "id": "TREND_FOLLOWING_V1",
                "universes": ["NIFTY50_CORE"],
                "timeframe": "DAILY", # Use DAILY to ensure data availability (fallback)
                "holding": "MULTI_DAY",
                "regime": "TREND",
                "desc": "Exponential Moving Average Crossover System"
            },
            {
                "id": "MEAN_REVERSION_RSI",
                "universes": ["NIFTY50_CORE"],
                "timeframe": "DAILY",
                "holding": "MULTI_DAY",
                "regime": "RANGE",
                "desc": "RSI Oversold/Overbought Reversion"
            },
            {
                "id": "VOLATILITY_BREAKOUT",
                "universes": ["NIFTY50_CORE"],
                "timeframe": "DAILY",
                "holding": "MULTI_DAY",
                "regime": "VOLATILITY",
                "desc": "Bollinger Band Breakout"
            }
        ]
        
        for c in contracts:
            contract = db.query(StrategyContract).filter_by(strategy_id=c['id']).first()
            if not contract:
                contract = StrategyContract(
                    strategy_id=c['id'],
                    allowed_universes=c['universes'],
                    timeframe=c['timeframe'],
                    holding_period=c['holding'],
                    regime=c['regime'],
                    description=c['desc'],
                    lifecycle_state="LIVE", # Auto-approve for testing
                    approved_at=datetime.utcnow(),
                    approved_by="SYSTEM"
                )
                db.add(contract)
                print(f"Created Contract: {c['id']}")
            else:
                contract.allowed_universes = c['universes']
                contract.timeframe = c['timeframe']
                contract.lifecycle_state = "LIVE"
                print(f"Updated Contract: {c['id']}")
                
        db.commit()
        print("Config Seeding Complete.")
        
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_config()
