"""
Smart NSE Bhavcopy Downloader - Only Missing Files

Scans existing downloads and only fetches missing files.
Much faster than checking each date sequentially.
"""

import os
import glob
from datetime import date, timedelta
from pathlib import Path

# Quick scan of what we have
def scan_existing_files():
    """Scan and return set of dates we already have"""
    base_dir = Path("nse_data/raw/equities")
    existing_dates = set()
    
    # Find all CSV files
    for csv_file in base_dir.rglob("cm*.csv"):
        # Extract date from filename: cm01JAN2016bhav.csv
        filename = csv_file.stem
        try:
            # Parse: cm + DD + MMM + YYYY
            day = filename[2:4]
            month = filename[4:7]
            year = filename[7:11]
            
            # Convert to date
            month_map = {
                'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
                'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
                'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
            }
            
            if month in month_map:
                file_date = date(int(year), month_map[month], int(day))
                existing_dates.add(file_date)
        except:
            pass
    
    return existing_dates

def get_missing_dates(start_date, end_date):
    """Get list of dates we need to download"""
    existing = scan_existing_files()
    print(f"Found {len(existing):,} existing files")
    
    missing = []
    current = start_date
    
    while current <= end_date:
        # Skip weekends
        if current.weekday() < 5:  # Monday=0, Friday=4
            if current not in existing:
                missing.append(current)
        current += timedelta(days=1)
    
    return missing

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) != 3:
        print("Usage: python scan_missing.py YYYY-MM-DD YYYY-MM-DD")
        sys.exit(1)
    
    start = date.fromisoformat(sys.argv[1])
    end = date.fromisoformat(sys.argv[2])
    
    print(f"Scanning {start} to {end}...")
    missing = get_missing_dates(start, end)
    
    print(f"\nMissing {len(missing):,} files")
    
    if missing:
        print(f"\nFirst missing: {missing[0]}")
        print(f"Last missing:  {missing[-1]}")
        
        # Group by year for easier download
        by_year = {}
        for d in missing:
            year = d.year
            if year not in by_year:
                by_year[year] = []
            by_year[year].append(d)
        
        print(f"\nMissing files by year:")
        for year in sorted(by_year.keys()):
            print(f"  {year}: {len(by_year[year]):,} files")
        
        # Suggest download commands
        print(f"\n💡 Download commands:")
        for year in sorted(by_year.keys()):
            dates = by_year[year]
            start_date = min(dates)
            end_date = max(dates)
            print(f"python nse_bhavcopy_downloader.py {start_date} {end_date}")
