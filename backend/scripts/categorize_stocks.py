"""
Stock Categorization Script
==========================
Categorizes stocks into broad-based and sector indices from CSV files.

Usage:
    python scripts/categorize_stocks.py

This reads from: nse_data/index_universe/constituents/
Maps stocks to:
- broad_market: NIFTY50, NIFTY100, NIFTY200, NIFTY500, NIFTYNEXT50, etc.
- sector_index: NIFTYIT, NIFTYBANK, NIFTYAUTO, NIFTYPHARMA, etc.
"""
import csv
import logging
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models import Company

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Path to constituents CSV files
PROJECT_ROOT = Path(__file__).parent.parent.parent
CSV_PATH = PROJECT_ROOT / "nse_data" / "index_universe" / "constituents"

# Broad-based indices
BROAD_INDICES = {
    "ind_nifty50list.csv": "NIFTY50",
    "ind_nifty100list.csv": "NIFTY100",
    "ind_nifty200list.csv": "NIFTY200",
    "ind_nifty500list.csv": "NIFTY500",
    "ind_niftynext50list.csv": "NIFTYNEXT50",
    "ind_niftytotalmarket_list.csv": "NIFTYTOTALMARKET",
    "ind_NiftySmallcap500_list.csv": "NIFTYSMALLCAP500",
    # Midcap indices
    "ind_niftymidcap50list.csv": "NIFTYMIDCAP50",
    "ind_niftymidcap100list.csv": "NIFTYMIDCAP100",
    "ind_niftymidcap150list.csv": "NIFTYMIDCAP150",
    "ind_niftymidcapselect_list.csv": "NIFTYMIDCAPSELECT",
    # Smallcap indices
    "ind_niftysmallcap250list.csv": "NIFTYSMALLCAP250",
    "ind_niftylargemidcap250list.csv": "NIFTYLARGEMIDCAP250",
    "ind_niftymicrocap250_list.csv": "NIFTYMICROCAP250",
    "ind_niftymidsmallcap400list.csv": "NIFTYMIDSMALLCAP400",
}

# Sector indices
SECTOR_INDICES = {
    "ind_niftybanklist.csv": "NIFTYBANK",
    "ind_niftyitlist.csv": "NIFTYIT",
    "ind_niftyautolist.csv": "NIFTYAUTO",
    "ind_niftypharmalist.csv": "NIFTYPHARMA",
    "ind_niftyfinancelist.csv": "NIFTYFINANCE",
    "ind_niftyrealtylist.csv": "NIFTYREALTY",
    "ind_niftyfmcglist.csv": "NIFTYFMCG",
    "ind_niftymetallist.csv": "NIFTYMETAL",
    "ind_niftyoilgaslist.csv": "NIFTYOILGAS",
    "ind_nifty_privatebanklist.csv": "NIFTYPRIVATEBANK",
    "ind_niftypsubanklist.csv": "NIFTYPSUBANK",
    "ind_niftyhealthcarelist.csv": "NIFTYHEALTHCARE",
    "ind_niftyconsumerdurableslist.csv": "NIFTYCONSUMERDURABLES",
    "ind_niftyChemicals_list.csv": "NIFTYCHEMICALS",
    "ind_niftymedialist.csv": "NIFTYMEDIA",
    # Special sector indices
    "ind_niftymidsmallfinancailservice_list.csv": "NIFTYMIDSMALLFINANCIALSERVICES",
    "ind_niftymidsmallhealthcare_list.csv": "NIFTYMIDSMALLHEALTHCARE",
    "ind_niftymidsmallitAndtelecom_list.csv": "NIFTYMIDSMALLITANDTELECOM",
}


def get_all_csv_files() -> dict[str, str]:
    """Get all CSV files and their index codes."""
    files = {}
    files.update(BROAD_INDICES)
    files.update(SECTOR_INDICES)
    return files


def read_csv_symbols(csv_file: str) -> set[str]:
    """Read symbols from a CSV file."""
    symbols = set()
    csv_path = CSV_PATH / csv_file

    if not csv_path.exists():
        logger.warning(f"CSV file not found: {csv_path}")
        return symbols

    try:
        with open(csv_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try different column names
                symbol = row.get('Symbol') or row.get('symbol') or row.get('SYMBOL')
                if symbol:
                    symbols.add(symbol.strip().upper())
    except Exception as e:
        logger.error(f"Error reading {csv_file}: {e}")

    return symbols


def categorize_stocks(dry_run: bool = False):
    """Categorize all stocks from CSV files into indices."""
    logger.info("=" * 60)
    logger.info("Stock Categorization")
    logger.info("=" * 60)

    # Read all CSV files and build mappings
    broad_mapping: dict[str, list[str]] = {}  # symbol -> [indices]
    sector_mapping: dict[str, list[str]] = {}  # symbol -> [indices]

    csv_files = get_all_csv_files()

    for csv_file, index_code in csv_files.items():
        logger.info(f"Reading {csv_file} -> {index_code}")

        symbols = read_csv_symbols(csv_file)

        # Determine if broad or sector
        is_broad = csv_file in BROAD_INDICES

        for symbol in symbols:
            if is_broad:
                if symbol not in broad_mapping:
                    broad_mapping[symbol] = []
                broad_mapping[symbol].append(index_code)
            else:
                if symbol not in sector_mapping:
                    sector_mapping[symbol] = []
                sector_mapping[symbol].append(index_code)

        logger.info(f"  Found {len(symbols)} symbols")

    # Update database
    db = SessionLocal()
    try:
        # Get all companies
        companies = db.query(Company).filter(Company.is_active.is_(True)).all()

        updated = 0
        skipped = 0

        for company in companies:
            symbol = company.symbol

            # Get broad market indices
            broad_indices = broad_mapping.get(symbol, [])
            if broad_indices:
                company.broad_market = broad_indices[0]  # Take first one
            else:
                company.broad_market = None

            # Get sector indices
            sector_indices = sector_mapping.get(symbol, [])
            if sector_indices:
                company.sector_index = sector_indices[0]  # Take first one
            else:
                company.sector_index = None

            if broad_indices or sector_indices:
                updated += 1
            else:
                skipped += 1

        if not dry_run:
            db.commit()
            logger.info(f"Updated {updated} companies")
        else:
            logger.info(f"[DRY RUN] Would update {updated} companies")

        logger.info(f"Skipped (not in any index): {skipped}")

        # Print summary
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)

        # Count by index
        broad_counts = {}
        for _symbol, indices in broad_mapping.items():
            for idx in indices:
                broad_counts[idx] = broad_counts.get(idx, 0) + 1

        logger.info("Broad-based indices:")
        for idx, count in sorted(broad_counts.items()):
            logger.info(f"  {idx}: {count} stocks")

        sector_counts = {}
        for _symbol, indices in sector_mapping.items():
            for idx in indices:
                sector_counts[idx] = sector_counts.get(idx, 0) + 1

        logger.info("Sector indices:")
        for idx, count in sorted(sector_counts.items()):
            logger.info(f"  {idx}: {count} stocks")

    finally:
        db.close()


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description='Categorize stocks into indices')
    parser.add_argument('--dry-run', action='store_true', help='Run without saving to database')
    args = parser.parse_args()

    categorize_stocks(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
