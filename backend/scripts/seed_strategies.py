import sys
import os
from datetime import datetime

# Add the backend directory to sys.path to resolve imports
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal, StrategyContract, StrategyMetadata

def seed_strategies():
    """
    Removes all existing strategies and seeds the 10 institutional strategies.
    """
    db = SessionLocal()
    
    try:
        print("🔌 Connecting to database...")

        # 1. Clear existing data
        print("🧹 Clearing existing strategies...")
        db.query(StrategyContract).delete()
        db.query(StrategyMetadata).delete()
        db.commit()

        # 2. Define new strategies
        new_strategies = [
            {
                "id": "NIFTY_VOL_CONTRACTION",
                "name": "NIFTY Volatility Contraction Breakout",
                "description": "Exploits breakouts from low-volatility coiling patterns.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails when volatility compressions falsify breakout",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_DARVAS_BOX",
                "name": "NIFTY Darvas Box Structural Breakout",
                "description": "Classic Darvas Box strategy focusing on new highs after consolidation.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails in choppy lateral markets",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_MTF_TREND",
                "name": "NIFTY Multi-Timeframe Trend",
                "description": "Aligns daily entries with weekly trend direction.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails when higher timeframe trend collapses",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_DUAL_MA",
                "name": "NIFTY Dual MA Crossover",
                "description": "Trend following using dual moving average crossovers with confirmation.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails in multi-cross whipsaws",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_VOL_SPIKE",
                "name": "NIFTY Volume Spike Breakout",
                "description": "Validates price breakouts with significant volume expansion.",
                "timeframe": "DAILY",
                "regime": "EVENT",
                "when_loses": "Fails with low follow-through breakouts",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_TREND_ENVELOPE",
                "name": "NIFTY Trend Envelope",
                "description": "Momentum strategy trading breakouts of calculated price envelopes.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails on mean-reversion days",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_REGIME_MOM",
                "name": "NIFTY Regime Filtered Momentum",
                "description": "Momentum strategy that activates only in favorable market regimes.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails when regime flips abruptly",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_ATR_BREAK",
                "name": "NIFTY ATR Breakout",
                "description": "Volatility expansion strategy using ATR bands.",
                "timeframe": "DAILY",
                "regime": "TREND",
                "when_loses": "Fails on false volatility accelerations",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_MA_RIBBON",
                "name": "NIFTY MA Ribbon",
                "description": "Multiple moving average alignment strategy.",
                "timeframe": "WEEKLY",
                "regime": "TREND",
                "when_loses": "Fails when ribbon order collapses",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            },
            {
                "id": "NIFTY_MACRO_BREAK",
                "name": "NIFTY Macro Breakout",
                "description": "Long-term breakout strategy with macro risk overlays.",
                "timeframe": "WEEKLY",
                "regime": "TREND",
                "when_loses": "Fails under rising risk regimes",
                "allowed_universes": ["NIFTY50"],
                "holding_period": "MULTI_DAY"
            }
        ]

        # 3. Insert new records
        print(f"🌱 Seeding {len(new_strategies)} strategies...")

        for s in new_strategies:
            # Contract
            contract = StrategyContract(
                strategy_id=s['id'],
                allowed_universes=s['allowed_universes'],
                timeframe=s['timeframe'],
                holding_period=s['holding_period'],
                regime=s['regime'],
                when_loses=s['when_loses'],
                description=s['description'],
                parameters={}, # Locked parameters
                lifecycle_state="RESEARCH",
                state_since=datetime.utcnow()
            )
            db.add(contract)

            # Metadata
            meta = StrategyMetadata(
                strategy_id=s['id'],
                display_name=s['name'],
                description=s['description'],
                regime_notes=s['when_loses'],
                lifecycle_status="RESEARCH",
                risk_profile={} # Empty initial profile
            )
            db.add(meta)

        db.commit()
        print("✅ Strategies seeded successfully!")

    except Exception as e:
        print(f"❌ Error seeding strategies: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_strategies()
