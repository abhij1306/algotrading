"""
Data repository layer for database operations
Provides clean interface for data access
"""
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc
from datetime import datetime, date, timedelta
from typing import List, Optional, Dict
import pandas as pd

from .database import (
    Company, HistoricalPrice, FinancialStatement, 
    QuarterlyResult, DataUpdateLog, IntradayCandle, get_db
)


class DataRepository:
    """Repository pattern for data access"""
    
    def __init__(self, db: Session):
        self.db = db
    
    # ==================== COMPANY OPERATIONS ====================
    
    def get_company(self, symbol: str) -> Optional[Company]:
        """Get company by symbol"""
        return self.db.query(Company).filter(Company.symbol == symbol).first()
    
    def get_or_create_company(self, symbol: str, **kwargs) -> Company:
        """Get existing company or create new"""
        company = self.get_company(symbol)
        if not company:
            company = Company(symbol=symbol, **kwargs)
            self.db.add(company)
            self.db.commit()
            self.db.refresh(company)
        return company
    
    def get_all_companies(self, fno_only: bool = False, active_only: bool = True) -> List[Company]:
        """Get all companies with optional filters"""
        query = self.db.query(Company)
        if fno_only:
            query = query.filter(Company.is_fno == True)
        if active_only:
            query = query.filter(Company.is_active == True)
        return query.all()
    
    # ==================== HISTORICAL PRICE OPERATIONS ====================
    
    def save_historical_prices(self, symbol: str, df: pd.DataFrame, source: str = 'fyers'):
        """Save historical prices from DataFrame"""
        try:
            company = self.get_or_create_company(symbol)
            
            records_added = 0
            for idx, row in df.iterrows():
                # Check if record exists
                existing = self.db.query(HistoricalPrice).filter(
                    and_(
                        HistoricalPrice.company_id == company.id,
                        HistoricalPrice.date == idx.date()
                    )
                ).first()
                
                if not existing:
                    price = HistoricalPrice(
                        company_id=company.id,
                        date=idx.date(),
                        open=float(row['Open']),
                        high=float(row['High']),
                        low=float(row['Low']),
                        close=float(row['Close']),
                        volume=int(row['Volume']),
                        adj_close=float(row.get('Adj Close', row['Close'])),
                        source=source
                    )
                    self.db.add(price)
                    records_added += 1
            
            self.db.commit()
            
            # Log update
            try:
                self.log_data_update(
                    company.id, 
                    'historical', 
                    records_added, 
                    'success',
                    start_date=df.index.min().date(),
                    end_date=df.index.max().date()
                )
            except:
                pass  # Don't fail if logging fails
            
            return records_added
        except Exception as e:
            self.db.rollback()
            raise e
    
    def get_historical_prices(
        self, 
        symbol: str, 
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        days: Optional[int] = None
    ) -> pd.DataFrame:
        """Get historical prices as DataFrame"""
        company = self.get_company(symbol)
        if not company:
            return pd.DataFrame()
        
        query = self.db.query(HistoricalPrice).filter(
            HistoricalPrice.company_id == company.id
        )
        
        if start_date:
            query = query.filter(HistoricalPrice.date >= start_date)
        if end_date:
            query = query.filter(HistoricalPrice.date <= end_date)
        if days:
            # Use the latest available date in database, not today's date
            # This ensures we get data even if database hasn't been updated today
            latest_record = self.db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == company.id
            ).order_by(desc(HistoricalPrice.date)).first()
            
            if latest_record:
                cutoff_date = latest_record.date - timedelta(days=days)
                query = query.filter(HistoricalPrice.date >= cutoff_date)
        
        query = query.order_by(HistoricalPrice.date)
        prices = query.all()
        
        if not prices:
            return pd.DataFrame()
        
        # Convert to DataFrame with UPPERCASE column names (required by portfolio analysis)
        data = {
            'Open': [p.open for p in prices],
            'High': [p.high for p in prices],
            'Low': [p.low for p in prices],
            'Close': [p.close for p in prices],
            'Volume': [p.volume for p in prices],
        }
        df = pd.DataFrame(data, index=[p.date for p in prices])
        df.index.name = 'date'
        df.index = pd.to_datetime(df.index)
        
        return df
    
    def get_latest_price_date(self, symbol: str) -> Optional[date]:
        """Get the latest date for which we have price data"""
        company = self.get_company(symbol)
        if not company:
            return None
        
        latest = self.db.query(HistoricalPrice).filter(
            HistoricalPrice.company_id == company.id
        ).order_by(desc(HistoricalPrice.date)).first()
        
        return latest.date if latest else None
    
    # ==================== INTRADAY DATA OPERATIONS ====================
    
    def get_intraday_candles(
        self,
        symbol: str,
        timeframe: int = 5,  # timeframe in minutes
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        days: Optional[int] = None
    ) -> pd.DataFrame:
        """
        Get intraday candle data as DataFrame
        
        Args:
            symbol: Stock symbol
            timeframe: Candle timeframe in minutes (1, 5, 15, 30, 60)
            start_date: Start datetime
            end_date: End datetime
            days: Alternative to start_date - fetch last N days
            
        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        company = self.get_company(symbol)
        if not company:
            return pd.DataFrame()
        
        query = self.db.query(IntradayCandle).filter(
            and_(
                IntradayCandle.company_id == company.id,
                IntradayCandle.timeframe == timeframe
            )
        )
        
        if start_date:
            query = query.filter(IntradayCandle.timestamp >= start_date)
        if end_date:
            query = query.filter(IntradayCandle.timestamp <= end_date)
        if days and not start_date:
            cutoff = datetime.now() - timedelta(days=days)
            query = query.filter(IntradayCandle.timestamp >= cutoff)
        
        query = query.order_by(IntradayCandle.timestamp)
        candles = query.all()
        
        if not candles:
            return pd.DataFrame()
        
        # Convert to DataFrame
        data = {
            'timestamp': [c.timestamp for c in candles],
            'open': [c.open for c in candles],
            'high': [c.high for c in candles],
            'low': [c.low for c in candles],
            'close': [c.close for c in candles],
            'volume': [c.volume for c in candles],
        }
        df = pd.DataFrame(data)
        return df
    
    # ==================== FINANCIAL STATEMENT OPERATIONS ====================
    
    def save_financial_statement(self, symbol: str, data: Dict, period_type: str = 'quarterly'):
        """Save financial statement data"""
        company = self.get_or_create_company(symbol)
        
        # Extract period_type from data if present, otherwise use parameter
        actual_period_type = data.pop('period_type', period_type)
        
        # Check if exists
        existing = self.db.query(FinancialStatement).filter(
            and_(
                FinancialStatement.company_id == company.id,
                FinancialStatement.period_end == data['period_end'],
                FinancialStatement.period_type == actual_period_type
            )
        ).first()
        
        if existing:
            # Update existing
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            existing.updated_at = datetime.utcnow()
        else:
            # Create new
            statement = FinancialStatement(
                company_id=company.id,
                period_type=actual_period_type,
                **data
            )
            self.db.add(statement)
        
        self.db.commit()
    
    def get_financial_statements(
        self, 
        symbol: str, 
        period_type: Optional[str] = None,
        limit: int = 10
    ) -> List[FinancialStatement]:
        """Get financial statements"""
        company = self.get_company(symbol)
        if not company:
            return []
        
        query = self.db.query(FinancialStatement).filter(
            FinancialStatement.company_id == company.id
        )
        
        if period_type:
            query = query.filter(FinancialStatement.period_type == period_type)
        
        query = query.order_by(desc(FinancialStatement.period_end)).limit(limit)
        return query.all()
    
    def get_latest_financials(self, symbol: str) -> Optional[FinancialStatement]:
        """Get the most recent financial statement for a symbol"""
        statements = self.get_financial_statements(symbol, limit=1)
        return statements[0] if statements else None
    
    # ==================== QUARTERLY RESULT OPERATIONS ====================
    
    def save_quarterly_result(self, symbol: str, data: Dict):
        """Save quarterly result"""
        company = self.get_or_create_company(symbol)
        
        existing = self.db.query(QuarterlyResult).filter(
            and_(
                QuarterlyResult.company_id == company.id,
                QuarterlyResult.quarter_end == data['quarter_end']
            )
        ).first()
        
        if existing:
            for key, value in data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            existing.updated_at = datetime.utcnow()
        else:
            result = QuarterlyResult(
                company_id=company.id,
                **data
            )
            self.db.add(result)
        
        self.db.commit()
    
    def get_quarterly_results(self, symbol: str, limit: int = 8) -> List[QuarterlyResult]:
        """Get quarterly results"""
        company = self.get_company(symbol)
        if not company:
            return []
        
        return self.db.query(QuarterlyResult).filter(
            QuarterlyResult.company_id == company.id
        ).order_by(desc(QuarterlyResult.quarter_end)).limit(limit).all()
    
    # ==================== UPDATE LOG OPERATIONS ====================
    
    def log_data_update(
        self, 
        company_id: int, 
        data_type: str, 
        records_updated: int,
        status: str,
        error_message: str = None,
        start_date: date = None,
        end_date: date = None
    ):
        """Log data update"""
        log = DataUpdateLog(
            company_id=company_id,
            data_type=data_type,
            last_update=datetime.utcnow(),
            records_updated=records_updated,
            status=status,
            error_message=error_message,
            start_date=start_date,
            end_date=end_date
        )
        self.db.add(log)
        self.db.commit()
    
    def get_last_update(self, symbol: str, data_type: str) -> Optional[DataUpdateLog]:
        """Get last update log for a company and data type"""
        company = self.get_company(symbol)
        if not company:
            return None
        
        return self.db.query(DataUpdateLog).filter(
            and_(
                DataUpdateLog.company_id == company.id,
                DataUpdateLog.data_type == data_type,
                DataUpdateLog.status == 'success'
            )
        ).order_by(desc(DataUpdateLog.last_update)).first()
