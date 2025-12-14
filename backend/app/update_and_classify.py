"""
Update company names and then populate sectors
"""
import csv
from pathlib import Path
from .database import Company, SessionLocal

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CSV_FILE = BASE_DIR / "sector" / "EQUITY_L.csv"

def update_company_names_from_csv():
    """Update company names from CSV"""
    db = SessionLocal()
    
    try:
        print("Loading company names from CSV...")
        
        with open(CSV_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            # Strip spaces from fieldnames
            reader.fieldnames = [name.strip() if name else name for name in reader.fieldnames]
            
            updated = 0
            not_found = 0
            
            for row in reader:
                symbol = row['SYMBOL'].strip()
                name = row['NAME OF COMPANY'].strip()
                
                company = db.query(Company).filter(Company.symbol == symbol).first()
                if company:
                    company.name = name
                    updated += 1
                    
                    if updated % 100 == 0:
                        print(f"  Updated {updated} company names...")
                        db.commit()
                else:
                    not_found += 1
            
            db.commit()
            
            print(f"\nCompany Name Update Summary:")
            print(f"  Updated: {updated}")
            print(f"  Not found in DB: {not_found}")
            
    except Exception as e:
        print(f"Error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    print("Step 1: Updating company names from CSV...")
    print("="*60)
    update_company_names_from_csv()
    
    print("\n" + "="*60)
    print("Step 2: Populating sectors using keywords...")
    print("="*60)
    
    from .populate_sectors_keywords import populate_sectors_from_keywords
    populate_sectors_from_keywords()
