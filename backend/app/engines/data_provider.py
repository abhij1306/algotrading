
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from ..database import IntradayCandle, Company, HistoricalPrice
import pandas as pd

logger = logging.getLogger(__name__)

class DataProvider:
    """
    Provides efficient OHLCV data for backtesting.
    """
    
    def __init__(self, db: Session):
        self.db = db

    def get_intraday_data(self, symbols: List[str], target_date: date, timeframe: int = 5) -> Dict[str, pd.DataFrame]:
        """
        Fetches 5-minute bars for a list of symbols on a specific date.
        Returns a dictionary mapping symbol to pandas DataFrame.
        """
        # Batch fetch for all symbols
        start_dt = datetime.combine(target_date, datetime.min.time())
        end_dt = datetime.combine(target_date, datetime.max.time())
        
        query = (
            self.db.query(IntradayCandle, Company.symbol)
            .join(Company, IntradayCandle.company_id == Company.id)
            .filter(Company.symbol.in_(symbols))
            .filter(IntradayCandle.timeframe == timeframe)
            .filter(IntradayCandle.timestamp >= start_dt)
            .filter(IntradayCandle.timestamp <= end_dt)
            .order_by(IntradayCandle.timestamp)
        )
        
        results = query.all()
        
        # Organize into DataFrames
        data_by_symbol = {}
        temp_data = {}
        
        for candle, symbol in results:
            if symbol not in temp_data:
                temp_data[symbol] = []
            
            temp_data[symbol].append({
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume
            })
            
        for symbol, bars in temp_data.items():
            df = pd.DataFrame(bars)
            df.set_index("timestamp", inplace=True)
            data_by_symbol[symbol] = df
            
        return data_by_symbol

    def get_daily_data(self, symbols: List[str], start_date: date, end_date: date) -> Dict[str, pd.DataFrame]:
        """
        Fetches daily OHLCV bars for a list of symbols over a date range.
        Returns a dictionary mapping symbol to pandas DataFrame.
        """
        if not symbols:
            logger.warning("get_daily_data called with empty symbols list")
            return {}
        
        try:
            query = (
                self.db.query(HistoricalPrice, Company.symbol)
                .join(Company, HistoricalPrice.company_id == Company.id)
                .filter(Company.symbol.in_(symbols))
                .filter(HistoricalPrice.date >= start_date)
                .filter(HistoricalPrice.date <= end_date)
                .order_by(HistoricalPrice.date)
            )
            
            results = query.all()
            
            if not results:
                logger.warning(f"No historical data found for {len(symbols)} symbols from {start_date} to {end_date}")
                return {}
            
            data_by_symbol = {}
            temp_data = {}
            
            for price, symbol in results:
                if symbol not in temp_data:
                    temp_data[symbol] = []
                
                # Validate data before adding
                if price.close is None or price.open is None:
                    logger.debug(f"Skipping {symbol} on {price.date} due to null prices")
                    continue
                
                temp_data[symbol].append({
                    "date": price.date,
                    "open": float(price.open),
                    "high": float(price.high) if price.high else float(price.close),
                    "low": float(price.low) if price.low else float(price.close),
                    "close": float(price.close),
                    "volume": int(price.volume) if price.volume else 0
                })
                
            for symbol, bars in temp_data.items():
                if not bars:
                    continue
                    
                df = pd.DataFrame(bars)
                df.set_index("date", inplace=True)
                data_by_symbol[symbol] = df
                
            logger.debug(f"Fetched data for {len(data_by_symbol)} symbols with {sum(len(df) for df in data_by_symbol.values())} total rows")
            return data_by_symbol
            
        except Exception as e:
            logger.error(f"Error fetching daily data: {str(e)}", exc_info=True)
            return {}

    def get_history(self, symbol: str, timeframe: str, end_date: date, days: int) -> pd.DataFrame:
        """
        Unified method to get historical data for a single symbol.
        Used by Nifty Strategies.
        """
        start_date = end_date - timedelta(days=days * 2) # Fetch extra to account for weekends/holidays
        # Trim later based on actual count if needed, but for now simple range
        
        if timeframe == "1D":
            data_dict = self.get_daily_data([symbol], start_date, end_date)
        else:
            # Fallback for intraday - simpler logic for now, assumes 'days' means calendar days back
            # Note: get_intraday_data takes a single target_date currently. 
            # If strategies need history of intraday, we might need a loop.
            # But Nifty strategies generally use 1D for logic or 5MIN for execution.
            # Let's support 1D primarily.
            logger.warning(f"get_history Not fully implemented for timeframe {timeframe}, falling back to daily logic or empty")
            return pd.DataFrame()
            
        df = data_dict.get(symbol, pd.DataFrame())
        
        # Ensure standard columns [Open, High, Low, Close, Volume]
        # Rename if necessary. get_daily_data returns lower case columns.
        if not df.empty:
            # Capitalize columns to match typical strategy expectations (Open, High, Low, Close, Volume)
            df.columns = [c.capitalize() for c in df.columns]
            
            # Slice to requested days (approx)
            return df.tail(days)
            
        return df
