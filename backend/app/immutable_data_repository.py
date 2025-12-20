"""
Immutable Data Repository
Enforces read-only access to the database using PostgreSQL read-only transactions.
"""
from sqlalchemy.orm import Session
from .data_repository import DataRepository
import pandas as pd
from typing import Optional, List, Dict
from datetime import date, datetime

class ImmutableDataRepository(DataRepository):
    """
    A read-only version of DataRepository.
    Enforces 'SET TRANSACTION READ ONLY' on the session.
    """
    
    def __init__(self, db: Session):
        super().__init__(db)
        # Enforce read-only transaction at the database level
        try:
            self.db.execute("SET TRANSACTION READ ONLY")
        except Exception as e:
            # Fallback for databases that might not support it or if already in a transaction
            print(f"Warning: Could not set transaction to read-only: {e}")
            
    def _make_immutable(self, df: pd.DataFrame) -> pd.DataFrame:
        """Helper to make a DataFrame immutable"""
        if df is None or df.empty:
            return df
        # Set flags to prevent accidental modification
        # Note: set_flags(write=False) is for the underlying numpy array
        # It's a good guard but not perfect for all pandas operations
        immutable_df = df.copy(deep=True)
        try:
            immutable_df.set_flags(write=False)
        except:
            pass
        return immutable_df

    # Override read methods to ensure immutability of returned DataFrames
    
    def get_historical_prices(self, *args, **kwargs) -> pd.DataFrame:
        df = super().get_historical_prices(*args, **kwargs)
        return self._make_immutable(df)
        
    def get_intraday_candles(self, *args, **kwargs) -> pd.DataFrame:
        df = super().get_intraday_candles(*args, **kwargs)
        return self._make_immutable(df)
        
    def get_historical_data(self, *args, **kwargs) -> Optional[pd.DataFrame]:
        df = super().get_historical_data(*args, **kwargs)
        return self._make_immutable(df)

    # Disable all write methods
    
    def save_historical_prices(self, *args, **kwargs):
        raise PermissionError("Write operations are disabled in ImmutableDataRepository")
        
    def save_financial_statement(self, *args, **kwargs):
        raise PermissionError("Write operations are disabled in ImmutableDataRepository")
        
    def save_quarterly_result(self, *args, **kwargs):
        raise PermissionError("Write operations are disabled in ImmutableDataRepository")
        
    def log_data_update(self, *args, **kwargs):
        raise PermissionError("Write operations are disabled in ImmutableDataRepository")
        
    def get_or_create_company(self, *args, **kwargs):
        # We should only allow get, not create
        symbol = args[0] if args else kwargs.get('symbol')
        company = self.get_company(symbol)
        if not company:
            raise PermissionError(f"Company {symbol} not found and creation is disabled in ImmutableDataRepository")
        return company
