from fastapi import FastAPI, HTTPException, WebSocket, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import sessionmaker
from typing import List, Optional
import json
from datetime import date
from .database import Base, Company, FinancialStatement, engine, SessionLocal
from .data_repository import DataRepository

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Start Cache Manager
# Initialize Database
Base.metadata.create_all(bind=engine)

# --- Compute Features (EMA, RSI, etc) ---
import pandas as pd
from .indicators import compute_features

# --- Cache for Screeners ---
SCREENER_CACHE = {
    'last_update': None,
    'data': []
}

@app.get("/")
def read_root():
    return {"status": "ok", "message": "AlgoTrading Backend Running"}

@app.get("/api/symbols/search")
async def search_symbols(q: str = ""):
    if len(q) < 1:
        return {"symbols": []}
    
    from .database import SessionLocal, Company
    db = SessionLocal()
    try:
        companies = db.query(Company).filter(
            Company.symbol.ilike(f"{q}%")
        ).order_by(Company.symbol).limit(10).all()
        
        return {
            "symbols": [
                {"symbol": c.symbol, "name": c.name} 
                for c in companies
            ]
        }
    finally:
        db.close()

@app.get("/api/screener")
async def get_screener(page: int = 1, limit: int = 100, sort_by: str = 'symbol', sort_order: str = 'asc', symbol: str = None):
    """
    Returns paginated list of companies with technical indicators.
    Also supports sorting and filtering by symbol.
    """
    db = SessionLocal()
    repo = DataRepository(db)
    try:
        # Complex sorting (RSI etc) requires full compute, so for first load we force Symbol sort.
        
        offset = (page - 1) * limit
        companies_query = db.query(Company).filter(Company.is_active == True)
        
        # Add symbol filter if provided
        if symbol:
            companies_query = companies_query.filter(Company.symbol == symbol.upper())
        
        total_records = companies_query.count() # Fast count
        
        # If user requested specific sort that we can't do in SQL easily (like RSI), 
        # we warn or fallback. For now, just fetch by symbol to show *something*.
        companies = companies_query.order_by(Company.symbol).offset(offset).limit(limit).all()
        
        computed_list = []
        for company in companies:
            try:
                hist = repo.get_historical_prices(company.symbol, days=200)
                if hist is not None and not hist.empty:
                    features = compute_features(company.symbol, hist)
                else:
                    features = {'symbol': company.symbol, 'close': 0, 'volume': 0, 'ema20': 0, 'ema50': 0, 'atr_pct': 0, 'rsi': 0, 'vol_percentile': 0}
                
                if 'symbol' not in features:
                    features['symbol'] = company.symbol
                computed_list.append(features)
            except:
                continue
        
        db.close()
        
        return {
            'data': computed_list,
            'meta': {
                'page': page,
                'limit': limit,
                'total': total_records,
                'total_pages': (total_records + limit - 1) // limit if limit > 0 else 0
            }
        }
        
    except Exception as e:
        db.close()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/screener/financials")
async def get_financials_screener(page: int = 1, limit: int = 100, sort_by: str = 'symbol', sort_order: str = 'asc', symbol: str = None):
    """
    Returns paginated list of companies with latest financial data
    """
    db = SessionLocal()
    try:
        offset = (page - 1) * limit
        
        # Subquery to get latest financial statement date for each company
        latest_subquery = db.query(
            FinancialStatement.company_id,
            func.max(FinancialStatement.period_end).label('max_date')
        ).filter(FinancialStatement.period_type == 'annual').group_by(FinancialStatement.company_id).subquery()
        
        query = db.query(Company, FinancialStatement).join(
            latest_subquery,
            (FinancialStatement.company_id == latest_subquery.c.company_id) & 
            (FinancialStatement.period_end == latest_subquery.c.max_date)
        ).join(
            Company, Company.id == FinancialStatement.company_id
        ).filter(Company.is_active == True)
        
        # Add symbol filter if provided
        if symbol:
            query = query.filter(Company.symbol == symbol.upper())
        
        # Get total count before pagination
        total_records = query.count()
        
        # Apply sorting
        if sort_by == 'symbol':
            sort_col = Company.symbol
        elif hasattr(FinancialStatement, sort_by):
            sort_col = getattr(FinancialStatement, sort_by)
        else:
            sort_col = Company.symbol
            
        if sort_order == 'desc':
            query = query.order_by(desc(sort_col))
        else:
            query = query.order_by(sort_col)
            
        results = query.offset(offset).limit(limit).all()
        
        data = []
        for company, fs in results:
            data.append({
                'symbol': company.symbol,
                'market_cap': float(company.market_cap) if company.market_cap else 0,
                'revenue': float(fs.revenue) if fs.revenue else 0,
                'net_income': float(fs.net_income) if fs.net_income else 0,
                'eps': float(fs.eps) if fs.eps else 0,
                'roe': float(fs.roe) if fs.roe else 0,
                'debt_to_equity': float(fs.debt_to_equity) if fs.debt_to_equity else 0,
                'pe_ratio': float(fs.pe_ratio) if fs.pe_ratio else 0,
                'period_end': fs.period_end.isoformat()
            })
            
        db.close()
        
        return {
            'data': data,
            'meta': {
                'page': page,
                'limit': limit,
                'total': total_records,
                'total_pages': (total_records + limit - 1) // limit if limit > 0 else 0
            }
        }
        
    except Exception as e:
        print(f"Error in financials screener: {str(e)}")
        # import traceback
        # traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Financials failed: {str(e)}")

