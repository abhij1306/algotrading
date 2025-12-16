"""
Sector Mapping Loader

Downloads NSE equity list and loads sector classifications into Postgres.
Uses .env file for database credentials.
"""

import os
import pandas as pd
import psycopg2
import requests
from io import StringIO
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

EQUITY_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

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
    """Create sector_membership table if it doesn't exist"""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sector_membership (
            symbol TEXT,
            sector TEXT,
            start_date DATE,
            end_date DATE,
            PRIMARY KEY (symbol, start_date)
        );
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sector_lookup
        ON sector_membership(symbol, start_date, end_date);
    """)
    
    conn.commit()
    cursor.close()
    print("✓ Table schema created/verified")


def download_equity_list():
    """Download NSE equity list with sector classifications"""
    print("Downloading NSE equity list...")
    
    resp = requests.get(EQUITY_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    
    df = pd.read_csv(StringIO(resp.text))
    print(f"✓ Downloaded {len(df):,} equity records")
    
    return df


def load_sectors(df):
    """Load sector mappings into Postgres"""
    print("\nLoading sector mappings into Postgres...")
    
    # Clean and prepare data
    df = df[["SYMBOL", "INDUSTRY"]].dropna()
    df.columns = ["symbol", "sector"]
    
    # Set start_date to a historical date (assuming current classification)
    df["start_date"] = "2000-01-01"
    df["end_date"] = None
    
    print(f"Found {len(df):,} symbols with sector classifications")
    
    # Connect to Postgres
    conn = psycopg2.connect(**PG_CONN)
    create_table(conn)
    
    cursor = conn.cursor()
    
    # Insert sector mappings
    inserted = 0
    for _, row in df.iterrows():
        try:
            cursor.execute("""
                INSERT INTO sector_membership (symbol, sector, start_date, end_date)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (symbol, start_date) DO UPDATE
                SET sector = EXCLUDED.sector
            """, (row["symbol"], row["sector"], row["start_date"], row["end_date"]))
            inserted += 1
        except Exception as e:
            print(f"  Warning: Failed to insert {row['symbol']}: {e}")
    
    conn.commit()
    cursor.close()
    conn.close()
    
    print(f"✓ Loaded {inserted:,} sector mappings")


def run():
    """Main pipeline"""
    print("=" * 60)
    print("NSE Sector Mapping Loader")
    print("=" * 60)
    
    # Download equity list
    df = download_equity_list()
    
    # Load to Postgres
    load_sectors(df)
    
    print("\n" + "=" * 60)
    print("✅ Sector Mappings Loaded Successfully!")
    print("=" * 60)
    print("\n💡 Query example:")
    print("   SELECT * FROM sector_membership WHERE symbol = 'RELIANCE';")


if __name__ == "__main__":
    run()
