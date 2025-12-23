"""
Seed Strategy Contracts Table

Populates the database with hard-coded strategy contracts.
Run this once after schema creation.
"""

import sys
import os
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, StrategyContract
from app.engines.strategy_contracts import STRATEGY_CONTRACTS
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def seed_strategy_contracts():
    """Seed strategy contracts from hard-coded definitions"""
    db = SessionLocal()
    
    try:
        for strategy_id, contract in STRATEGY_CONTRACTS.items():
            # Check if already exists
            existing = db.query(StrategyContract).filter(
                StrategyContract.strategy_id == strategy_id
            ).first()
            
            if existing:
                logger.info(f"Contract for {strategy_id} already exists, updating...")
                existing.allowed_universes = contract.allowed_universes
                existing.timeframe = contract.timeframe
                existing.holding_period = contract.holding_period
                existing.regime = contract.regime
                existing.when_loses = contract.when_loses
                existing.description = contract.description
            else:
                db_contract = StrategyContract(
                    strategy_id=contract.strategy_id,
                    allowed_universes=contract.allowed_universes,
                    timeframe=contract.timeframe,
                    holding_period=contract.holding_period,
                    regime=contract.regime,
                    when_loses=contract.when_loses,
                    description=contract.description
                )
                db.add(db_contract)
                logger.info(f"Created contract for {strategy_id}")
        
        db.commit()
        logger.info(f"\n✅ Successfully seeded {len(STRATEGY_CONTRACTS)} strategy contracts")
        
        # Verify
        count = db.query(StrategyContract).count()
        logger.info(f"Total contracts in database: {count}")
        
    except Exception as e:
        logger.error(f"Error seeding contracts: {e}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    seed_strategy_contracts()
