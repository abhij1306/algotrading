import os
import sys
import time
import zipfile
import requests
import datetime
from io import BytesIO

# ---------------- CONFIG ----------------
BASE_URL = "https://archives.nseindia.com/content/historical/EQUITIES"
ROOT_DIR = "nse_data/raw/equities"

# Default: Last 5 years (more likely to have data)
START_DATE = datetime.date(2020, 1, 1)
END_DATE = datetime.date.today()

REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_REQUESTS = 1.5  # seconds
MAX_CONSECUTIVE_MISSING = 30  # Stop if too many consecutive missing files

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/zip",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive"
}

# ----------------------------------------


def daterange(start_date, end_date):
    """Generate dates between start and end"""
    for n in range((end_date - start_date).days + 1):
        yield start_date + datetime.timedelta(n)


def is_weekend(date):
    """Check if date is Saturday or Sunday"""
    return date.weekday() >= 5


def print_progress(current, total, status, date):
    """Print progress bar with status"""
    percent = (current / total) * 100
    bar_length = 40
    filled = int(bar_length * current / total)
    bar = '█' * filled + '░' * (bar_length - filled)
    
    print(f'\r[{bar}] {percent:.1f}% | {date} → {status}', end='', flush=True)


def download_bhavcopy(date):
    """Download bhavcopy for a specific date"""
    year = date.strftime("%Y")
    month = date.strftime("%b").upper()
    day = date.strftime("%d")

    filename = f"cm{day}{month}{year}bhav.csv"
    zip_filename = filename + ".zip"

    url = f"{BASE_URL}/{year}/{month}/{zip_filename}"

    save_dir = os.path.join(ROOT_DIR, year, month)
    os.makedirs(save_dir, exist_ok=True)

    csv_path = os.path.join(save_dir, filename)

    # Skip if already downloaded
    if os.path.exists(csv_path):
        return "exists"

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 404:
            return "missing"
        elif response.status_code != 200:
            return f"http_{response.status_code}"

        # Verify it's a valid zip file
        try:
            with zipfile.ZipFile(BytesIO(response.content)) as z:
                z.extractall(save_dir)
        except zipfile.BadZipFile:
            return "bad_zip"

        return "downloaded"

    except requests.Timeout:
        return "timeout"
    except requests.RequestException as e:
        return f"network_error"
    except Exception as e:
        return f"error"


def run(start_date=None, end_date=None):
    """Main download function"""
    start = start_date or START_DATE
    end = end_date or END_DATE
    
    print(f"NSE Bhavcopy Downloader")
    print(f"{'='*60}")
    print(f"Date Range: {start} to {end}")
    print(f"Target Dir: {ROOT_DIR}")
    print(f"{'='*60}\n")

    stats = {
        "downloaded": 0,
        "exists": 0,
        "missing": 0,
        "errors": 0
    }
    
    # Count total trading days (excluding weekends)
    total_days = sum(1 for d in daterange(start, end) if not is_weekend(d))
    current_day = 0
    consecutive_missing = 0

    for date in daterange(start, end):
        if is_weekend(date):
            continue

        current_day += 1
        status = download_bhavcopy(date)

        # Track consecutive missing files
        if status == "missing":
            consecutive_missing += 1
            stats["missing"] += 1
        else:
            consecutive_missing = 0
            if status == "downloaded":
                stats["downloaded"] += 1
            elif status == "exists":
                stats["exists"] += 1
            else:
                stats["errors"] += 1

        print_progress(current_day, total_days, status, date)

        # Stop if too many consecutive missing files
        if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
            print(f"\n\n⚠️  Stopped: {MAX_CONSECUTIVE_MISSING} consecutive missing files")
            print(f"   Data might not be available before {date}")
            break

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    print("\n\n" + "="*60)
    print("Download Summary:")
    print("="*60)
    print(f"✓ Downloaded:  {stats['downloaded']:>6}")
    print(f"○ Already Had: {stats['exists']:>6}")
    print(f"✗ Missing:     {stats['missing']:>6}")
    print(f"⚠ Errors:      {stats['errors']:>6}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    # Allow command line date override
    if len(sys.argv) == 3:
        try:
            start = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
            end = datetime.datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
            run(start, end)
        except ValueError:
            print("Usage: python nse_bhavcopy_downloader.py YYYY-MM-DD YYYY-MM-DD")
            sys.exit(1)
    else:
        run()
