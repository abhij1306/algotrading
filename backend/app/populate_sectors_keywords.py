"""
PopulateSectors using keyword-based classification

Since the PDF doesn't have direct ISIN-to-sector mappings, we'll use
intelligent keyword matching based on company names to assign sectors.
"""

from sqlalchemy.orm import Session
from .database import Company, SessionLocal
import re

# Sector classification based on company name keywords
SECTOR_KEYWORDS = {
    'Bank': ['Bank', 'Banking'],
    'Financial Services': ['Finance', 'Financial', 'Capital', 'Securities', 'Investment', 'Insurance', 'Asset Management', 'AMC', 'Finserv'],
    'IT Services': ['Infotech', 'Information Technology', 'Software', 'Technologies', 'Tech', 'Systems', 'IT'],
    'Pharmaceuticals': ['Pharma', 'Pharmaceutical', 'Drug', 'Healthcare', 'Health Care', 'Medical', 'Lifesciences', 'Life Sciences', 'Bio'],
    'Automobiles': ['Auto', 'Automobile', 'Motors', 'Tyres', 'Tyre'],
    'Cement': ['Cement'],
    'Metals & Mining': ['Steel', 'Metal', 'Alloy', 'Mining', 'Iron', 'Aluminium', 'Aluminum', 'Copper'],
    'Oil & Gas': ['Petroleum', 'Oil', 'Gas', 'Energy', 'Power'],
    'Telecommunications': ['Telecom', 'Telecommunication', 'Airtel', 'Network'],
    'FMCG': ['Consumer', 'Foods', 'Food', 'Beverages', 'Beverage', 'FMCG', 'Products'],
    'Textiles': ['Textile', 'Fabric', 'Garment', 'Apparel', 'Fashion', 'Cloth'],
    'Chemicals': ['Chemical', 'Dye'],
    'Construction': ['Construction', 'Infrastructure', 'Infra', 'Buildcon', 'Builder', 'Engineering', 'Projects'],
    'Real Estate': ['Realty', 'Real Estate', 'Properties', 'Housing', 'Developers'],
    'Media & Entertainment': ['Media', 'Entertainment', 'Broadcasting', 'Films', 'Television', 'TV'],
    'Logistics': ['Logistics', 'Transport', 'Shipping', 'Cargo', 'Delivery'],
    'Retail': ['Retail', 'Mart', 'Supermarket'],
    'Hotels & Tourism': ['Hotels', 'Hotel', 'Hospitality', 'Resorts', 'Tourism'],
    'Agriculture': ['Agro', 'Agriculture', 'Fertilizer', 'Fertiliser', 'Seeds', 'Crop'],
    'Paper & Pulp': ['Paper', 'Pulp'],
    'Power': ['Power', 'Electricity', 'Electric'],
    'Defence': ['Defence', 'Defense', 'Aerospace', 'Aeronautics'],
    'Education': ['Education', 'Edutech', 'Learning', 'School'],
    'Paints': ['Paint', 'Coating'],
    'Plastics': ['Plastic', 'Polymer', 'Polypropylene'],
    'Glass': ['Glass'],
    'Cables': ['Cable', 'Wire'],
    'Pipes': ['Pipe', 'Tube'],
    'Industrial Manufacturing': ['Industries', 'Industrial', 'Manufacturing', 'Equipment', 'Machinery'],
}

def classify_sector_by_name(company_name: str) -> tuple:
    """
    Classify sector based on company name keywords
    
    Returns:
        tuple: (sector, industry) or (None, None) if no match
    """
    if not company_name:
        return (None, None)
    
    name_upper = company_name.upper()
    
    for sector, keywords in SECTOR_KEYWORDS.items():
        for keyword in keywords:
            if keyword.upper() in name_upper:
                return (sector, keyword)
    
    return (None, None)

def populate_sectors_from_keywords():
    """Populate sectors using keyword-based classification"""
    db = SessionLocal()
    
    try:
        # Get all companies
        companies = db.query(Company).all()
        total = len(companies)
        
        updated_count = 0
        skipped_count = 0
        unknown_count = 0
        
        print(f"Processing {total} companies...")
        print("="*60)
        
        for i, company in enumerate(companies, 1):
            # Skip if already has sector
            if company.sector and company.sector != '' and company.sector != 'Unknown':
                skipped_count += 1
                continue
            
            # Classify using keywords
            sector, industry = classify_sector_by_name(company.name)
            
            if sector:
                company.sector = sector
                company.industry = industry
                updated_count += 1
                
                if updated_count % 100 == 0:
                    print(f"  Updated {updated_count} companies...")
                    db.commit()
            else:
                # Mark as Unknown for now
                company.sector = 'Unknown'
                company.industry = 'Unknown'
                unknown_count += 1
        
        # Final commit
        db.commit()
        
        # Summary
        print(f"\n" + "="*60)
        print(f"SECTOR POPULATION SUMMARY (KEYWORD-BASED)")
        print(f"="*60)
        print(f"Total companies: {total}")
        print(f"Already had sector: {skipped_count}")
        print(f"Successfully classified: {updated_count}")
        print(f"Marked as Unknown: {unknown_count}")
        print(f"Success rate: {(updated_count/(total-skipped_count)*100):.1f}%")
        
        # Show sector distribution
        print(f"\nSector Distribution:")
        from sqlalchemy import func
        sector_counts = db.query(
            Company.sector,
            func.count(Company.id).label('count')
        ).filter(
            (Company.sector != None) & (Company.sector != '')
        ).group_by(Company.sector).order_by(func.count(Company.id).desc()).all()
        
        for sector, count in sector_counts:
            print(f"  {sector}: {count}")
        
    except Exception as e:
        print(f"Error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    print("Starting keyword-based sector population...")
    print("This method uses company names to intelligently classify sectors")
    print("="*60)
    populate_sectors_from_keywords()
