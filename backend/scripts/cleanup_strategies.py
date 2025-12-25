import sys
import os

# Fix Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, StrategyContract

def cleanup_strategies():
    db = SessionLocal()
    
    # List of the 10 NIFTY strategies we want to KEEP
    keep_strategies = [
        "NIFTY_VOL_CONTRACTION",
        "NIFTY_DARVAS_BOX",
        "NIFTY_MTF_TREND",
        "NIFTY_DUAL_MA",
        "NIFTY_VOL_SPIKE",
        "NIFTY_TREND_ENVELOPE",
        "NIFTY_REGIME_MOM",
        "NIFTY_ATR_BREAK",
        "NIFTY_MA_RIBBON",
        "NIFTY_MACRO_BREAK"
    ]
    
    print(f"Cleaning up strategies...")
    print(f"Keeping only: {keep_strategies}")
    
    # Delete everything NOT in the keep list
    deleted = db.query(StrategyContract).filter(
        StrategyContract.strategy_id.notin_(keep_strategies)
    ).delete(synchronize_session=False)
    
    db.commit()
    db.close()
    
    print(f"Deleted {deleted} legacy strategies.")
    print("Cleanup Complete.")

if __name__ == "__main__":
    cleanup_strategies()
