"""
Populate sector and industry data for all companies using predefined mapping
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

from app.database import SessionLocal, Company

# Sector mapping for F&O stocks
SECTOR_MAPPING = {
    "Information Technology": [
        "TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", 
        "MPHASIS", "COFORGE", "PERSISTENT"
    ],
    "Banking": [
        "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
        "INDUSINDBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB"
    ],
    "Financial Services": [
        "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
        "CHOLAFIN", "SHRIRAMFIN", "MUTHOOTFIN", "PFC", "RECLTD"
    ],
    "Oil & Gas": [
        "RELIANCE", "ONGC", "IOC", "BPCL", "HPCL", "GAIL", "OIL"
    ],
    "Metals": [
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "JINDALSTEL",
        "SAIL", "NALCO", "NMDC", "COALINDIA"
    ],
    "Automobile": [
        "MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT",
        "TVSMOTOR", "ASHOKLEY", "HEROMOTOCO", "ESCORTS"
    ],
    "Pharmaceuticals": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN",
        "AUROPHARMA", "ALKEM", "TORNTPHARM", "BIOCON"
    ],
    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM",
        "DABUR", "GODREJCP", "MARICO", "COLPAL"
    ],
    "Power": [
        "NTPC", "POWERGRID", "TATAPOWER", "ADANIPOWER", "NHPC",
        "SJVN", "TORNTPOWER"
    ],
    "Infrastructure & Capital Goods": [
        "LT", "SIEMENS", "ABB", "ADANIENT", "ADANIPORTS",
        "BHEL", "BEL", "HAL", "IRCTC"
    ],
    "Chemicals": [
        "UPL", "PIDILITIND", "SRF", "AARTIIND", "DEEPAKNTR",
        "GNFC", "BALRAMCHIN", "TATACHEM"
    ],
    "Telecom": [
        "BHARTIARTL", "IDEA"
    ],
    "Cement": [
        "ULTRACEMCO", "SHREECEM", "AMBUJACEM", "ACC", "DALBHARAT", "RAMCOCEM"
    ],
    "Consumer Durables": [
        "TITAN", "VOLTAS", "WHIRLPOOL", "BLUESTARCO", "CROMPTON"
    ],
    "Media & Entertainment": [
        "ZEEL", "SUNTV", "PVRINOX"
    ],
    "Real Estate": [
        "DLF", "OBEROIRLTY", "GODREJPROP", "PRESTIGE", "BRIGADE"
    ]
}

def populate_sectors():
    """
    Populate sector and industry data for all companies based on predefined mapping
    """
    db = SessionLocal()
    
    try:
        # Create reverse mapping: symbol -> sector
        symbol_to_sector = {}
        for sector, symbols in SECTOR_MAPPING.items():
            for symbol in symbols:
                symbol_to_sector[symbol] = sector
        
        print("🎯 Populating Sector Data for Companies")
        print("=" * 70)
        
        total_updated = 0
        total_not_found = 0
        
        # Get all companies
        all_companies = db.query(Company).all()
        print(f"📊 Total companies in database: {len(all_companies)}")
        print()
        
        # Update sectors for mapped symbols
        for symbol, sector in symbol_to_sector.items():
            company = db.query(Company).filter(Company.symbol == symbol).first()
            
            if company:
                company.sector = sector
                company.industry = sector  # Using sector as industry for now
                total_updated += 1
                print(f"✅ {symbol:15} -> {sector}")
            else:
                total_not_found += 1
                print(f"⚠️  {symbol:15} -> Not found in database")
        
        db.commit()
        
        # Statistics
        print("\n" + "=" * 70)
        print(f"✅ Successfully updated: {total_updated} companies")
        print(f"⚠️  Symbols not found: {total_not_found}")
        
        # Show sector distribution
        print("\n📊 Sector Distribution:")
        print("-" * 70)
        for sector, symbols in SECTOR_MAPPING.items():
            count = db.query(Company).filter(Company.sector == sector).count()
            print(f"{sector:35} : {count:3} companies")
        
        # Show companies without sector
        no_sector_count = db.query(Company).filter(
            (Company.sector == None) | (Company.sector == '')
        ).count()
        print(f"\n⚠️  Companies without sector: {no_sector_count}")
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        db.rollback()
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    print("🚀 Starting Sector Population")
    print()
    populate_sectors()
    print("\n✅ Sector population completed!")
