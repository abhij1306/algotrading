"""
Seed Strategy Contracts
Creates initial strategy contracts in database per MASTER_PROMPT

Run once: python seed_strategies.py
"""
import sys
from datetime import datetime

# Add parent to path
sys.path.append('.')

from app.database import SessionLocal, StrategyContract, Base, engine


def create_tables():
    """Create tables if they don't exist"""
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Tables verified/created")
    except Exception as e:
        print(f"⚠️  Table creation: {e}")


def seed_strategies():
    """Seed strategy contracts as per MASTER_PROMPT approved set"""
    create_tables()
   
    db = SessionLocal()
    
    strategies = [
        {
            "strategy_id": "INDEX_TREND_DAILY",
            "description": "Index Trend Following (Daily)",
            "regime": "Momentum",
            "timeframe": "1D",
            "holding_period": "Multi-day",
            "allowed_universes": ["NIFTY50_CORE", "BANKNIFTY"],
            "when_loses": "Choppy, range-bound markets with frequent trend reversals.",
            "lifecycle_state": "RESEARCH",
            "parameters": {
                "trend_filter_days": 50,
                "entry_threshold": 0.02,
                "stop_loss_pct": 2.0
            }
        },
        {
            "strategy_id": "INTRADAY_MEAN_REVERSION",
            "description": "Intraday Index Mean Reversion",
            "regime": "MeanReversion",
            "timeframe": "5MIN",
            "holding_period": "Same day",
            "allowed_universes": ["NIFTY50_CORE"],
            "when_loses": "Strong trend days and breakout regimes.",
            "lifecycle_state": "RESEARCH",
            "parameters": {
                "deviation_threshold": 1.5,
                "lookback_periods": 20,
                "take_profit_pct": 0.5
            }
        },
        {
            "strategy_id": "ORB_NIFTY_5MIN",
            "description": "Opening Range Breakout - NIFTY 5MIN",
            "regime": "Momentum",
            "timeframe": "5MIN",
            "holding_period": "Intraday",
            "allowed_universes": ["NIFTY50_CORE"],
            "when_loses": "Low volatility days with no clear directional bias.",
            "lifecycle_state": "LIVE",  # Already approved for paper trading
            "parameters": {
                "or_duration_minutes": 15,
                "breakout_threshold": 0.001,
                "stop_loss_pct": 1.0,
                "take_profit_pct": 2.0
            }
        }
    ]
    
    print("=" * 80)
    print("SEEDING STRATEGY CONTRACTS")
    print("=" * 80)
    
    created_count = 0
    existing_count = 0
    
    for strategy_data in strategies:
        # Check if exists
        existing = db.query(StrategyContract).filter_by(
            strategy_id=strategy_data["strategy_id"]
        ).first()
        
        if existing:
            print(f"⏭️  SKIPPED: {strategy_data['strategy_id']}")
            existing_count += 1
            continue
        
        # Create new
        try:
            strategy = StrategyContract(
                strategy_id=strategy_data["strategy_id"],
                description=strategy_data["description"],
                regime=strategy_data["regime"],
                timeframe=strategy_data["timeframe"],
                holding_period=strategy_data["holding_period"],
                allowed_universes=strategy_data["allowed_universes"],
                when_loses=strategy_data["when_loses"],
                lifecycle_state=strategy_data["lifecycle_state"],
                parameters=strategy_data["parameters"]
            )
            db.add(strategy)
            db.commit()
            print(f"✅ CREATED: {strategy_data['strategy_id']} ({strategy_data['lifecycle_state']})")
            created_count += 1
        except Exception as e:
            print(f"❌ ERROR: {strategy_data['strategy_id']} - {e}")
            db.rollback()
    
    db.close()
    
    print("=" * 80)
    print(f"SUMMARY: Created {created_count}, Skipped {existing_count}")
    print("=" * 80)


if __name__ == "__main__":
    try:
        seed_strategies()
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
