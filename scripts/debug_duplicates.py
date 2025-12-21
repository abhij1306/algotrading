import sys
import os
sys.path.append(os.getcwd())
from backend.app.database import SessionLocal, Company, FinancialStatement
from sqlalchemy import func

def check_duplicates():
    db = SessionLocal()
    try:
        # Check specific symbol reported
        print("Checking ABSLAMC...")
        company = db.query(Company).filter(Company.symbol == 'ABSLAMC').first()
        if company:
            print(f"Company ID: {company.id}")
            stmts = db.query(FinancialStatement).filter(
                FinancialStatement.company_id == company.id
            ).all()
            print(f"Found {len(stmts)} statements for ABSLAMC:")
            for s in stmts:
                print(f"  ID: {s.id}, Period: {s.period_end}, Type: {s.period_type}, Revenue: {s.revenue}")
        
        # Check query logic simulation
        print("\nSimulating Join Query...")
        
        # Subquery for max date
        latest_fin_subquery = db.query(
            FinancialStatement.company_id,
            func.max(FinancialStatement.period_end).label('latest_period')
        ).group_by(FinancialStatement.company_id).subquery()
        
        results = db.query(Company, FinancialStatement).join(
            latest_fin_subquery,
            Company.id == latest_fin_subquery.c.company_id
        ).join(
            FinancialStatement,
            (FinancialStatement.company_id == Company.id) & 
            (FinancialStatement.period_end == latest_fin_subquery.c.latest_period)
        ).filter(Company.symbol == 'ABSLAMC').all()
        
        print(f"\nQuery returned {len(results)} rows:")
        for r in results:
             c = r[0]
             fs = r[1]
             print(f"  Symbol: {c.symbol}, FS ID: {fs.id}, Period: {fs.period_end}, Type: {fs.period_type}")

    finally:
        db.close()

if __name__ == "__main__":
    check_duplicates()
