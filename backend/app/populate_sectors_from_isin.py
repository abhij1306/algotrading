"""
Populate Sectors from NSE PDF using ISIN matching

This script extracts sector/industry data from the NSE PDF and matches
it with companies in the database using ISIN numbers from EQUITY_L.csv.

Usage:
    python -m app.populate_sectors_from_isin
"""

import csv
import PyPDF2
import re
from pathlib import Path
from sqlalchemy.orm import Session
from .database import Company, SessionLocal

# File paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent
CSV_FILE = BASE_DIR / "sector" / "EQUITY_L.csv"
PDF_FILE = BASE_DIR / "sector" / "nse-indices_industry-classification-structure-2023-07.pdf"

def load_isin_to_symbol_mapping():
    """Load ISIN to Symbol mapping from CSV"""
    isin_map = {}
    
    with open(CSV_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        # Strip spaces from fieldnames
        reader.fieldnames = [name.strip() if name else name for name in reader.fieldnames]
        
        for row in reader:
            isin = row['ISIN NUMBER'].strip()
            symbol = row['SYMBOL'].strip()
            isin_map[isin] = symbol
    
    print(f"Loaded {len(isin_map)} ISIN mappings from CSV")
    return isin_map

def extract_sector_data_from_pdf():
    """Extract sector and industry data from NSE PDF"""
    sector_data = {}
    
    try:
        with open(PDF_FILE, 'rb') as f:
            pdf_reader = PyPDF2.PdfReader(f)
            total_pages = len(pdf_reader.pages)
            
            print(f"Reading {total_pages} pages from PDF...")
            
            full_text = ""
            for page_num in range(total_pages):
                page = pdf_reader.pages[page_num]
                full_text += page.extract_text() + "\n"
            
            # Parse the extracted text for ISIN, Sector, Industry
            # The PDF likely has a table format with ISIN, Company Name, Sector, Industry
            lines = full_text.split('\n')
            
            current_sector = None
            current_industry = None
            
            for line in lines:
                line = line.strip()
                
                # Look for ISIN patterns (12 alphanumeric characters starting with INE)
                isin_match = re.search(r'(INE[A-Z0-9]{9})', line)
                
                # Try to detect sector/industry headers or keywords
                if 'SECTOR' in line.upper() or 'INDUSTRY' in line.upper():
                    # Extract sector/industry name
                    parts = line.split()
                    if len(parts) > 1:
                        current_sector = ' '.join(parts[1:])
                
                if isin_match:
                    isin = isin_match.group(1)
                    
                    # Try to extract sector/industry from the same line
                    # This is a heuristic and may need adjustment based on actual PDF format
                    if current_sector:
                        sector_data[isin] = {
                            'sector': current_sector,
                            'industry': current_industry or current_sector
                        }
            
            print(f"Extracted sector data for {len(sector_data)} ISINs from PDF")
            
    except Exception as e:
        print(f"Error reading PDF: {str(e)}")
        print("Note: You may need to install PyPDF2: pip install PyPDF2")
    
    return sector_data

def extract_sector_data_from_pdf_alt():
    """
    Alternative approach: Extract data as structured table
    Assumes PDF has tabular data with columns like: ISIN, Symbol, Sector, Industry
    """
    sector_data = {}
    
    try:
        import pdfplumber
        
        with pdfplumber.open(PDF_FILE) as pdf:
            print(f"Reading {len(pdf.pages)} pages with pdfplumber...")
            
            for page_num, page in enumerate(pdf.pages):
                # Extract tables from the page
                tables = page.extract_tables()
                
                for table in tables:
                    if not table:
                        continue
                    
                    # Try to find header row
                    headers = None
                    for row in table:
                        if any('ISIN' in str(cell).upper() for cell in row if cell):
                            headers = [str(cell).strip().upper() if cell else '' for cell in row]
                            break
                    
                    if not headers:
                        continue
                    
                    # Find column indices
                    isin_idx = next((i for i, h in enumerate(headers) if 'ISIN' in h), None)
                    sector_idx = next((i for i, h in enumerate(headers) if 'SECTOR' in h), None)
                    industry_idx = next((i for i, h in enumerate(headers) if 'INDUSTRY' in h), None)
                    
                    if isin_idx is None:
                        continue
                    
                    # Process data rows
                    for row in table:
                        if row == table[0]:  # Skip header
                            continue
                        
                        if len(row) > isin_idx and row[isin_idx]:
                            isin = str(row[isin_idx]).strip()
                            
                            # Validate ISIN format
                            if re.match(r'^INE[A-Z0-9]{9}$', isin):
                                sector = row[sector_idx].strip() if sector_idx and len(row) > sector_idx and row[sector_idx] else None
                                industry = row[industry_idx].strip() if industry_idx and len(row) > industry_idx and row[industry_idx] else None
                                
                                if sector:
                                    sector_data[isin] = {
                                        'sector': sector,
                                        'industry': industry or sector
                                    }
        
        print(f"Extracted sector data for {len(sector_data)} ISINs using pdfplumber")
        
    except ImportError:
        print("pdfplumber not installed. Install with: pip install pdfplumber")
        print("Falling back to PyPDF2 method...")
        return extract_sector_data_from_pdf()
    except Exception as e:
        print(f"Error with pdfplumber: {str(e)}")
        print("Falling back to PyPDF2 method...")
        return extract_sector_data_from_pdf()
    
    return sector_data

def populate_sectors_from_csv_pdf():
    """Main function to populate sectors using ISIN matching"""
    db = SessionLocal()
    
    try:
        # Step 1: Load ISIN mappings from CSV
        isin_to_symbol = load_isin_to_symbol_mapping()
        
        # Step 2: Extract sector data from PDF (try pdfplumber first)
        sector_data = extract_sector_data_from_pdf_alt()
        
        if not sector_data:
            print("No sector data extracted from PDF!")
            return
        
        # Step 3: Match and update companies
        updated_count = 0
        not_found_count = 0
        
        for isin, data in sector_data.items():
            symbol = isin_to_symbol.get(isin)
            
            if not symbol:
                not_found_count += 1
                continue
            
            # Find company in database
            company = db.query(Company).filter(Company.symbol == symbol).first()
            
            if company:
                company.sector = data['sector']
                company.industry = data['industry']
                updated_count += 1
                
                if updated_count % 100 == 0:
                    print(f"Updated {updated_count} companies...")
                    db.commit()
        
        # Final commit
        db.commit()
        
        # Summary
        print(f"\n" + "="*60)
        print(f"SECTOR POPULATION SUMMARY")
        print(f"="*60)
        print(f"Total ISINs in PDF: {len(sector_data)}")
        print(f"Successfully updated: {updated_count}")
        print(f"Not found in database: {not_found_count}")
        
        # Check overall statistics
        total = db.query(Company).count()
        with_sector = db.query(Company).filter(
            (Company.sector != None) & (Company.sector != '') & (Company.sector != 'Unknown')
        ).count()
        
        print(f"\nDatabase Statistics:")
        print(f"Total companies: {total}")
        print(f"With sector data: {with_sector} ({with_sector/total*100:.1f}%)")
        print(f"Missing sector: {total - with_sector} ({(total-with_sector)/total*100:.1f}%)")
        
    except Exception as e:
        print(f"Error: {str(e)}")
        db.rollback()
        raise
    finally:
        db.close()

if __name__ == "__main__":
    print("Starting ISIN-based sector population...")
    print("="*60)
    populate_sectors_from_csv_pdf()
