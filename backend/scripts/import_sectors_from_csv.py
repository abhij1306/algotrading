"""
Import sector data from Nifty 500 CSV file and update the database
"""
import pandas as pd
from app.database import SessionLocal, Company

def import_sectors_from_csv():
    """Import sector data from ind_nifty500list.csv"""
    
    # Read CSV file from parent directory
    import os
    csv_path = os.path.join(os.path.dirname(__file__), '..', 'ind_nifty500list.csv')
    
    print(f"Reading CSV file from: {csv_path}")
    df = pd.read_csv(csv_path)
    
    print(f"Found {len(df)} companies in CSV")
    print(f"Columns: {df.columns.tolist()}")
    
    # Display first few rows to understand structure
    print("\nFirst 5 rows:")
    print(df.head())
    
    # Initialize database session
    db = SessionLocal()
    
    stats = {
        'total': len(df),
        'updated': 0,
        'not_found': 0,
        'sectors': set()
    }
    
    try:
        for idx, row in df.iterrows():
            # CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
            symbol = str(row.get('Symbol', '')).strip().upper()
            industry = str(row.get('Industry', '')).strip()
            
            if not symbol or symbol == 'NAN' or pd.isna(symbol):
                continue
            
            if not industry or industry == 'NAN' or pd.isna(industry):
                continue
            
            # Find company in database
            company = db.query(Company).filter(Company.symbol == symbol).first()
            
            if company:
                # Update sector and industry (using Industry column for both)
                company.sector = industry
                company.industry = industry
                stats['updated'] += 1
                stats['sectors'].add(industry)
                
                if stats['updated'] % 50 == 0:
                    print(f"Updated {stats['updated']} companies...")
            else:
                stats['not_found'] += 1
                if stats['not_found'] <= 10:  # Show first 10 not found
                    print(f"⚠️  Symbol not found in database: {symbol}")
        
        # Commit all changes
        db.commit()
        
        print("\n" + "="*50)
        print("Import Summary:")
        print("="*50)
        print(f"Total companies in CSV: {stats['total']}")
        print(f"✅ Updated in database: {stats['updated']}")
        print(f"⚠️  Not found in database: {stats['not_found']}")
        print(f"📊 Unique sectors: {len(stats['sectors'])}")
        print("\nSectors found:")
        for sector in sorted(stats['sectors']):
            if sector and sector != 'nan':
                count = db.query(Company).filter(Company.sector == sector).count()
                print(f"  - {sector}: {count} companies")
        
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    import_sectors_from_csv()
