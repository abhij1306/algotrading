"""
Stage 5: Sector + Index Membership Mapping
Downloads index constituent lists from NSE and maps sectors to symbols
"""

import requests
import pandas as pd
from pathlib import Path
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from backend.app.database import SessionLocal, Company

print("=" * 70)
print("STAGE 5: SECTOR + INDEX MEMBERSHIP MAPPING")
print("=" * 70)

# NSE Index URLs
INDEX_URLS = {
    'nifty50': 'https://archives.nseindia.com/content/indices/ind_nifty50list.csv',
    'nifty500': 'https://archives.nseindia.com/content/indices/ind_nifty500list.csv',
    'niftybank': 'https://archives.nseindia.com/content/indices/ind_niftybanklist.csv',
    'niftyit': 'https://archives.nseindia.com/content/indices/ind_niftyitlist.csv',
}

# Headers to mimic browser
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

# Download directory
download_dir = Path("nse_data/metadata/index_constituents")
download_dir.mkdir(parents=True, exist_ok=True)

# ============================================================
# 1. DOWNLOAD INDEX CONSTITUENT LISTS
# ============================================================
print("\n1. Downloading Index Constituent Lists")
print("-" * 70)

index_data = {}

for index_name, url in INDEX_URLS.items():
    try:
        print(f"Downloading {index_name}...")
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        
        # Save to file
        file_path = download_dir / f"{index_name}.csv"
        with open(file_path, 'wb') as f:
            f.write(response.content)
        
        # Read CSV
        df = pd.read_csv(file_path)
        index_data[index_name] = df
        
        print(f"✅ {index_name}: {len(df)} stocks")
        
    except Exception as e:
        print(f"❌ Failed to download {index_name}: {e}")
        continue

# ============================================================
# 2. EXTRACT SECTOR INFORMATION
# ============================================================
print("\n2. Extracting Sector Information")
print("-" * 70)

# Nifty 500 has the most comprehensive sector data
if 'nifty500' in index_data:
    nifty500_df = index_data['nifty500']
    
    # Check column names
    print(f"Columns: {nifty500_df.columns.tolist()}")
    
    # Map SYMBOL → INDUSTRY (sector)
    sector_map = {}
    
    # Check if Industry column exists, try different possible column names
    industry_col = None
    for col in ['Industry', 'INDUSTRY', 'Sector', 'SECTOR']:
        if col in nifty500_df.columns:
            industry_col = col
            break
    
    if industry_col:
        for _, row in nifty500_df.iterrows():
            symbol = str(row['Symbol']).strip().upper()
            industry = str(row[industry_col]).strip()
            sector_map[symbol] = industry
        
        print(f"✅ Extracted sectors for {len(sector_map)} symbols using column '{industry_col}'")
    else:
        print(f"⚠️  No industry/sector column found. Available columns: {nifty500_df.columns.tolist()}")
        sector_map = {}

else:
    print("⚠️  Nifty 500 data not available, using limited sector info")
    sector_map = {}

# ============================================================
# 3. CREATE INDEX MEMBERSHIP MAP
# ============================================================
print("\n3. Creating Index Membership Map")
print("-" * 70)

index_membership = {}

for index_name, df in index_data.items():
    for _, row in df.iterrows():
        symbol = str(row['Symbol']).strip().upper()
        
        if symbol not in index_membership:
            index_membership[symbol] = []
        
        index_membership[symbol].append(index_name.upper())

print(f"✅ Mapped index membership for {len(index_membership)} symbols")

# ============================================================
# 4. UPDATE SYMBOL_SECTOR_MAP.CSV
# ============================================================
print("\n4. Updating symbol_sector_map.csv")
print("-" * 70)

# Combine all data
all_symbols = set(sector_map.keys()) | set(index_membership.keys())

map_data = []
for symbol in sorted(all_symbols):
    sector = sector_map.get(symbol, 'Unknown')
    indices = ';'.join(sorted(index_membership.get(symbol, [])))
    
    map_data.append({
        'SYMBOL': symbol,
        'SECTOR': sector,
        'INDEX_MEMBERSHIP': indices
    })

map_df = pd.DataFrame(map_data)

# Save to metadata
output_file = Path("nse_data/metadata/symbol_sector_map.csv")
map_df.to_csv(output_file, index=False)

print(f"✅ Saved {len(map_df)} symbols to {output_file}")
print(f"   - With sectors: {len(map_df[map_df['SECTOR'] != 'Unknown'])}")
print(f"   - With index membership: {len(map_df[map_df['INDEX_MEMBERSHIP'] != ''])}")

# ============================================================
# 5. UPDATE DATABASE
# ============================================================
print("\n5. Updating Database with Sector Information")
print("-" * 70)

db = SessionLocal()

try:
    updated_count = 0
    not_found_count = 0
    
    for _, row in map_df.iterrows():
        symbol = row['SYMBOL']
        sector = row['SECTOR']
        
        # Find company in database
        company = db.query(Company).filter(Company.symbol == symbol).first()
        
        if company:
            # Update sector if we have valid data
            if sector and sector != 'Unknown':
                company.sector = sector
                updated_count += 1
        else:
            not_found_count += 1
    
    db.commit()
    
    print(f"✅ Updated {updated_count} companies with sector data")
    if not_found_count > 0:
        print(f"ℹ️  {not_found_count} symbols not found in database")
    
except Exception as e:
    print(f"❌ Database update failed: {e}")
    db.rollback()
finally:
    db.close()

# ============================================================
# 6. VERIFICATION
# ============================================================
print("\n6. Verification")
print("-" * 70)

db = SessionLocal()

try:
    # Count companies with sectors
    total_companies = db.query(Company).filter(Company.is_active == True).count()
    with_sectors = db.query(Company).filter(
        Company.is_active == True,
        Company.sector.isnot(None),
        Company.sector != ''
    ).count()
    
    print(f"✅ Active companies: {total_companies}")
    print(f"✅ With sector data: {with_sectors} ({with_sectors/total_companies*100:.1f}%)")
    
    # Sample sectors
    sectors = db.query(Company.sector).filter(
        Company.sector.isnot(None),
        Company.sector != ''
    ).distinct().limit(10).all()
    
    print(f"\nSample sectors:")
    for sector in sectors:
        count = db.query(Company).filter(Company.sector == sector[0]).count()
        print(f"   - {sector[0]}: {count} companies")
    
except Exception as e:
    print(f"❌ Verification failed: {e}")
finally:
    db.close()

# ============================================================
# SUMMARY
# ============================================================
print("\n" + "=" * 70)
print("STAGE 5 COMPLETE!")
print("=" * 70)
print(f"\n✅ Downloaded {len(index_data)} index constituent lists")
print(f"✅ Mapped sectors for {len(sector_map)} symbols")
print(f"✅ Updated database with sector information")
print(f"✅ Created symbol_sector_map.csv with {len(map_df)} symbols")
print("\n📁 Files created:")
print(f"   - {output_file}")
for index_name in index_data.keys():
    print(f"   - {download_dir / index_name}.csv")
print("=" * 70)
