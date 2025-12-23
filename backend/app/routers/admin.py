from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session
from ..database import get_db, SessionLocal, FinancialStatement, Company
from typing import List
import pandas as pd
from datetime import date
import io

router = APIRouter()

@router.post("/financials/upload")
async def upload_financials(
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    """
    Upload and parse financial statements (Excel/CSV)
    Expected columns: Symbol, Revenue, Net Income, EPS, ROE, Debt/Equity, P/E Ratio, Period End
    """
    results = {
        "processed": 0,
        "errors": [],
        "details": []
    }
    
    for file in files:
        try:
            content = await file.read()
            df = None
            
            if file.filename.endswith('.xlsx'):
                df = pd.read_excel(io.BytesIO(content))
            elif file.filename.endswith('.csv'):
                df = pd.read_csv(io.BytesIO(content))
            else:
                results["errors"].append(f"Skipped {file.filename}: Invalid format")
                continue
                
            # Normalize columns
            df.columns = [c.lower().strip().replace(' ', '_').replace('/', '_') for c in df.columns]
            
            # Required columns check
            required = ['symbol', 'period_end']
            if not all(col in df.columns for col in required):
                results["errors"].append(f"Skipped {file.filename}: Missing required columns")
                continue
                
            processed_count = 0
            for _, row in df.iterrows():
                try:
                    symbol = str(row['symbol']).upper().strip()
                    
                    # Find company
                    company = db.query(Company).filter(Company.symbol == symbol).first()
                    if not company:
                        # Auto-create company if missing (optional)
                        # For now, skip
                        continue
                        
                    # Parse date
                    period_date = pd.to_datetime(row['period_end']).date()
                    
                    # Create/Update Financial Statement
                    # Check if exists
                    stmt = db.query(FinancialStatement).filter(
                        FinancialStatement.company_id == company.id,
                        FinancialStatement.period_end == period_date
                    ).first()
                    
                    if not stmt:
                        stmt = FinancialStatement(
                            company_id=company.id,
                            period_end=period_date,
                            period_type='annual' # Default to annual for bulk upload
                        )
                        db.add(stmt)
                    
                    # Update fields
                    if 'revenue' in row and pd.notna(row['revenue']):
                        stmt.revenue = float(row['revenue'])
                    if 'net_income' in row and pd.notna(row['net_income']):
                        stmt.net_income = float(row['net_income'])
                    if 'eps' in row and pd.notna(row['eps']):
                        stmt.eps = float(row['eps'])
                    if 'roe' in row and pd.notna(row['roe']):
                        stmt.roe = float(row['roe'])
                    if 'debt_to_equity' in row and pd.notna(row['debt_to_equity']):
                        stmt.debt_to_equity = float(row['debt_to_equity'])
                    if 'p_e_ratio' in row and pd.notna(row['p_e_ratio']):
                        stmt.pe_ratio = float(row['p_e_ratio'])
                    
                    processed_count += 1
                    
                except Exception as e:
                    results["details"].append(f"Row error {symbol}: {str(e)}")
                    continue
            
            db.commit()
            results["processed"] += processed_count
            results["details"].append(f"File {file.filename}: Processed {processed_count} rows")
            
        except Exception as e:
            results["errors"].append(f"File error {file.filename}: {str(e)}")
            
    return results

@router.get("/sectors/stats")
async def get_sector_statistics():
    """Get statistics about sector data population"""
    db = SessionLocal()
    try:
        total_companies = db.query(Company).count()
        with_sector = db.query(Company).filter(Company.sector.isnot(None)).count()
        
        # Breakdown by sector
        breakdown = db.query(
            Company.sector, func.count(Company.id)
        ).filter(
            Company.sector.isnot(None)
        ).group_by(Company.sector).order_by(desc(func.count(Company.id))).all()
        
        return {
            "total_companies": total_companies,
            "companies_with_sector": with_sector,
            "coverage_pct": round(with_sector / total_companies * 100, 1) if total_companies > 0 else 0,
            "sectors": [
                {"name": name, "count": count} 
                for name, count in breakdown
            ]
        }
    finally:
        db.close()

@router.get("/sectors/list")
async def get_sectors_list():
    """Get list of available sectors for filtering"""
    db = SessionLocal()
    try:
        sectors = db.query(Company.sector).filter(
            Company.sector.isnot(None)
        ).distinct().order_by(Company.sector).all()
        
        return {"sectors": [s[0] for s in sectors]}
    finally:
        db.close()

@router.post("/sectors/populate")
async def trigger_sector_population(background_tasks: BackgroundTasks, limit: int = 50):
    """
    Trigger bulk sector population (limited to prevent timeout)
    """
    def run_population():
        # This would import from a sector population service
        # For now, stubbed
        print(f"Running sector population for limit {limit}")
        pass
        
    background_tasks.add_task(run_population)
    return {"message": "Sector population started in background"}
