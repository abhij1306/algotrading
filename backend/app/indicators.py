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
    
    # Calculate Advanced Indicators
    macd, macd_signal, macd_hist = calculate_macd(hist['Close'])
    hist['macd'] = macd
    hist['macd_signal'] = macd_signal
    hist['macd_hist'] = macd_hist
    
    stoch_k, stoch_d = calculate_stoch(hist['High'], hist['Low'], hist['Close'])
    hist['stoch_k'] = stoch_k
    hist['stoch_d'] = stoch_d
    
    bb_upper, bb_middle, bb_lower = calculate_bbands(hist['Close'])
    hist['bb_upper'] = bb_upper
    hist['bb_middle'] = bb_middle
    hist['bb_lower'] = bb_lower
    
    hist['adx'] = calculate_adx(hist['High'], hist['Low'], hist['Close'])
    hist['obv'] = calculate_obv(hist['Close'], hist['Volume'])

    # Update 'recent' after adding columns
    recent = hist.iloc[-1]

    # Additional Technical Indicators
    features['macd'] = float(recent['macd']) if not pd.isna(recent['macd']) else 0.0
    features['macd_signal'] = float(recent['macd_signal']) if not pd.isna(recent['macd_signal']) else 0.0
    
    features['stoch_k'] = float(recent['stoch_k']) if not pd.isna(recent['stoch_k']) else 0.0
    features['stoch_d'] = float(recent['stoch_d']) if not pd.isna(recent['stoch_d']) else 0.0
    
    features['bb_upper'] = float(recent['bb_upper']) if not pd.isna(recent['bb_upper']) else 0.0
    features['bb_middle'] = float(recent['bb_middle']) if not pd.isna(recent['bb_middle']) else 0.0
    features['bb_lower'] = float(recent['bb_lower']) if not pd.isna(recent['bb_lower']) else 0.0
    
    features['adx'] = float(recent['adx']) if not pd.isna(recent['adx']) else 0.0
    features['obv'] = int(recent['obv']) if not pd.isna(recent['obv']) else 0
    
    return features


# Aliases for backward compatibility
calculate_ema = ema
calculate_atr = atr
calculate_rsi = rsi


# --- Advanced Indicators ---

def calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Calculate MACD, Signal, and Histogram"""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calculate_bbands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """Calculate Bollinger Bands"""
    ma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = ma + (std * num_std)
    lower = ma - (std * num_std)
    return upper, ma, lower

def calculate_stoch(high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3):
    """Calculate Stochastic Oscillator"""
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    
    k = 100 * ((close - lowest_low) / (highest_high - lowest_low + 1e-9))
    d = k.rolling(window=d_period).mean()
    return k, d

def calculate_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14):
    """Calculate Average Directional Index (ADX)"""
    plus_dm = high.diff()
    minus_dm = low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm > 0] = 0
    
    tr1 = pd.DataFrame(high - low)
    tr2 = pd.DataFrame(abs(high - close.shift(1)))
    tr3 = pd.DataFrame(abs(low - close.shift(1)))
    frames = [tr1, tr2, tr3]
    tr = pd.concat(frames, axis=1, join='inner').max(axis=1)
    atr = tr.rolling(period).mean()
    
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = 100 * (abs(minus_dm).ewm(alpha=1/period).mean() / atr)
    dx = (abs(plus_di - minus_di) / abs(plus_di + minus_di)) * 100
    adx = dx.rolling(period).mean()
    return adx

def calculate_obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """Calculate On-Balance Volume"""
    obv = (np.sign(close.diff()) * volume).fillna(0).cumsum()
    return obv
