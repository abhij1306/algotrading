"""
Helper functions for NSE data fallback in trending scanner
"""

from datetime import datetime, timedelta
from typing import Dict, Optional
import pandas as pd


def get_nse_fallback_metrics(symbol: str, data_service) -> Optional[Dict]:
    """
    Get historical metrics from NSE data when Postgres data is missing
    
    Args:
        symbol: Stock symbol
        data_service: UnifiedDataService instance
    
    Returns:
        Dict with avg_volume, rsi, ema_20, ema_50, high_52w, or None
    """
    try:
        # Get last 365 days of data for 52W high
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        
        df = data_service.get_historical_data(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            intent="scanner"  # 1-minute cache TTL
        )
        
        if df is None or df.empty:
            return None
        
        # Calculate metrics from NSE data
        metrics = {}
        
        # 52W High
        metrics['high_52w'] = float(df['HIGH'].max())
        
        # Average Volume (last 20 days)
        if len(df) >= 20:
            metrics['avg_volume'] = int(df['VOLUME'].tail(20).mean())
        else:
            metrics['avg_volume'] = int(df['VOLUME'].mean())
        
        # Latest close
        metrics['hist_close'] = float(df['CLOSE'].iloc[-1])
        
        # Calculate EMAs if we have enough data
        if len(df) >= 50:
            # EMA 20
            ema_20 = df['CLOSE'].ewm(span=20, adjust=False).mean()
            metrics['ema_20'] = float(ema_20.iloc[-1])
            
            # EMA 50
            ema_50 = df['CLOSE'].ewm(span=50, adjust=False).mean()
            metrics['ema_50'] = float(ema_50.iloc[-1])
            
            # Simple RSI calculation
            delta = df['CLOSE'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            metrics['rsi'] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50
            
            # Volume percentile
            vol_percentile = (df['VOLUME'].iloc[-1] / df['VOLUME'].quantile(0.95)) * 100
            metrics['vol_percentile'] = min(float(vol_percentile), 100)
        else:
            # Not enough data for indicators
            metrics['ema_20'] = 0
            metrics['ema_50'] = 0
            metrics['rsi'] = 50
            metrics['vol_percentile'] = 50
        
        return metrics
        
    except Exception as e:
        print(f"NSE fallback error for {symbol}: {e}")
        return None


def get_nifty50_data(index_reader) -> Optional[Dict]:
    """
    Get latest Nifty 50 index data
    
    Args:
        index_reader: NSEIndexReader instance
    
    Returns:
        Dict with close, change_pct, or None
    """
    try:
        # Get last 2 days of Nifty 50 data
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        
        df = index_reader.get_index_data(
            index_name="nifty50",
            start_date=start_date,
            end_date=end_date
        )
        
        if df is None or df.empty or len(df) < 2:
            return None
        
        # Get latest and previous close (use lowercase column names from NSE reader)
        latest_close = float(df['close'].iloc[-1])
        prev_close = float(df['close'].iloc[-2])
        
        # Calculate change
        change_pct = ((latest_close - prev_close) / prev_close) * 100
        
        return {
            'close': latest_close,
            'change_pct': change_pct,
            'date': df['trade_date'].iloc[-1]
        }
        
    except Exception as e:
        print(f"Error getting Nifty 50 data: {e}")
        return None
