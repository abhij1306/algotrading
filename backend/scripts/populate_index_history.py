"""
Populate Index History Script
=============================
Reads current NSE index constituents from CSV files via IndexUniverseLoader
and populates the IndexConstituentHistory table.

Since we don't have historical rebalance data yet, we initialize all current
constituents with effective_from='2000-01-01' to ensure they appear in
historical queries.
"""
import logging
import sys
import traceback
from datetime import date
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.append(str(PROJECT_ROOT))

from app.database import SessionLocal
from app.models.universe import IndexConstituentHistory, IndexUniverseDefinition
from app.services.index_universe_loader import index_universe_loader
from app.services.symbol_master import symbol_master

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def populate_index_history():
    db = SessionLocal()
    try:
        # Load all indices from CSVs
        logger.info("Loading index data from CSVs...")
        index_universe_loader.load_all()

        # Get all available indices
        indices = index_universe_loader.get_available_indices()
        logger.info(f"Found {len(indices)} indices to process")

        # Create IndexUniverseDefinitions first
        for index_code in indices:
            desc = index_universe_loader.get_index_description(index_code)

            # Check or create definition
            definition = db.query(IndexUniverseDefinition).filter(
                IndexUniverseDefinition.index_code == index_code
            ).first()

            if not definition:
                definition = IndexUniverseDefinition(
                    index_code=index_code,
                    index_name=desc,
                    description=desc,
                    is_custom=False,
                    last_download_date="2000-01-01",
                    last_weightage_file_date="2000-01-01"
                )
                db.add(definition)
                db.flush()
                logger.info(f"Created definition for {index_code}")

            # Get constituents
            symbols = index_universe_loader.get_index_symbols(index_code)
            universe = index_universe_loader.get_index_universe(index_code)

            count = 0
            for constituent in universe.constituents:
                # Check if already exists
                exists = db.query(IndexConstituentHistory).filter(
                    IndexConstituentHistory.universe_id == definition.id,
                    IndexConstituentHistory.symbol == constituent.symbol,
                    IndexConstituentHistory.effective_from == date(2000, 1, 1)
                ).first()

                if not exists:
                    # Get Fyers symbol from master
                    fyers_symbol = symbol_master.to_fyers(constituent.symbol)

                    history = IndexConstituentHistory(
                        universe_id=definition.id,
                        symbol=constituent.symbol,
                        fyers_symbol=fyers_symbol,
                        isin=constituent.isin,
                        company_name=constituent.company_name,
                        industry=constituent.industry,
                        effective_from=date(2000, 1, 1),
                        effective_to=None,  # Active indefinitely
                        import_date=date.today(),
                        source_file=f"init_load_{date.today()}"
                    )
                    db.add(history)
                    count += 1

            logger.info(f"Populated {count} symbols for {index_code}")

        db.commit()
        logger.info("Index history population complete!")

    except Exception as e:
        logger.error(f"Failed to populate index history: {e}")
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    # Ensure tables exist
    # Base.metadata.create_all(bind=engine) # Tables should already exist via alembic or app startup
    populate_index_history()
