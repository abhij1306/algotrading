"""
Technical indicators module
Implements EMA, ATR, RSI, and z-score calculations
"""
import pandas as pd
import numpy as np

def ema(series: pd.Series, span: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return series.ewm(span=span, adjust=False).mean()

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Calculate Average True Range"""
    high = df['High']
    low = df['Low']
    close = df['Close']
    
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """Calculate Relative Strength Index"""
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    
    ma_up = up.ewm(alpha=1/n, adjust=False).mean()
    ma_down = down.ewm(alpha=1/n, adjust=False).mean()
    
    rs = ma_up / (ma_down + 1e-9)
    return 100 - (100 / (1 + rs))

def zscore(value: float, series: pd.Series) -> float:
    """Calculate z-score deviation from series mean"""
    mu = series.mean()
    sigma = series.std(ddof=0) if series.std(ddof=0) > 0 else 1
    return (value - mu) / sigma

def compute_features(symbol: str, hist: pd.DataFrame) -> dict:
    """
    Compute all technical features for a symbol
    
    Args:
        symbol: Stock symbol
        hist: Historical OHLCV dataframe
        
    Returns:
        Dictionary of features or None if insufficient data
    """
    if hist.empty or len(hist) < 20:  # Reduced from 60 to 20 days
        return None
    
    hist = hist.copy()
    
    # Normalize column names to uppercase (database returns lowercase)
    hist.columns = [col.capitalize() if col.lower() in ['open', 'high', 'low', 'close', 'volume'] else col for col in hist.columns]
    
    hist.index = pd.to_datetime(hist.index)
    hist = hist.dropna()
    
    # Calculate indicators
    hist['EMA20'] = ema(hist['Close'], 20)
    hist['EMA34'] = ema(hist['Close'], 34)
    hist['EMA50'] = ema(hist['Close'], 50)
    hist['ATR14'] = atr(hist, 14)
    hist['RSI14'] = rsi(hist['Close'], 14)
    
    recent = hist.iloc[-1]
    lookback = hist['Close']
    
    features = {
        'symbol': symbol,
        'close': float(recent['Close']),
        'ema20': float(recent['EMA20']),
        'ema34': float(recent['EMA34']),
        'ema50': float(recent['EMA50']),
        'atr': float(recent['ATR14']),
        'rsi': float(recent['RSI14']),
    }
    
    # ATR percentage
    features['atr_pct'] = (features['atr'] / features['close']) * 100
    
    # Volume metrics
    vol_series = hist['Volume']
    features['adv20'] = int(vol_series.tail(20).mean())
    features['volume'] = int(recent['Volume'])
    features['vol_percentile'] = float((vol_series.rank(pct=True).iloc[-1]) * 100)
    
    # Deviation metrics
    features['z_close'] = float(zscore(features['close'], lookback))
    features['z_atr_pct'] = float(zscore(features['atr_pct'], hist['Close'].pct_change().abs().dropna()))
    
    # Trend flags
    features['price_above_ema50'] = bool(features['close'] > features['ema50'])
    features['ema20_above_50'] = bool(features['ema20'] > features['ema50'])
    
    # Breakout detection
    features['20d_high'] = float(hist['Close'].rolling(20).max().iloc[-1])
    features['is_20d_breakout'] = features['close'] >= features['20d_high']

    # Trend Metrics (User Requested)
    # 7D Trend (~5 trading days)
    features['trend_7d'] = float(hist['Close'].pct_change(5).iloc[-1] * 100) if len(hist) >= 6 else 0.0
    # 30D Trend (~21 trading days)
    features['trend_30d'] = float(hist['Close'].pct_change(21).iloc[-1] * 100) if len(hist) >= 22 else 0.0
    
    return features