@app.post("/api/upload/bulk-financials")
async def upload_financials(files: List[UploadFile] = File(...)):
    db = SessionLocal()
    from .excel_parser import SmartFinancialParser
    parser = SmartFinancialParser()
    
    try:
        all_metadata = []
        stats = {
            'total_processed': 0,
            'symbols_updated': [],
            'symbols_not_found': []
        }
        all_errors = []
        total_success = 0
        symbols_updated = []
        symbols_not_found = []

        for file in files:
            try:
                # Read file
                contents = await file.read()
                
                # Use AI parser
                parsed_df, metadata = parser.parse_excel(contents, file.filename)
                all_metadata.append({
                    'filename': file.filename,
                    **metadata
                })
                
                print(f"Parsed {file.filename}: {len(parsed_df)} rows")
                if not parsed_df.empty:
                    print(f"First row: {parsed_df.iloc[0].to_dict()}")
                
                file_success_count = 0
                
                for _, row in parsed_df.iterrows():
                    try:
                        symbol = row['symbol']
                        print(f"Processing symbol: {symbol}, revenue: {row.get('revenue', 0)}")
                        
                        # Find Company - Strict Match Only
                        company = db.query(Company).filter(Company.symbol == symbol).first()
                        if not company:
                            # Try finding with symbol matching logic first
                            matched_symbol = parser._match_symbol_from_db(symbol)
                            if matched_symbol != symbol:
                                company = db.query(Company).filter(Company.symbol == matched_symbol).first()
                        
                        if not company:
                            print(f"Skipping {symbol}: Not found in master list")
                            symbols_not_found.append(symbol)
                            continue
                                
                        # Upsert Financial Statement
                        today = date.today()
                        existing_fs = db.query(FinancialStatement).filter(
                            FinancialStatement.company_id == company.id,
                            FinancialStatement.period_type == 'annual',
                            FinancialStatement.period_end == today
                        ).first()

                        if existing_fs:
                            existing_fs.revenue = row['revenue']
                            existing_fs.net_income = row['net_income']
                            existing_fs.eps = row['eps']
                            existing_fs.roe = row['roe']
                            existing_fs.debt_to_equity = row['debt_to_equity']
                            existing_fs.pe_ratio = row['pe_ratio']
                            existing_fs.source = 'ai_bulk_upload'
                            print(f"Updated existing financial statement for {company.symbol}")
                        else:
                            fs = FinancialStatement(
                                company_id=company.id,
                                period_end=today,
                                period_type='annual',
                                revenue=row['revenue'],
                                net_income=row['net_income'],
                                eps=row['eps'],
                                roe=row['roe'],
                                debt_to_equity=row['debt_to_equity'],
                                pe_ratio=row['pe_ratio'],
                                source='ai_bulk_upload'
                            )
                            db.add(fs)
                            print(f"Created new financial statement for {company.symbol}")
                        
                        symbols_updated.append(company.symbol)
                        file_success_count += 1
                        
                    except Exception as row_e:
                        print(f"❌ Row error for {symbol}: {str(row_e)}")
                        all_errors.append(f"Row error for {symbol}: {str(row_e)}")
                        continue
                
                db.commit()
                total_success += file_success_count
                
            except Exception as file_e:
                all_errors.append(f"File {file.filename}: {str(file_e)}")
                continue
        
        # Build detailed message
        message_parts = [f"Successfully processed {total_success} records from {len(files)} files"]
        if symbols_updated:
            message_parts.append(f"Updated: {', '.join(sorted(list(set(symbols_updated)))[:10])}")
        if symbols_not_found:
            message_parts.append(f"Symbols not in database: {', '.join(sorted(list(set(symbols_not_found)))[:10])}")
        
        full_message = ". ".join(message_parts)
            
        return {
            "message": full_message,
            "metadata": all_metadata,
            "stats": {
                'total_processed': total_success,
                'symbols_updated': list(set(symbols_updated)),
                'symbols_not_found': list(set(symbols_not_found))
            },
            "errors": all_errors
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
