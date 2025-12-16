"""
Index Membership Snapshotter

Takes monthly snapshots of NSE index constituents and tracks entry/exit dates.
Prevents survivorship bias in backtests.
Uses .env file for database credentials.

Run this monthly via cron/scheduler to maintain historical membership data.
"""

import os
import datetime
import pandas as pd
import psycopg2
import requests
from io import StringIO
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "niftybank": "https://archives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "niftyit": "https://archives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "niftypharma": "https://archives.nseindia.com/content/indices/ind_niftypharmalist.csv",
    "niftyfmcg": "https://archives.nseindia.com/content/indices/ind_niftyfmcglist.csv",
    "niftymetal": "https://archives.nseindia.com/content/indices/ind_niftymetallist.csv",
    "niftyenergy": "https://archives.nseindia.com/content/indices/ind_niftyenergylist.csv",
    "niftymidcap150": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "niftysmallcap250": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    "niftyfinservice": "https://archives.nseindia.com/content/indices/ind_niftyfinservicelist.csv"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv"
}

# Get Postgres credentials from .env
PG_CONN = {
    "host": os.getenv("DB_HOST", "localhost"),
    "dbname": os.getenv("DB_NAME", "algotrading"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
    "port": int(os.getenv("DB_PORT", 5432))
}


def create_table(conn):
    """Create index_membership table if it doesn't exist"""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_membership (
            symbol TEXT,
            index_name TEXT,
            start_date DATE,
            end_date DATE,
            PRIMARY KEY (symbol, index_name, start_date)
        );
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_index_membership_lookup
        ON index_membership(symbol, index_name, start_date, end_date);
    """)
    
    conn.commit()
    cursor.close()
    print("✓ Table schema created/verified")


def download_index_constituents(index_name, url):
    """Download current constituents for a specific index"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        
        df = pd.read_csv(StringIO(resp.text))
        
        # NSE index files have different column names, try to find symbol column
        symbol_col = None
        for col in df.columns:
            if 'symbol' in col.lower():
                symbol_col = col
                break
        
        if symbol_col:
            symbols = set(df[symbol_col].str.strip())
            return symbols
        else:
            print(f"  Warning: Could not find symbol column in {index_name}")
            return set()
            
    except Exception as e:
        print(f"  Error downloading {index_name}: {e}")
        return set()


def snapshot_index(conn, index_name, current_symbols, snapshot_date):
    """
    Update index membership by comparing current constituents with database.
    
    - New symbols: Add with start_date = snapshot_date
    - Removed symbols: Set end_date = snapshot_date
    """
    cursor = conn.cursor()
    
    # Get currently active members from database
    cursor.execute("""
        SELECT symbol FROM index_membership
        WHERE index_name = %s AND end_date IS NULL
    """, (index_name,))
    
    active_symbols = {row[0] for row in cursor.fetchall()}
    
    # Find new entries (in current but not in active)
    new_entries = current_symbols - active_symbols
    
    # Find exits (in active but not in current)
    exits = active_symbols - current_symbols
    
    # Insert new entries
    for symbol in new_entries:
        cursor.execute("""
            INSERT INTO index_membership (symbol, index_name, start_date, end_date)
            VALUES (%s, %s, %s, NULL)
            ON CONFLICT (symbol, index_name, start_date) DO NOTHING
        """, (symbol, index_name, snapshot_date))
    
    # Mark exits
    for symbol in exits:
        cursor.execute("""
            UPDATE index_membership
            SET end_date = %s
            WHERE symbol = %s AND index_name = %s AND end_date IS NULL
        """, (snapshot_date, symbol, index_name))
    
    conn.commit()
    cursor.close()
    
    return len(new_entries), len(exits), len(current_symbols)


def run():
    """Main snapshot pipeline"""
    print("=" * 60)
    print("NSE Index Membership Snapshotter")
    print("=" * 60)
    
    snapshot_date = datetime.date.today()
    print(f"\nSnapshot Date: {snapshot_date}")
    
    # Connect to Postgres
    conn = psycopg2.connect(**PG_CONN)
    create_table(conn)
    
    print("\nProcessing indices...")
    
    total_new = 0
    total_exits = 0
    
    for index_name, url in INDEX_URLS.items():
        print(f"\n  {index_name.upper()}:")
        
        # Download current constituents
        current_symbols = download_index_constituents(index_name, url)
        
        if not current_symbols:
            print(f"    Skipped (no data)")
            continue
        
        # Update membership
        new, exits, total = snapshot_index(conn, index_name, current_symbols, snapshot_date)
        
        print(f"    Current: {total} stocks")
        print(f"    New:     {new} entries")
        print(f"    Exits:   {exits} stocks")
        
        total_new += new
        total_exits += exits
    
    conn.close()
    
    print("\n" + "=" * 60)
    print("✅ Snapshot Complete!")
    print("=" * 60)
    print(f"\nTotal New Entries: {total_new}")
    print(f"Total Exits:       {total_exits}")
    print("\n💡 Query example:")
    print("   SELECT * FROM index_membership")
    print("   WHERE symbol = 'SBIN' AND index_name = 'nifty50'")
    print("   AND '2024-01-01' BETWEEN start_date AND COALESCE(end_date, '9999-12-31');")


if __name__ == "__main__":
    run()
