"""
Corporate Actions Data Loader

Downloads NSE corporate actions CSV and loads it into Postgres.
Creates the necessary table schema if it doesn't exist.
"""

import os
import pandas as pd
import psycopg2
import requests
from io import StringIO

CA_URL = "https://archives.nseindia.com/content/corporate_actions/ca.csv"
CA_LOCAL = "nse_data/raw/corporate_actions/ca.csv"

PG_CONN = {
    "host": "localhost",
    "dbname": "algotrading",
    "user": "postgres",
    "password": "postgres",
    "port": 5432
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv"
}


def download_ca_file():
    """Download corporate actions CSV from NSE"""
    print("Downloading corporate actions data from NSE...")
    
    os.makedirs(os.path.dirname(CA_LOCAL), exist_ok=True)
    
    resp = requests.get(CA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    
    with open(CA_LOCAL, 'w', encoding='utf-8') as f:
        f.write(resp.text)
    
    print(f"✓ Downloaded to {CA_LOCAL}")
    return CA_LOCAL


def create_table(conn):
    """Create corporate_actions table if it doesn't exist"""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS corporate_actions (
            symbol TEXT,
            ex_date DATE,
            purpose TEXT
        );
    """)
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ca_symbol_date
        ON corporate_actions(symbol, ex_date);
    """)
    
    conn.commit()
    cursor.close()
    print("✓ Table schema created/verified")


def load_to_postgres(csv_path):
    """Load corporate actions CSV into Postgres"""
    print("\nLoading data into Postgres...")
    
    # Read CSV
    df = pd.read_csv(csv_path)
    
    # Normalize column names (NSE CSV may have different casing)
    df.columns = df.columns.str.strip().str.lower()
    
    # Select and rename relevant columns
    # Adjust these column names based on actual NSE CSV structure
    col_map = {
        'symbol': 'symbol',
        'ex_date': 'ex_date',
        'purpose': 'purpose'
    }
    
    # Try to find the right columns
    for old_col in list(df.columns):
        if 'symbol' in old_col.lower():
            col_map['symbol'] = old_col
        elif 'ex' in old_col.lower() and 'date' in old_col.lower():
            col_map['ex_date'] = old_col
        elif 'purpose' in old_col.lower() or 'subject' in old_col.lower():
            col_map['purpose'] = old_col
    
    df = df[[col_map['symbol'], col_map['ex_date'], col_map['purpose']]]
    df.columns = ['symbol', 'ex_date', 'purpose']
    
    # Parse dates
    df['ex_date'] = pd.to_datetime(df['ex_date'], errors='coerce', dayfirst=True)
    df = df.dropna(subset=['ex_date'])
    
    # Filter for relevant actions (splits, bonuses)
    df = df[
        df['purpose'].str.lower().str.contains('split|bonus', na=False)
    ]
    
    print(f"Found {len(df):,} relevant corporate actions (splits/bonuses)")
    
    # Connect and load
    conn = psycopg2.connect(**PG_CONN)
    create_table(conn)
    
    # Clear existing data
    cursor = conn.cursor()
    cursor.execute("DELETE FROM corporate_actions")
    conn.commit()
    
    # Insert new data
    from psycopg2.extras import execute_values
    
    values = [
        (row['symbol'], row['ex_date'].date(), row['purpose'])
        for _, row in df.iterrows()
    ]
    
    execute_values(
        cursor,
        "INSERT INTO corporate_actions (symbol, ex_date, purpose) VALUES %s",
        values
    )
    
    conn.commit()
    cursor.close()
    conn.close()
    
    print(f"✓ Loaded {len(values):,} records into Postgres")


def run():
    """Main pipeline"""
    print("=" * 60)
    print("NSE Corporate Actions Loader")
    print("=" * 60)
    
    # Download
    csv_path = download_ca_file()
    
    # Load to Postgres
    load_to_postgres(csv_path)
    
    print("\n" + "=" * 60)
    print("✅ Corporate Actions Loaded Successfully!")
    print("=" * 60)


if __name__ == "__main__":
    run()
