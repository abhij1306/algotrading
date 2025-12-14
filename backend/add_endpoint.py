"""
Quick script to add /api/sectors/list endpoint
"""
import sys
sys.path.insert(0, 'c:/AlgoTrading/backend')

# Read the main.py file
with open('c:/AlgoTrading/backend/app/main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find where to insert the new endpoint (after sector-stats endpoint)
insert_marker = '    finally:\n        db.close()\n\n@app.post("/api/admin/populate-sectors")'
new_endpoint = '''    finally:
        db.close()

@app.get("/api/sectors/list")
async def get_sectors_list():
    """Get list of available sectors for filtering"""
    db = SessionLocal()
    
    try:
        from sqlalchemy import distinct
        
        # Get distinct sectors that are not null or empty
        sectors = db.query(distinct(Company.sector)).filter(
            (Company.sector != None) & 
            (Company.sector != '') & 
            (Company.sector != 'Unknown')
        ).order_by(Company.sector).all()
        
        # Extract sector names from tuples
        sector_list = [s[0] for s in sectors]
        
        return {'sectors': sector_list}
    finally:
        db.close()

@app.post("/api/admin/populate-sectors")'''

content = content.replace(insert_marker, new_endpoint)

# Write back
with open('c:/AlgoTrading/backend/app/main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ Added /api/sectors/list endpoint")
