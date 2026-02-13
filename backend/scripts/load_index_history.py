
import os
import sys
from pathlib import Path
from datetime import date, timedelta
import calendar
import logging

# Add backend to path
sys.path.append(str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models.index_membership import IndexMembership
from app.services.index_weightage_parser import IndexWeightageParser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_last_day_of_month(any_day: date) -> date:
    last_day = calendar.monthrange(any_day.year, any_day.month)[1]
    return date(any_day.year, any_day.month, last_day)

def load_index_history():
    db = SessionLocal()
    parser = IndexWeightageParser()
    files = parser.get_all_files()

    if not files:
        logger.warning("No weightage files found in nse_data/index_universe/weightages")
        return

    logger.info(f"Found {len(files)} weightage files to process")

    for filepath in files:
        logger.info(f"Processing {filepath.name}...")
        records = parser.parse_file(filepath)

        if not records:
            continue

        # Each file represents a month
        # We assume the records in the file are valid for the entire month
        file_date = records[0]['date']
        start_date = date(file_date.year, file_date.month, 1)
        end_date = get_last_day_of_month(start_date)

        count = 0
        for rec in records:
            # Check if record already exists for this index, symbol, and start_date
            existing = db.query(IndexMembership).filter(
                IndexMembership.index_name == rec['index_name'],
                IndexMembership.symbol == rec['symbol'],
                IndexMembership.start_date == start_date
            ).first()

            if existing:
                existing.weight = rec['weight']
                existing.end_date = end_date
            else:
                new_membership = IndexMembership(
                    index_name=rec['index_name'],
                    symbol=rec['symbol'],
                    start_date=start_date,
                    end_date=end_date,
                    weight=rec['weight']
                )
                db.add(new_membership)

            count += 1

        db.commit()
        logger.info(f"Loaded/Updated {count} records for {start_date.strftime('%B %Y')}")

    db.close()
    logger.info("Index history loading completed.")

if __name__ == "__main__":
    load_index_history()
