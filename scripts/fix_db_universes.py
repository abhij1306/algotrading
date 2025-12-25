"""Update strategy contracts in database with correct universe IDs"""
from backend.app.database import SessionLocal, StrategyContract

db = SessionLocal()

# Update all strategies with new universe IDs
updates = {
    "NIFTY_VOL_CONTRACTION": ["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
    "NIFTY_DARVAS_BOX": ["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
    "NIFTY_MTF_TREND": ["NIFTY50_CORE", "NIFTY100_CORE"],
    "NIFTY_DUAL_MA": ["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100", "NIFTY_SMALLCAP_100"],
    "NIFTY_VOL_SPIKE": ["NIFTY50_CORE", "NIFTY100_CORE"],
    "NIFTY_TREND_ENVELOPE": ["NIFTY50_CORE", "NIFTY100_CORE"],
    "NIFTY_REGIME_MOM": ["NIFTY50_CORE"],
    "NIFTY_ATR_BREAK": ["NIFTY50_CORE", "NIFTY100_CORE", "NIFTY_MIDCAP_100"],
    "NIFTY_MA_RIBBON": ["NIFTY50_CORE", "NIFTY100_CORE"],
    "NIFTY_MACRO_BREAK": ["NIFTY50_CORE"],
}

for strategy_id, universes in updates.items():
    contract = db.query(StrategyContract).filter_by(strategy_id=strategy_id).first()
    if contract:
        print(f"Updating {strategy_id}: {contract.allowed_universes} -> {universes}")
        contract.allowed_universes = universes
    else:
        print(f"WARNING: {strategy_id} not found in database!")

db.commit()
print("\n✅ Database updated successfully!")

# Verify
print("\nVerification:")
for sid in ["NIFTY_VOL_CONTRACTION", "NIFTY_DARVAS_BOX", "NIFTY_REGIME_MOM"]:
    c = db.query(StrategyContract).filter_by(strategy_id=sid).first()
    print(f"{sid}: {c.allowed_universes if c else 'NOT FOUND'}")

db.close()
