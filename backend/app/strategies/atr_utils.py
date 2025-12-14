"""
Technical Indicators and ATR Calculation
"""

import pandas as pd
import numpy as np


def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Calculate Average True Range (ATR)
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ATR period (default: 14)
    
    Returns:
        Current ATR value
    """
    if len(df) < period:
        # Fallback if not enough data
        return (df['high'] - df['low']).mean()
    
    # Calculate True Range
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    
    # Calculate ATR (Exponential Moving Average of TR)
    atr = tr.rolling(window=period).mean()
    
    # Return the most recent ATR value
    current_atr = atr.iloc[-1]
    
    return current_atr if not pd.isna(current_atr) else (df['high'] - df['low']).mean()


def calculate_atr_percentage(df: pd.DataFrame, period: int = 14) -> float:
    """
    Calculate ATR as a percentage of current price
    
    Args:
        df: DataFrame with OHLC data
        period: ATR period
        
    Returns:
        ATR as percentage of current close price
    """
    atr = calculate_atr(df, period)
    current_price = df['close'].iloc[-1]
    
    if current_price == 0:
        return 0
        
    return (atr / current_price) * 100
