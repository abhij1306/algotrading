"""
Parse Corporate Actions from ca.csv and load into database
"""

import pandas as pd
from pathlib import Path
import sys
import os
from datetime import datetime
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from backend.app.database import SessionLocal
from sqlalchemy import text

print("=" * 70)
print("PARSING CORPORATE ACTIONS FROM ca.csv")
print("=" * 70)

# Read the file
ca_file = Path("nse_data/raw/corporate_actions/ca.csv")

if not ca_file.exists():
    print(f"❌ File not found: {ca_file}")
    sys.exit(1)

print(f"\n1. Reading {ca_file}")
print("-" * 70)

try:
    ca_df = pd.read_csv(ca_file)
    print(f"✅ Loaded {len(ca_df)} records")
    print(f"   Columns: {ca_df.columns.tolist()}")
    print(f"\nFirst few rows:")
    print(ca_df.head(3))
    
except Exception as e:
    print(f"❌ Failed to read file: {e}")
    sys.exit(1)

# Process data
print(f"\n2. Processing Data")
print("-" * 70)

try:
    # Standardize column names
    ca_df.columns = ca_df.columns.str.strip().str.upper()
    
    # Identify date columns and convert them
    date_cols = []
    for col in ca_df.columns:
        if 'DATE' in col or 'DT' in col:
            date_cols.append(col)
            try:
                ca_df[col] = pd.to_datetime(ca_df[col], errors='coerce')
            except:
                pass
    
    print(f"✅ Identified date columns: {date_cols}")
    
    # Filter relevant actions
    if 'PURPOSE' in ca_df.columns:
        relevant = ca_df[
            ca_df['PURPOSE'].str.contains('split|bonus|dividend', case=False, na=False)
        ].copy()
        print(f"✅ Filtered to {len(relevant)} relevant actions (split/bonus/dividend)")
    else:
        relevant = ca_df
        print(f"⚠️  No PURPOSE column - using all {len(relevant)} records")
    
except Exception as e:
    print(f"❌ Processing failed: {e}")
    relevant = ca_df

# Load into database
print(f"\n3. Loading into Database")
print("-" * 70)

db = SessionLocal()

try:
    # Clear existing
    db.execute(text("DELETE FROM corporate_actions"))
    db.commit()
    
    inserted = 0
    
    # Map columns dynamically
    col_map = {}
    for col in relevant.columns:
        col_upper = col.upper()
        if 'SYMBOL' in col_upper:
            col_map['symbol'] = col
        elif 'COMPANY' in col_upper or 'NAME' in col_upper:
            col_map['company'] = col
        elif 'EX' in col_upper and 'DATE' in col_upper:
            col_map['ex_date'] = col
        elif 'PURPOSE' in col_upper or 'ACTION' in col_upper:
            col_map['purpose'] = col
        elif 'RECORD' in col_upper and 'DATE' in col_upper:
            col_map['record_date'] = col
        elif 'BC' in col_upper and 'START' in col_upper:
            col_map['bc_start_date'] = col
        elif 'BC' in col_upper and 'END' in col_upper:
            col_map['bc_end_date'] = col
    
    print(f"Column mapping: {col_map}")
    
    for _, row in relevant.iterrows():
        try:
            insert_stmt = text("""
                INSERT INTO corporate_actions 
                (symbol, company, ex_date, purpose, record_date, bc_start_date, bc_end_date)
                VALUES (:symbol, :company, :ex_date, :purpose, :record_date, :bc_start_date, :bc_end_date)
            """)
            
            db.execute(insert_stmt, {
                'symbol': str(row.get(col_map.get('symbol', ''), '')).strip() if 'symbol' in col_map else '',
                'company': str(row.get(col_map.get('company', ''), '')).strip() if 'company' in col_map else '',
                'ex_date': row.get(col_map.get('ex_date', '')) if 'ex_date' in col_map else None,
                'purpose': str(row.get(col_map.get('purpose', ''), '')).strip() if 'purpose' in col_map else '',
                'record_date': row.get(col_map.get('record_date', '')) if 'record_date' in col_map else None,
                'bc_start_date': row.get(col_map.get('bc_start_date', '')) if 'bc_start_date' in col_map else None,
                'bc_end_date': row.get(col_map.get('bc_end_date', '')) if 'bc_end_date' in col_map else None,
            })
            
            inserted += 1
            
        except Exception as e:
            continue
    
    db.commit()
    print(f"✅ Inserted {inserted} corporate action records")
    
except Exception as e:
    print(f"❌ Database load failed: {e}")
    db.rollback()
finally:
    db.close()

# Verify
print(f"\n4. Verification")
print("-" * 70)

db = SessionLocal()

try:
    total = db.execute(text("SELECT COUNT(*) FROM corporate_actions")).scalar()
    print(f"✅ Total in database: {total}")
    
    # Breakdown by type
    splits = db.execute(text("SELECT COUNT(*) FROM corporate_actions WHERE purpose LIKE '%split%'")).scalar()
    bonuses = db.execute(text("SELECT COUNT(*) FROM corporate_actions WHERE purpose LIKE '%bonus%'")).scalar()
    dividends = db.execute(text("SELECT COUNT(*) FROM corporate_actions WHERE purpose LIKE '%dividend%'")).scalar()
    
    print(f"   - Splits: {splits}")
    print(f"   - Bonuses: {bonuses}")
    print(f"   - Dividends: {dividends}")
    
    # Samples
    samples = db.execute(text("""
        SELECT symbol, ex_date, purpose 
        FROM corporate_actions 
        ORDER BY ex_date DESC 
        LIMIT 10
    """)).fetchall()
    
    print(f"\nRecent corporate actions:")
    for symbol, ex_date, purpose in samples:
        print(f"   - {symbol}: {purpose} (Ex-date: {ex_date})")
    
except Exception as e:
    print(f"❌ Verification failed: {e}")
finally:
    db.close()

print("\n" + "=" * 70)
print(f"COMPLETE! Loaded {inserted} corporate actions")
print("=" * 70)
