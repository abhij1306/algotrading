"""
NSE Data Reader - DuckDB-based Parquet Reader
Reads historical NSE data from Parquet files on-demand (Cold Layer)
"""

import duckdb
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
from typing import Optional, List

class NSEDataReader:
    """
    Read-only NSE data reader using DuckDB
    - Single DuckDB connection per instance (prevents file locks)
    - Explicit cleanup on deletion
    - Reads from Parquet files directly
    - Supports date range queries and symbol filtering
    """
    
    def __init__(self, data_dir: str = "nse_data/processed/equities_clean"):
        self.data_dir = Path(data_dir)
        if not self.data_dir.exists():
            raise ValueError(f"NSE data directory not found: {self.data_dir}")
        
        # Create single DuckDB connection for this instance
        # Use in-memory to avoid file lock issues on Windows
        self.con = duckdb.connect(':memory:', read_only=False)
    
    def __del__(self):
        """Cleanup DuckDB connection on deletion"""
        if hasattr(self, 'con') and self.con:
            try:
                self.con.close()
            except Exception:
                pass  # Ignore errors during cleanup
    
    def get_historical_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        Get historical OHLCV data for a symbol
        
        Args:
            symbol: Stock symbol (e.g., 'RELIANCE')
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with columns: trade_date, open, high, low, close, volume, etc.
        """
        try:
            # SECURITY FIX: Use parameterized query to prevent SQL injection
            query = f"""
                SELECT *
                FROM read_parquet('{self.data_dir}/equity_ohlcv.parquet')
                WHERE symbol = ?
                  AND trade_date >= ?
                  AND trade_date <= ?
                ORDER BY trade_date ASC
            """
            
            df = self.con.execute(query, [symbol, start_date, end_date]).df()
            
            if df.empty:
                return None
            
            # Rename columns to standard format for compatibility
            df = df.rename(columns={
                'trade_date': 'DATE',
                'symbol': 'SYMBOL',
                'open': 'OPEN',
                'high': 'HIGH',
                'low': 'LOW',
                'close': 'CLOSE',
                'volume': 'VOLUME',
                'turnover': 'TURNOVER'
            })
            
            return df
        
        except Exception as e:
            print(f"Error reading NSE data for {symbol}: {e}")
            return None
    
    def get_multiple_symbols(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        Get historical data for multiple symbols
        
        Args:
            symbols: List of stock symbols
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with all symbols combined
        """
        try:
            # SECURITY FIX: Use parameterized query with list binding
            # Create placeholders for the IN clause
            placeholders = ','.join(['?' for _ in symbols])
            
            query = f"""
                SELECT *
                FROM read_parquet('{self.data_dir}/equity_ohlcv.parquet')
                WHERE symbol IN ({placeholders})
                  AND trade_date >= ?
                  AND trade_date <= ?
                ORDER BY trade_date ASC, symbol ASC
            """
            
            # Combine symbols list with date parameters
            params = symbols + [start_date, end_date]
            df = self.con.execute(query, params).df()
            
            if df.empty:
                return None
            
            return df
        
        except Exception as e:
            print(f"Error reading NSE data for multiple symbols: {e}")
            return None
    
    def get_latest_date(self) -> Optional[str]:
        """Get the latest available date in the dataset"""
        try:
            query = f"""
                SELECT MAX(trade_date) as latest_date
                FROM read_parquet('{self.data_dir}/equity_ohlcv.parquet')
            """
            
            result = self.con.execute(query).fetchone()
            
            return result[0] if result else None
        
        except Exception as e:
            print(f"Error getting latest date: {e}")
            return None
    
    def get_date_range(self) -> Optional[tuple]:
        """Get the min and max dates available in the dataset"""
        try:
            query = f"""
                SELECT MIN(trade_date) as min_date, MAX(trade_date) as max_date
                FROM read_parquet('{self.data_dir}/equity_ohlcv.parquet')
            """
            
            result = self.con.execute(query).fetchone()
            
            return (result[0], result[1]) if result else None
        
        except Exception as e:
            print(f"Error getting date range: {e}")
            return None
    
    def get_symbols_for_date(self, date: str) -> Optional[List[str]]:
        """Get all symbols available for a specific date"""
        try:
            # SECURITY FIX: Use parameterized query
            query = f"""
                SELECT DISTINCT symbol
                FROM read_parquet('{self.data_dir}/equity_ohlcv.parquet')
                WHERE trade_date = ?
                ORDER BY symbol
            """
            
            df = self.con.execute(query, [date]).df()
            
            return df['symbol'].tolist() if not df.empty else None
        
        except Exception as e:
            print(f"Error getting symbols for date {date}: {e}")
            return None


# Example usage
if __name__ == "__main__":
    reader = NSEDataReader()
    
    # Test 1: Get date range
    print("Date Range:", reader.get_date_range())
    
    # Test 2: Get historical data for RELIANCE
    df = reader.get_historical_data(
        symbol="RELIANCE",
        start_date="2024-01-01",
        end_date="2024-12-31"
    )
    
    if df is not None:
        print(f"\nRELIANCE data: {len(df)} records")
        print(df.head())
    else:
        print("\nNo data found for RELIANCE")
