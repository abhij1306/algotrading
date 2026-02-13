
import csv
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import date
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)

class IndexWeightageParser:
    """
    Parses monthly index weightage files from NSE.
    Expected format: CSV with columns like [Index Name, Symbol, Weightage %]
    """

    def __init__(self, data_path: Optional[Path] = None):
        self.data_path = data_path or Path("nse_data/index_universe/weightages")

    def parse_file(self, filepath: Path) -> List[Dict[str, Any]]:
        """
        Parses a single weightage CSV file.
        Returns a list of records with normalized symbols and weights.
        """
        records = []
        if not filepath.exists():
            logger.error(f"File not found: {filepath}")
            return records

        # Try to extract date from filename (expected: weightages_YYYY_MM.csv)
        file_date = self._extract_date_from_filename(filepath.name)
        if not file_date:
            logger.warning(f"Could not extract date from filename {filepath.name}. Using file modification date.")
            file_date = date.fromtimestamp(filepath.stat().st_mtime)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                # Use Sniffer to detect delimiter
                content = f.read(1024)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(content)
                    reader = csv.DictReader(f, dialect=dialect)
                except csv.Error:
                    reader = csv.DictReader(f)

                for row in reader:
                    # Map common column names
                    index_name = row.get('Index Name') or row.get('Index') or row.get('INDEX NAME')
                    symbol_raw = row.get('Symbol') or row.get('SYMBOL') or row.get('Stock Symbol')
                    weight_raw = row.get('Weightage %') or row.get('Weight') or row.get('WEIGHTAGE (%)') or row.get('Weightage')

                    if not index_name or not symbol_raw:
                        continue

                    try:
                        # Normalize symbol using SymbolMaster
                        symbol = symbol_master.to_db(symbol_raw.strip())

                        # Parse weight
                        weight = 0.0
                        if weight_raw:
                            weight = float(str(weight_raw).replace('%', '').strip())

                        records.append({
                            'index_name': index_name.strip().upper(),
                            'symbol': symbol,
                            'weight': weight,
                            'date': file_date
                        })
                    except Exception as e:
                        logger.warning(f"Error parsing row {row}: {e}")

        except Exception as e:
            logger.error(f"Failed to parse weightage file {filepath}: {e}")

        return records

    def _extract_date_from_filename(self, filename: str) -> Optional[date]:
        """
        Extracts date from filename like 'weightages_2023_10.csv' or 'nifty50_oct2023.csv'
        """
        import re
        # Try YYYY_MM
        match = re.search(r'(\d{4})_(\d{2})', filename)
        if match:
            year, month = map(int, match.groups())
            return date(year, month, 1)

        # Try MM_YYYY
        match = re.search(r'(\d{2})_(\d{4})', filename)
        if match:
            month, year = map(int, match.groups())
            return date(year, month, 1)

        return None

    def get_all_files(self) -> List[Path]:
        """Returns list of all weightage CSV files in the data directory."""
        return sorted(list(self.data_path.glob("*.csv")))
