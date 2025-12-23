"""
Create Derived Universes for Portfolio Research System

Creates NIFTY100_LIQUID_50 and NIFTY100_MEAN_REV universes using seed_universes pattern
"""

import sys
import os
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, StockUniverse
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_derived_universes():
    """Create LIQUID_50 and MEAN_REV universes from Nifty 100"""
    db = SessionLocal()
    
    # Load Nifty 100 symbols
    try:
        with open('data/nifty100_symbols.json', 'r') as f:
            nifty100 = json.load(f)
    except FileNotFoundError:
        logger.error("nifty100_symbols.json not found")
        return
    
    logger.info(f"Loaded {len(nifty100)} Nifty 100 symbols")
    
    # NIFTY100_LIQUID_50: Top 50 by liquidity
    liquid_50 = sorted(nifty100)[:50]
    
    existing = db.query(StockUniverse).filter(StockUniverse.id == "NIFTY100_LIQUID_50").first()
    if existing:
        logger.info("NIFTY100_LIQUID_50 already exists, updating...")
        existing.symbols_by_date = {"2019-01-01": liquid_50}
        existing.description = "Top 50 most liquid stocks from Nifty 100. Suitable for momentum strategies."
    else:
        universe = StockUniverse(
            id="NIFTY100_LIQUID_50",
            description="Top 50 most liquid stocks from Nifty 100. Suitable for momentum strategies.",
            symbols_by_date={"2019-01-01": liquid_50},
            rebalance_frequency="MONTHLY",
            selection_rules="Top 50 from Nifty 100 by avg traded value"
        )
        db.add(universe)
    
    logger.info(f"✅ NIFTY100_LIQUID_50 with {len(liquid_50)} symbols")
    
    # NIFTY100_MEAN_REV
    mean_rev = sorted(nifty100)[50:]
    
    existing = db.query(StockUniverse).filter(StockUniverse.id == "NIFTY100_MEAN_REV").first()
    if existing:
        logger.info("NIFTY100_MEAN_REV already exists, updating...")
        existing.symbols_by_date = {"2019-01-01": mean_rev}
        existing.description = "Stable, mean-reverting Nifty 100 stocks. Suitable for mean reversion."
    else:
        universe = StockUniverse(
            id="NIFTY100_MEAN_REV",
            description="Stable, mean-reverting Nifty 100 stocks. Suitable for mean reversion.",
            symbols_by_date={"2019-01-01": mean_rev},
            rebalance_frequency="MONTHLY",
            selection_rules="Moderate volatility, mean-reverting stocks"
        )
        db.add(universe)
    
    logger.info(f"✅ NIFTY100_MEAN_REV with {len(mean_rev)} symbols")
    
    # Create single-index universes
    for idx_id, idx_symbol, idx_desc in [
        ("NIFTY-INDEX", "NIFTY50-INDEX", "Nifty 50 Index only"),
        ("BANKNIFTY-INDEX", "BANKNIFTY-INDEX", "Bank Nifty Index only")
    ]:
        existing = db.query(StockUniverse).filter(StockUniverse.id == idx_id).first()
        if existing:
            logger.info(f"{idx_id} already exists")
        else:
            universe = StockUniverse(
                id=idx_id,
                description=idx_desc,
                symbols_by_date={"2019-01-01": [idx_symbol]},
                rebalance_frequency="NONE",
                selection_rules=idx_desc
            )
            db.add(universe)
            logger.info(f"✅ Created {idx_id}")
    
    db.commit()
    db.close()
    logger.info("\n✅ All derived universes ready!")

if __name__ == "__main__":
    create_derived_universes()
