"""
Quick PDF analyzer to understand the structure of NSE sector PDF
"""
import pdfplumber
from pathlib import Path

PDF_FILE = Path(__file__).resolve().parent.parent.parent / "sector" / "nse-indices_industry-classification-structure-2023-07.pdf"

def analyze_pdf_structure():
    with pdfplumber.open(PDF_FILE) as pdf:
        print(f"Total pages: {len(pdf.pages)}\n")
        
        # Analyze first few pages
        for page_num in range(min(3, len(pdf.pages))):
            page = pdf.pages[page_num]
            print(f"=" * 60)
            print(f"PAGE {page_num + 1}")
            print(f"=" * 60)
            
            # Extract text
            text = page.extract_text()
            print("\n--- TEXT SAMPLE (first 500 chars) ---")
            print(text[:500] if text else "No text")
            
            # Extract tables
            tables = page.extract_tables()
            print(f"\n--- TABLES FOUND: {len(tables)} ---")
            
            if tables:
                for i, table in enumerate(tables):
                    print(f"\nTable {i+1}:")
                    print(f"Rows: {len(table)}, Columns: {len(table[0]) if table else 0}")
                    if table:
                        print("First row (headers):", table[0])
                        if len(table) > 1:
                            print("Second row (sample):", table[1])
            
            print("\n")

if __name__ == "__main__":
    analyze_pdf_structure()
