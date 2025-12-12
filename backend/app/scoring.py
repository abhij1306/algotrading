"""
Scoring algorithms for intraday and swing trading
"""

# F&O universe for futures preference logic
FUTURES_UNIVERSE = {
    'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'INFY', 'TCS', 'SBIN', 'AXISBANK', 'KOTAKBANK',
    'HINDUNILVR', 'LT', 'BAJFINANCE', 'BHARTIARTL', 'MARUTI', 'M&M', 'ULTRACEMCO', 'ITC',
    'ASIANPAINT', 'SUNPHARMA', 'WIPRO', 'HCLTECH', 'TITAN', 'NESTLEIND', 'BAJAJFINSV',
    'TECHM', 'POWERGRID', 'NTPC', 'TATAMOTORS', 'TATASTEEL', 'ONGC', 'ADANIENT'
}

def score_swing(features: dict) -> float:
    """
    Calculate swing trading score (0-100)
    
    Scoring breakdown:
    - Breakout trigger: 40 points
    - Trend alignment: 25 points (15 + 10)
    - Volume confirmation: 20 points
    - Volatility fit: 15 points
    
    Args:
        features: Dictionary of technical features
        
    Returns:
        Score between 0 and 100
    """
    score = 0.0
    
    # Trigger strength: 20-day breakout
    if features.get('is_20d_breakout'):
        score += 40
    
    # Trend alignment
    if features.get('price_above_ema50'):
        score += 15
    if features.get('ema20_above_50'):
        score += 10
    
    # Volume confirmation (high percentile = strong interest)
    vol_pct = features.get('vol_percentile', 0)
    if vol_pct >= 75:
        score += 20
    elif vol_pct >= 50:
        score += 10
    
    # Volatility fit (ATR% in sweet spot for swing trading)
    atr_pct = features.get('atr_pct', 0)
    if 1.0 <= atr_pct <= 5.0:
        score += 15
    elif 0.5 <= atr_pct <= 7.0:
        score += 7
    
    return min(100.0, score)

def score_intraday(features: dict) -> float:
    """
    Calculate intraday trading score (0-100)
    
    Scoring breakdown:
    - Volume surge: 30 points
    - Breakout: 25 points
    - Trend: 15 points
    - Tight volatility: 30 points
    
    Args:
        features: Dictionary of technical features
        
    Returns:
        Score between 0 and 100
    """
    score = 0.0
    
    # Volume surge (critical for intraday)
    vol_pct = features.get('vol_percentile', 0)
    if vol_pct >= 80:
        score += 30
    elif vol_pct >= 60:
        score += 15
    
    # Breakout trigger
    if features.get('is_20d_breakout'):
        score += 25
    
    # Trend alignment
    if features.get('price_above_ema50'):
        score += 15
    
    # Tighter volatility for intraday (lower ATR% preferred)
    atr_pct = features.get('atr_pct', 0)
    if 0.8 <= atr_pct <= 3.0:
        score += 30
    elif 0.5 <= atr_pct <= 4.0:
        score += 15
    
    return min(100.0, score)

def apply_futures_preference(features: dict, score: float) -> float:
    """
    Apply futures preference logic
    
    - F&O stocks: use default score
    - Non-F&O stocks: only include if strong trigger (breakout + high volume)
    
    Args:
        features: Dictionary of technical features
        score: Base score
        
    Returns:
        Adjusted score
    """
    symbol = features.get('symbol', '')
    
    # If in futures universe, return original score
    if symbol in FUTURES_UNIVERSE:
        return score
    
    # Non-futures stock: require strong trigger
    has_breakout = features.get('is_20d_breakout', False)
    high_volume = features.get('vol_percentile', 0) >= 80
    
    if has_breakout and high_volume:
        return score
    else:
        # Penalize non-futures stocks without strong triggers
        return score * 0.5

def rank_and_filter(features_list: list, score_type: str, min_score: float) -> list:
    """
    Rank stocks by score and filter by minimum threshold
    
    Args:
        features_list: List of feature dictionaries
        score_type: 'intraday' or 'swing'
        min_score: Minimum score threshold
        
    Returns:
        Filtered and sorted list
    """
    scored = []
    
    for features in features_list:
        if score_type == 'intraday':
            score = score_intraday(features)
            features['intraday_score'] = score
        else:
            score = score_swing(features)
            features['swing_score'] = score
        
        # Apply futures preference
        adjusted_score = apply_futures_preference(features, score)
        
        if adjusted_score >= min_score:
            features[f'{score_type}_score_adjusted'] = adjusted_score
            scored.append(features)
    
    # Sort by adjusted score
    return sorted(scored, key=lambda x: x.get(f'{score_type}_score_adjusted', 0), reverse=True)
