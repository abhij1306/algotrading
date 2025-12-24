import sys
import os
from datetime import datetime

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, StrategyContract

def seed_nifty_strategies():
    """Seed 10 institutional-grade NIFTY strategies"""
    db = SessionLocal()
    
    nifty_strategies = [
        {
            "strategy_id": "NIFTY_VOL_CONTRACTION",
            "allowed_universes": ["NIFTY50", "NIFTY100"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "LOW_VOL_TREND",
            "description": "Volatility Contraction Breakout - Enters when ATR contracts below historical average and price breaks out with volume.",
            "when_loses": "False breakouts in choppy, range-bound markets without clear direction.",
            "parameters": {"volatility_lookback": 60, "contraction_threshold": 0.7, "volume_filter": 1.2},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_DARVAS_BOX",
            "allowed_universes": ["NIFTY50", "NIFTY100"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "STRONG_TREND",
            "description": "Darvas Box Breakout - Trades new 50-day highs confirmed by volume surge above 1.1x average.",
            "when_loses": "Whipsaws and false breakouts during sideways consolidation phases.",
            "parameters": {"highs_period": 50, "min_volume_spike": 1.1},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_MTF_TREND",
            "allowed_universes": ["NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "SUSTAINED_BULL",
            "description": "Multi-Timeframe Trend - Price above 150-day SMA with momentum confirmation for long-term trends.",
            "when_loses": "Late entries in exhausted trends or sudden reversals in bull markets.",
            "parameters": {"sma_period": 150, "momentum_threshold": 0.02},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_DUAL_MA",
            "allowed_universes": ["NIFTY50", "NIFTY100"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "TRENDING",
            "description": "Dual MA Crossover - 50 EMA crosses above 200 SMA with momentum filter for trend changes.",
            "when_loses": "Choppy sideways markets generating frequent false crossover signals.",
            "parameters": {"fast_period": 50, "slow_period": 200},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_VOL_SPIKE",
            "allowed_universes": ["NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "INTRADAY",
            "regime": "HIGH_MOMENTUM",
            "description": "Volume Spike Breakout - 90-day high breakout with volume 1.5x above average for momentum trades.",
            "when_loses": "Volume spikes on distribution or profit-taking at resistance levels.",
            "parameters": {"breakout_period": 90, "volume_multiplier": 1.5},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_TREND_ENVELOPE",
            "allowed_universes": ["NIFTY50", "NIFTY100"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "EXPANSION",
            "description": "Bollinger Band Breakout - Closes above upper BB (20, 2.0) indicating strong momentum expansion.",
            "when_loses": "Mean reversion in ranging markets or failed breakouts above resistance.",
            "parameters": {"bb_period": 20, "bb_std": 2.0},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_REGIME_MOM",
            "allowed_universes": ["NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "ADAPTIVE",
            "description": "Regime-Based Momentum - Adapts to market regime (bull/bear/neutral) using correlation and volatility metrics.",
            "when_loses": "Regime mis-classification during transition periods or structural market changes.",
            "parameters": {"regime_lookback": 60},
            "lifecycle_state": "RESEARCH",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_ATR_BREAK",
            "allowed_universes": ["NIFTY50", "NIFTY100"],
            "timeframe": "DAILY",
            "holding_period": "INTRADAY",
            "regime": "VOLATILE_TREND",
            "description": "ATR Breakout - Price breaks above 20-MA + (1.8 × ATR) envelope confirming strong momentum move.",
            "when_loses": "False breakouts in low volatility environments or mean-reverting markets.",
            "parameters": {"ma_period": 20, "atr_multiplier": 1.8},
            "lifecycle_state": "LIVE",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_MA_RIBBON",
            "allowed_universes": ["NIFTY50"],
            "timeframe": "DAILY",
            "holding_period": "MULTI_DAY",
            "regime": "SUSTAINED_TREND",
            "description": "MA Ribbon - All short-term MAs aligned above long-term MAs indicating strong trend alignment.",
            "when_loses": "Lagging signals in fast-moving markets or sudden trend reversals.",
            "parameters": {"ribbon_periods": [10, 20, 30, 50, 100]},
            "lifecycle_state": "RESEARCH",
            "approved_by": "Quant Team"
        },
        {
            "strategy_id": "NIFTY_MACRO_BREAK",
            "allowed_universes": ["NIFTY50"],
            "timeframe": "WEEKLY",
            "holding_period": "MULTI_MONTH",
            "regime": "BULL_ONSET",
            "description": "Macro Breakout - Quarterly/yearly high breakouts capturing major structural trend changes.",
            "when_loses": "Rare signals with long holding periods; sensitive to macro shocks.",
            "parameters": {"breakout_period": 252},  # ~1 year
            "lifecycle_state": "RESEARCH",
            "approved_by": "Quant Team"
        }
    ]
    
    print("🔄 Seeding 10 NIFTY Quant Strategies...")
    added_count = 0
    skipped_count = 0
    
    for s_data in nifty_strategies:
        existing = db.query(StrategyContract).filter(
            StrategyContract.strategy_id == s_data["strategy_id"]
        ).first()
        
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
            print(f"  ✅ Added: {s_data['strategy_id']}")
            added_count += 1
        else:
            print(f"  ⏭️  Skipped: {s_data['strategy_id']} (Already exists)")
            skipped_count += 1
    
    db.commit()
    db.close()
    
    print(f"\n✨ Seeding Complete!")
    print(f"   Added: {added_count} strategies")
    print(f"   Skipped: {skipped_count} strategies")
    print(f"   Total: {len(nifty_strategies)} strategies\n")

if __name__ == "__main__":
    seed_nifty_strategies()
