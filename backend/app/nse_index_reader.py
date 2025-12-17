"""
NSE Index Data Reader - DuckDB-based Parquet Reader for Index Data
Reads historical NSE index data from Parquet files on-demand
"""

import duckdb
from pathlib import Path
from datetime import datetime
import pandas as pd
from typing import Optional, List

class NSEIndexReader:
    """
    Read-only NSE index data reader using DuckDB
    - No caching (pure on-demand reads)
    - Reads from Parquet files directly
    - Supports date range queries and multiple indices
    """
    
    def __init__(self, data_dir: str = "nse_data/processed/indices_clean"):
        self.data_dir = Path(data_dir)
        if not self.data_dir.exists():
            raise ValueError(f"NSE index data directory not found: {self.data_dir}")
    
    def get_index_data(
        self,
        index_name: str,
        start_date: str,
        end_date: str
    ) -> Optional[pd.DataFrame]:
        """
        Get historical OHLC data for an index
        
        Args:
            index_name: Index name (e.g., 'nifty50', 'niftybank')
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        
        Returns:
            DataFrame with columns: trade_date, open, high, low, close, volume, turnover
        """
        try:
            con = duckdb.connect(':memory:')
            
            index_file = self.data_dir / "index_ohlcv.parquet"
            if not index_file.exists():
                print(f"Index file not found: {index_file}")
                return None
            
            query = f"""
                SELECT *
                FROM read_parquet('{index_file}')
                WHERE index_name = '{index_name}'
                  AND trade_date >= '{start_date}'
                  AND trade_date <= '{end_date}'
                ORDER BY trade_date ASC
            """
            
            df = con.execute(query).df()
            con.close()
            
            if df.empty:
                return None
            
            # Rename columns to standard format for compatibility
            df = df.rename(columns={
                'trade_date': 'DATE',
                'index_name': 'INDEX',
                'open': 'OPEN',
                'high': 'HIGH',
                'low': 'LOW',
                'close': 'CLOSE',
                'volume': 'VOLUME',
                'turnover': 'TURNOVER'
            })
            
            return df
        
        except Exception as e:
            print(f"Error reading index data for {index_name}: {e}")
            return None
    
    def get_latest_date(self, index_name: str = "nifty50") -> Optional[str]:
        """Get the latest available date for an index"""
        try:
            con = duckdb.connect(':memory:')
            
            index_file = self.data_dir / "index_ohlcv.parquet"
            if not index_file.exists():
                return None
            
            query = f"""
                SELECT MAX(trade_date) as latest_date
                FROM read_parquet('{index_file}')
                WHERE index_name = '{index_name}'
            """
            
            result = con.execute(query).fetchone()
            con.close()
            
            return str(result[0]) if result else None
        
        except Exception as e:
            print(f"Error getting latest date: {e}")
            return None
    
    def get_date_range(self, index_name: str = "nifty50") -> Optional[tuple]:
        """Get the min and max dates available for an index"""
        try:
            con = duckdb.connect(':memory:')
            
            index_file = self.data_dir / "index_ohlcv.parquet"
            if not index_file.exists():
                return None
            
            query = f"""
                SELECT MIN(trade_date) as min_date, MAX(trade_date) as max_date
                FROM read_parquet('{index_file}')
                WHERE index_name = '{index_name}'
            """
            
            result = con.execute(query).fetchone()
            con.close()
            
            return (str(result[0]), str(result[1])) if result else None
        
        except Exception as e:
            print(f"Error getting date range: {e}")
            return None
    
    def get_available_indices(self) -> List[str]:
        """Get list of available indices"""
        try:
            index_file = self.data_dir / "index_ohlcv.parquet"
            if not index_file.exists():
                return []
            
            con = duckdb.connect(':memory:')
            query = f"SELECT DISTINCT index_name FROM read_parquet('{index_file}') ORDER BY index_name"
            df = con.execute(query).df()
            con.close()
            return df['index_name'].tolist()
        except Exception as e:
            print(f"Error getting available indices: {e}")
            return []


# Example usage
if __name__ == "__main__":
    reader = NSEIndexReader()
    
    # Test 1: Get available indices
    print("Available Indices:", reader.get_available_indices())
    
    # Test 2: Get date range
    print("\nNifty 50 Date Range:", reader.get_date_range("nifty50"))
    
    # Test 3: Get historical data
    df = reader.get_index_data(
        index_name="nifty50",
        start_date="2024-01-01",
        end_date="2024-12-31"
    )
    
    if df is not None:
        print(f"\nNifty 50 data: {len(df)} records")
        print(df.head())
    else:
        print("\nNo data found for Nifty 50")
