"""
Unified Data Provider
Single point of access for all market data (Historical, Fundamental, Live Quotes)

Architecture:
- PostgreSQL: Metadata, Financial Statements, Historical Prices
- Parquet: Heavy time-series data (future optimization)
- Fyers API v3: Live quotes, missing historical data
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import pandas as pd
from sqlalchemy.orm import Session

from ..database import Company, HistoricalPrice, FinancialStatement
from ..data_repository import DataRepository
from ..data_fetcher import fetch_historical_data
from .exceptions import (
    DataNotFoundError,
    DataSourceUnavailableError,
    InvalidSymbolError,
    MissingTokenError
)


class DataProvider:
    """
    Unified data access layer enforcing:
    1. Single Source of Truth (PostgreSQL/Parquet)
    2. Fyers as secondary source (fallback)
    3. Standard exception handling
    4. No direct Fyers access from UI components
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.repo = DataRepository(db)
    
    def get_history(
        self,
        symbol: str,
        timeframe: str = "1D",
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        days: int = 365
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE', 'NIFTY50')
            timeframe: '1D' (daily), '5MIN', '15MIN', '1H'
            start_date: Start date (defaults to `days` ago)
            end_date: End date (defaults to today)
            days: Lookback days if start_date not provided
            
        Returns:
            DataFrame with columns: [Date, Open, High, Low, Close, Volume]
            
        Raises:
            DataNotFoundError: If symbol has no data
            InvalidSymbolError: If symbol doesn't exist
        """
        # Validate symbol
        company = self.db.query(Company).filter(Company.symbol == symbol.upper()).first()
        if not company:
            raise InvalidSymbolError(symbol)
        
        # Daily data: Use PostgreSQL
        if timeframe == "1D":
            df = self.repo.get_historical_prices(symbol, days=days)
            
            if df is None or df.empty:
                # Fallback: Try fetching from Fyers
                try:
                    df = fetch_historical_data(symbol, days=days)
                    if df is None or df.empty:
                        raise DataNotFoundError(
                            symbol,
                            f"No historical data available for {symbol}"
                        )
                except Exception as e:
                    raise DataSourceUnavailableError(
                        "Fyers",
                        f"Failed to fetch data for {symbol}: {str(e)}"
                    )
            
            return df
        
        # Intraday data: Use IntradayCandle table or Parquet
        else:
            # TODO: Implement intraday fetch from IntradayCandle table
            raise NotImplementedError(
                f"Timeframe '{timeframe}' not yet supported. Use '1D' for daily data."
            )
    
    def get_fundamentals(self, symbol: str) -> Dict:
        """
        Fetch latest financial statement
        
        Returns:
            Dict with keys: revenue, net_income, eps, pe_ratio, debt_to_equity, roe, etc.
            
        Raises:
            DataNotFoundError: If no financial data exists
        """
        company = self.db.query(Company).filter(Company.symbol == symbol.upper()).first()
        if not company:
            raise InvalidSymbolError(symbol)
        
        fs = self.db.query(FinancialStatement)\
            .filter(FinancialStatement.company_id == company.id)\
            .order_by(FinancialStatement.period_end.desc())\
            .first()
        
        if not fs:
            raise DataNotFoundError(
                symbol,
                f"No financial statements available for {symbol}"
            )
        
        return {
            "symbol": symbol,
            "period_end": fs.period_end.isoformat() if fs.period_end else None,
            "revenue": fs.revenue,
            "net_income": fs.net_income,
            "eps": fs.eps,
            "pe_ratio": fs.pe_ratio,
            "pb_ratio": fs.pb_ratio,
            "debt_to_equity": fs.debt_to_equity,
            "roe": fs.roe,
            "roa": fs.roa,
            "market_cap": company.market_cap
        }
    
    def get_quote(self, symbol: str) -> Dict:
        """
        Fetch live quote from Fyers
        
        Returns:
            Dict with keys: ltp, open, high, low, close, volume, change_pct
            
        Raises:
            DataSourceUnavailableError: If Fyers is down
        """
        try:
            from ..data_fetcher import fetch_fyers_quotes
            
            quotes = fetch_fyers_quotes([symbol])
            if not quotes or symbol not in quotes:
                raise DataNotFoundError(symbol, f"No live quote available for {symbol}")
            
            return quotes[symbol]
        
        except Exception as e:
            if "token" in str(e).lower():
                raise MissingTokenError()
            raise DataSourceUnavailableError("Fyers", str(e))
    
    def get_multiple_quotes(self, symbols: List[str]) -> Dict[str, Dict]:
        """
        Batch fetch live quotes
        
        Returns:
            Dict mapping symbol -> quote data
        """
        try:
            from ..data_fetcher import fetch_fyers_quotes
            return fetch_fyers_quotes(symbols)
        except Exception as e:
            if "token" in str(e).lower():
                raise MissingTokenError()
            raise DataSourceUnavailableError("Fyers", str(e))
    
    def health_check(self) -> Dict:
        """
        Verify data source availability
        
        Returns:
            {
                "postgresql": "OK" | "DOWN",
                "fyers": "OK" | "DOWN" | "TOKEN_EXPIRED",
                "last_update": ISO timestamp,
                "total_symbols": int
            }
        """
        status = {
            "postgresql": "UNKNOWN",
            "fyers": "UNKNOWN",
            "last_update": None,
            "total_symbols": 0
        }
        
        # Check PostgreSQL
        try:
            count = self.db.query(Company).count()
            status["postgresql"] = "OK"
            status["total_symbols"] = count
        except Exception as e:
            status["postgresql"] = f"DOWN: {str(e)}"
        
        # Check Fyers
        try:
            from ..fyers_direct import get_fyers_client
            client = get_fyers_client()
            profile = client.get_profile()
            status["fyers"] = "OK"
        except Exception as e:
            if "token" in str(e).lower() or "401" in str(e):
                status["fyers"] = "TOKEN_EXPIRED"
            else:
                status["fyers"] = f"DOWN: {str(e)}"
        
        # Get latest data update timestamp
        try:
            latest = self.db.query(HistoricalPrice)\
                .order_by(HistoricalPrice.created_at.desc())\
                .first()
            if latest:
                status["last_update"] = latest.created_at.isoformat()
        except:
            pass
        
        return status
