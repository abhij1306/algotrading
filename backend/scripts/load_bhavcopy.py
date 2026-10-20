"""
Bhavcopy Loader Script
======================
Loads daily bhavcopy data from NSE CSV file into the database.

Usage:
    python scripts/load_bhavcopy.py                      # Load today's bhavcopy
    python scripts/load_bhavcopy.py --date 2026-02-13    # Load specific date
    python scripts/load_bhavcopy.py --file path/to/file.csv  # Load specific file

The bhavcopy contains: SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE,
HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY,
TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
"""

import argparse
import logging
import os
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Company, HistoricalPrice

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_date_from_filename(filename: str) -> date:
    """Extract date from bhavcopy filename like sec_bhavdata_full_13022026.csv"""
    # Format: sec_bhavdata_full_DDMMYYYY.csv
    try:
        basename = os.path.basename(filename)
        date_str = basename.replace("sec_bhavdata_full_", "").replace(".csv", "")
        return datetime.strptime(date_str, "%d%m%Y").date()
    except Exception as e:
        logger.warning(f"Could not parse date from filename: {e}")
        return date.today()


def load_bhavcopy(csv_path: str, target_date: date = None) -> dict:
    """
    Load bhavcopy CSV into database.

    Args:
        csv_path: Path to the bhavcopy CSV file
        target_date: Optional date override (otherwise extracted from filename)

    Returns:
        dict with statistics
    """
    stats = {"total": 0, "success": 0, "skipped": 0, "failed": 0, "errors": []}

    # Parse date from filename if not provided
    if target_date is None:
        target_date = parse_date_from_filename(csv_path)

    logger.info(f"Loading bhavcopy for date: {target_date}")

    # Read CSV
    df = pd.read_csv(csv_path)

    # Clean column names (remove leading spaces)
    df.columns = df.columns.str.strip()

    # Filter only equity series (EQ)
    df_eq = df[df["SERIES"].str.strip() == "EQ"].copy()
    stats["total"] = len(df_eq)
    logger.info(f"Found {stats['total']} EQ records")

    # Get symbols from bhavcopy for company sync
    bhavcopy_symbols = set(df_eq["SYMBOL"].str.strip().tolist())

    db = SessionLocal()

    try:
        _sync_companies(db, bhavcopy_symbols, stats)
    except Exception as e:
        logger.warning(f"Error updating company list: {e}")

    try:
        for _, row in df_eq.iterrows():
            try:
                if _upsert_price(db, row, target_date):
                    stats["success"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append(f"{row.get('SYMBOL', 'UNKNOWN')}: {str(e)}")

        db.commit()
        logger.info(f"Successfully processed {stats['success']} records")

    except Exception as e:
        logger.exception(f"Database error: {e}")
        db.rollback()
        stats["errors"].append(f"Database error: {str(e)}")
    finally:
        db.close()

    return stats


def _sync_companies(db: Session, bhavcopy_symbols: set[str], stats: dict) -> None:
    """Add companies new in the bhavcopy and mark every traded symbol active."""
    existing_symbols = {c[0] for c in db.query(Company.symbol).all()}

    new_symbols = bhavcopy_symbols - existing_symbols
    if new_symbols:
        logger.info(f"Adding {len(new_symbols)} new companies to database")
        for sym in new_symbols:
            try:
                db.add(Company(symbol=sym, is_active=True))
            except Exception as e:
                logger.warning(f"Could not add {sym}: {e}")
        db.commit()
        stats["added"] = len(new_symbols)

    # Mark all bhavcopy symbols as active (they traded today)
    if bhavcopy_symbols:
        logger.info(f"Marking {len(bhavcopy_symbols)} companies as active")
        db.query(Company).filter(Company.symbol.in_(bhavcopy_symbols)).update(
            {Company.is_active: True}, synchronize_session=False
        )
        db.commit()
        stats["activated"] = len(bhavcopy_symbols)


def _price_fields(row: pd.Series) -> Iterator[tuple[str, object]]:
    """HistoricalPrice OHLCV and delivery fields of a bhavcopy row, converted one at a time.

    Blank volume/delivery values count as 0.
    """
    yield "open", float(row["OPEN_PRICE"])
    yield "high", float(row["HIGH_PRICE"])
    yield "low", float(row["LOW_PRICE"])
    yield "close", float(row["CLOSE_PRICE"])
    yield "volume", int(row["TTL_TRD_QNTY"]) if pd.notna(row["TTL_TRD_QNTY"]) else 0
    yield "deliverable_qty", int(row["DELIV_QTY"]) if pd.notna(row["DELIV_QTY"]) else 0
    yield "delivery_pct", float(row["DELIV_PER"]) if pd.notna(row["DELIV_PER"]) else 0


def _upsert_price(db: Session, row: pd.Series, target_date: date) -> bool:
    """Insert or update the row's price for the date; False when the company is unknown."""
    symbol = row["SYMBOL"].strip()
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        return False

    existing = (
        db.query(HistoricalPrice)
        .filter(
            HistoricalPrice.company_id == company.id,
            HistoricalPrice.date == target_date,
        )
        .first()
    )
    if existing:
        # Same order as the original field-by-field update
        for name, value in _price_fields(row):
            setattr(existing, name, value)
    else:
        db.add(HistoricalPrice(company_id=company.id, date=target_date, **dict(_price_fields(row))))
    return True


def find_latest_bhavcopy() -> str:
    """Find the latest bhavcopy file in the bhavcopy directory."""
    # Canonical location
    possible_paths = [
        Path(__file__).parent.parent.parent / "data_system" / "01_sources" / "nse_bhavcopy",
    ]

    bhavcopy_dir = None
    for p in possible_paths:
        if p.exists():
            bhavcopy_dir = p
            break

    if not bhavcopy_dir:
        raise FileNotFoundError(f"Bhavcopy directory not found. Tried: {possible_paths}")

    # Find all CSV files
    csv_files = list(bhavcopy_dir.glob("*.csv"))

    if not csv_files:
        raise FileNotFoundError(f"No CSV files found in {bhavcopy_dir}")

    # Sort by modification time (newest first)
    latest = max(csv_files, key=lambda f: f.stat().st_mtime)
    return str(latest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load NSE bhavcopy into database")
    parser.add_argument("--file", "-f", help="Path to bhavcopy CSV file")
    parser.add_argument("--date", "-d", help="Target date (YYYY-MM-DD)", default=None)
    parser.add_argument(
        "--latest", "-l", action="store_true", help="Use latest file in bhavcopy directory"
    )

    args = parser.parse_args()

    # Determine which file to load
    if args.latest or args.file is None:
        csv_path = find_latest_bhavcopy()
        logger.info(f"Using latest bhavcopy: {csv_path}")
    else:
        csv_path = args.file

    # Parse date
    target_date = None
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    # Load data
    stats = load_bhavcopy(csv_path, target_date)

    # Print summary
    logger.info("=" * 50)
    logger.info("BHAVCOPY LOAD SUMMARY")
    logger.info("=" * 50)
    logger.info(f"Total records:    {stats['total']}")
    logger.info(f"Success:          {stats['success']}")
    logger.info(f"Skipped:          {stats['skipped']}")
    logger.info(f"Failed:           {stats['failed']}")

    if stats["errors"]:
        logger.info(f"\nErrors ({len(stats['errors'])}):")
        for error in stats["errors"][:10]:
            logger.info(f"  - {error}")


if __name__ == "__main__":
    main()
