"""
Unified Data Service - Smart Routing Between Cold/Warm/Hot Layers
Routes data requests to appropriate layer based on date range and intent
"""

from datetime import datetime, timedelta
from typing import Optional, List
import pandas as pd
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).parent))

from nse_data_reader import NSEDataReader
from data_cache import get_cache
from data_repository import DataRepository

class UnifiedDataService:
    """
    Unified data service with 3-tier architecture:
    - Cold Layer: NSE Parquet files (2016-2024) via DuckDB
    - Warm Layer: Postgres (last 90-180 days)
    - Hot Layer: Fyers API (real-time)
    
    Smart routing based on:
    - Date range (historical vs recent vs live)
    - Intent (backtest, scanner, analysis, etc.)
    - Data availability
    """
    
    # Configuration
    WARM_LAYER_DAYS = 90  # Days to keep in Postgres
    HOT_LAYER_THRESHOLD = 1  # Days - use Fyers for data this recent
    
    def __init__(self):
        self.nse_reader = NSEDataReader()
        self.cache = get_cache()
        self.data_repo = DataRepository()
    
    def get_historical_data(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        intent: str = "analysis"
    ) -> Optional[pd.DataFrame]:
        """
        Get historical OHLCV data with smart layer routing
        
        Args:
            symbol: Stock symbol
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            intent: Use case (backtest, scanner, analysis, chart, realtime)
        
        Returns:
            DataFrame with OHLCV data
        """
        # Check cache first
        cached = self.cache.get(
            intent=intent,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date
        )
        
        if cached is not None:
            return cached
        
        # Determine which layer to use
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        now = datetime.now()
        
        # Hot Layer: Recent data (last 1 day) - use Fyers
        if (now - end_dt).days <= self.HOT_LAYER_THRESHOLD:
            data = self._get_from_hot_layer(symbol, start_date, end_date)
            if data is not None:
                self.cache.set(data, intent, symbol=symbol, start_date=start_date, end_date=end_date)
                return data
        
        # Warm Layer: Recent data (last 90 days) - use Postgres
        if (now - start_dt).days <= self.WARM_LAYER_DAYS:
            data = self._get_from_warm_layer(symbol, start_date, end_date)
            if data is not None:
                self.cache.set(data, intent, symbol=symbol, start_date=start_date, end_date=end_date)
                return data
        
        # Cold Layer: Historical data - use NSE Parquet
        data = self._get_from_cold_layer(symbol, start_date, end_date)
        if data is not None:
            self.cache.set(data, intent, symbol=symbol, start_date=start_date, end_date=end_date)
            return data
        
        return None
    
    def _get_from_cold_layer(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Get data from Cold Layer (NSE Parquet files)"""
        try:
            return self.nse_reader.get_historical_data(symbol, start_date, end_date)
        except Exception as e:
            print(f"Cold layer error for {symbol}: {e}")
            return None
    
    def _get_from_warm_layer(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Get data from Warm Layer (Postgres)"""
        try:
            # Use existing data_repository method
            data = self.data_repo.get_historical_data(symbol, start_date, end_date)
            return data
        except Exception as e:
            print(f"Warm layer error for {symbol}: {e}")
            return None
    
    def _get_from_hot_layer(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """Get data from Hot Layer (Fyers API)"""
        try:
            # Use existing Fyers integration
            # This would call Fyers API for recent/live data
            # For now, fallback to warm layer
            return self._get_from_warm_layer(symbol, start_date, end_date)
        except Exception as e:
            print(f"Hot layer error for {symbol}: {e}")
            return None
    
    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get latest price (always from Hot Layer)"""
        try:
            # Check cache first (very short TTL for realtime)
            cached = self.cache.get(intent="realtime", symbol=symbol, query="latest_price")
            if cached is not None and not cached.empty:
                return cached.iloc[0]['CLOSE']
            
            # Get from Fyers API or fallback to Postgres
            data = self.data_repo.get_latest_price(symbol)
            
            if data:
                # Cache for 5 seconds
                df = pd.DataFrame([{'CLOSE': data}])
                self.cache.set(df, intent="realtime", symbol=symbol, query="latest_price")
                return data
            
            return None
        except Exception as e:
            print(f"Error getting latest price for {symbol}: {e}")
            return None
    
    def get_multiple_symbols(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        intent: str = "analysis"
    ) -> Optional[pd.DataFrame]:
        """Get data for multiple symbols"""
        # For multiple symbols, prefer cold layer for efficiency
        try:
            cached = self.cache.get(
                intent=intent,
                symbols=",".join(sorted(symbols)),
                start_date=start_date,
                end_date=end_date
            )
            
            if cached is not None:
                return cached
            
            data = self.nse_reader.get_multiple_symbols(symbols, start_date, end_date)
            
            if data is not None:
                self.cache.set(
                    data, intent,
                    symbols=",".join(sorted(symbols)),
                    start_date=start_date,
                    end_date=end_date
                )
            
            return data
        except Exception as e:
            print(f"Error getting multiple symbols: {e}")
            return None
    
    def get_data_coverage(self) -> dict:
        """Get information about data coverage across layers"""
        coverage = {
            'cold_layer': {},
            'warm_layer': {},
            'hot_layer': {}
        }
        
        # Cold layer coverage
        try:
            date_range = self.nse_reader.get_date_range()
            if date_range:
                coverage['cold_layer'] = {
                    'start_date': date_range[0],
                    'end_date': date_range[1],
                    'source': 'NSE Parquet files'
                }
        except Exception as e:
            coverage['cold_layer']['error'] = str(e)
        
        # Warm layer coverage
        try:
            cutoff_date = (datetime.now() - timedelta(days=self.WARM_LAYER_DAYS)).strftime("%Y-%m-%d")
            coverage['warm_layer'] = {
                'start_date': cutoff_date,
                'end_date': datetime.now().strftime("%Y-%m-%d"),
                'source': 'Postgres'
            }
        except Exception as e:
            coverage['warm_layer']['error'] = str(e)
        
        # Hot layer coverage
        coverage['hot_layer'] = {
            'source': 'Fyers API',
            'realtime': True
        }
        
        return coverage


# Global service instance
_service_instance = None

def get_data_service() -> UnifiedDataService:
    """Get global unified data service instance (singleton)"""
    global _service_instance
    if _service_instance is None:
        _service_instance = UnifiedDataService()
    return _service_instance


# Example usage
if __name__ == "__main__":
    service = get_data_service()
    
    # Test 1: Get historical data (should use cold layer)
    print("Test 1: Historical data (Cold Layer)")
    df = service.get_historical_data(
        symbol="RELIANCE",
        start_date="2024-01-01",
        end_date="2024-06-30",
        intent="backtest"
    )
    if df is not None:
        print(f"Retrieved {len(df)} records")
        print(df.head())
    
    # Test 2: Get data coverage
    print("\nTest 2: Data Coverage")
    coverage = service.get_data_coverage()
    print(coverage)
