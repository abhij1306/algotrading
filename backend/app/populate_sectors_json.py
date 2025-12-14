"""
Populate sectors from user-provided JSON data
"""
import json
from .database import Company, SessionLocal

# User-provided sector data
SECTOR_DATA = {
  "Financial Services": [
    { "symbol": "360ONE", "isin": "INE466L01038", "company": "360 ONE WAM Ltd." },
    { "symbol": "AAVAS", "isin": "INE216P01012", "company": "Aavas Financiers Ltd." },
    { "symbol": "ABCAPITAL", "isin": "INE674K01013", "company": "Aditya Birla Capital Ltd." },
    { "symbol": "AUBANK", "isin": "INE949L01017", "company": "AU Small Finance Bank Ltd." },
    { "symbol": "ICICIBANK", "isin": "INE090A01021", "company": "ICICI Bank Ltd." },
    { "symbol": "ICICIGI", "isin": "INE765G01017", "company": "ICICI Lombard Gen. Ins. Co. Ltd." },
    { "symbol": "ICICIPRULI", "isin": "INE726G01019", "company": "ICICI Prudential Life Ins. Co. Ltd." }
  ],
  "Healthcare": [
    { "symbol": "ABBOTINDIA", "isin": "INE358A01014", "company": "Abbott India Ltd." },
    { "symbol": "PPLPHARMA", "isin": "INE0DK501011", "company": "Piramal Pharma Ltd." },
    { "symbol": "POLYMED", "isin": "INE205C01021", "company": "Poly Medicure Ltd." }
  ],
  "Capital Goods": [
    { "symbol": "ABB", "isin": "INE117A01022", "company": "ABB India Ltd." },
    { "symbol": "AIAENG", "isin": "INE212H01026", "company": "AIA Engineering Ltd." },
    { "symbol": "ACE", "isin": "INE731H01025", "company": "Action Construction Equipment Ltd." },
    { "symbol": "HONAUT", "isin": "INE671A01010", "company": "Honeywell Automation India Ltd." },
    { "symbol": "POWERINDIA", "isin": "INE07Y701011", "company": "Hitachi Energy India Ltd." },
    { "symbol": "POLYCAB", "isin": "INE455K01017", "company": "Polycab India Ltd." }
  ],
  "Metals & Mining": [
    { "symbol": "ADANIENT", "isin": "INE423A01024", "company": "Adani Enterprises Ltd." },
    { "symbol": "HINDALCO", "isin": "INE038A01020", "company": "Hindalco Industries Ltd." },
    { "symbol": "HINDCOPPER", "isin": "INE531E01026", "company": "Hindustan Copper Ltd." },
    { "symbol": "HINDZINC", "isin": "INE267A01025", "company": "Hindustan Zinc Ltd." }
  ],
  "Construction Materials": [
    { "symbol": "ACC", "isin": "INE012A01025", "company": "ACC Ltd." }
  ],
  "Consumer Durables": [
    { "symbol": "HAVELLS", "isin": "INE176B01034", "company": "Havells India Ltd." }
  ],
  "Power": [
    { "symbol": "ADANIENSOL", "isin": "INE931S01010", "company": "Adani Energy Solutions Ltd." },
    { "symbol": "ADANIGREEN", "isin": "INE364U01010", "company": "Adani Green Energy Ltd." },
    { "symbol": "ADANIPOWER", "isin": "INE814H01011", "company": "Adani Power Ltd." }
  ],
  "Oil Gas & Consumable Fuels": [
    { "symbol": "ATGL", "isin": "INE399L01023", "company": "Adani Total Gas Ltd." },
    { "symbol": "HINDPETRO", "isin": "INE094A01015", "company": "Hindustan Petroleum Corp. Ltd." },
    { "symbol": "PETRONET", "isin": "INE347G01014", "company": "Petronet LNG Ltd." }
  ],
  "Fast Moving Consumer Goods": [
    { "symbol": "AWL", "isin": "INE699H01024", "company": "Adani Wilmar Ltd." },
    { "symbol": "HINDUNILVR", "isin": "INE030A01027", "company": "Hindustan Unilever Ltd." },
    { "symbol": "HONASA", "isin": "INE0J5401028", "company": "Honasa Consumer Ltd." },
    { "symbol": "PATANJALI", "isin": "INE619A01035", "company": "Patanjali Foods Ltd." }
  ],
  "Services": [
    { "symbol": "ADANIPORTS", "isin": "INE742F01042", "company": "Adani Ports & SEZ Ltd." }
  ],
  "Telecommunication": [
    { "symbol": "HFCL", "isin": "INE548A01028", "company": "HFCL Ltd." }
  ],
  "Automobile and Auto Components": [
    { "symbol": "HEROMOTOCO", "isin": "INE158A01026", "company": "Hero MotoCorp Ltd." },
    { "symbol": "OLECTRA", "isin": "INE260D01016", "company": "Olectra Greentech Ltd." }
  ],
  "Textiles": [
    { "symbol": "PAGEIND", "isin": "INE761H01022", "company": "Page Industries Ltd." }
  ],
  "Construction": [
    { "symbol": "PNCINFRA", "isin": "INE195J01029", "company": "PNC Infratech Ltd." }
  ],
  "Media Entertainment & Publication": [
    { "symbol": "PVRINOX", "isin": "INE191H01014", "company": "PVR INOX Ltd." }
  ]
}

def populate_sectors_from_json():
    """Populate sectors from user-provided JSON data"""
    db = SessionLocal()
    
    try:
        updated_count = 0
        not_found = []
        
        print("Populating sectors from JSON data...")
        print("="*60)
        
        for sector_name, companies in SECTOR_DATA.items():
            print(f"\n{sector_name}: {len(companies)} companies")
            
            for company_data in companies:
                symbol = company_data['symbol']
                
                # Find company in database
                company = db.query(Company).filter(Company.symbol == symbol).first()
                
                if company:
                    company.sector = sector_name
                    company.industry = sector_name  # Using sector name as industry for now
                    updated_count += 1
                    print(f"  ✅ {symbol}")
                else:
                    not_found.append(symbol)
                    print(f"  ❌ {symbol} - Not found in database")
        
        db.commit()
        
        # Summary
        print(f"\n" + "="*60)
        print(f"SECTOR UPDATE SUMMARY")
        print(f"="*60)
        print(f"Successfully updated: {updated_count}")
        print(f"Not found in DB: {len(not_found)}")
        
        if not_found:
            print(f"\nMissing symbols: {', '.join(not_found)}")
        
        # Show statistics
        from sqlalchemy import func
        total = db.query(Company).count()
        with_sector = db.query(Company).filter(
            (Company.sector != None) & (Company.sector != '')
        ).count()
        
        print(f"\nOverall Statistics:")
        print(f"Total companies in DB: {total}")
        print(f"Companies with sector: {with_sector} ({with_sector/total*100:.1f}%)")
        
        # Show sector breakdown
        print(f"\nSector Distribution:")
        sectors = db.query(
            Company.sector, 
            func.count(Company.id)
        ).filter(
            (Company.sector != None) & (Company.sector != '')
        ).group_by(Company.sector).order_by(func.count(Company.id).desc()).all()
        
        for sector, count in sectors:
            print(f"  {sector}: {count}")
        
    except Exception as e:
        print(f"Error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    populate_sectors_from_json()
