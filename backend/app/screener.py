"""
Core screening logic
"""
import json
import os
from pathlib import Path
from typing import List, Dict
from .data_fetcher import fetch_historical_data
from .indicators import compute_features
from .scoring import rank_and_filter
from .config import config

def load_universe() -> List[str]:
    """Load NSE F&O universe from JSON file"""
    try:
        # Get the directory where this file is located
        current_dir = Path(__file__).parent.parent
        
        # Use full F&O universe (200 stocks)
        universe_path = current_dir / 'data' / 'nse_fno_universe.json'
        
        print(f"Loading universe from: {universe_path}")
        
        with open(universe_path, 'r') as f:
            universe = json.load(f)
            print(f"Loaded {len(universe)} stocks from universe file")
            return universe
    except Exception as e:
        print(f"Error loading universe: {e}")
        # Fallback to small test universe
        print("Using fallback test universe (5 stocks)")
        return ['TCS', 'INFY', 'RELIANCE', 'HDFCBANK', 'ICICIBANK']

def run_screener() -> Dict:
    """
    Run the main screening logic
    
    Returns:
        Dictionary with intraday, swing, and combined lists
    """
    universe = load_universe()
    print(f"Screening {len(universe)} stocks...")
    
    all_features = []
    
    # Step 1: Fetch historical data for all symbols (for indicators)
    historical_data = {}
    for i, symbol in enumerate(universe):
        if (i + 1) % 10 == 0:
            print(f"Fetching historical data: {i + 1}/{len(universe)} stocks...")
        
        hist = fetch_historical_data(symbol)
        if hist is not None:
            historical_data[symbol] = hist
    
    print(f"Historical data fetched for {len(historical_data)} stocks")
    
    # Step 2: Get real-time quotes from Fyers (batch request)
    fyers_quotes = {}
    if config.HAS_FYERS:
        try:
            print("Fetching real-time quotes from Fyers...")
            from .data_fetcher import fetch_fyers_quotes
            fyers_quotes = fetch_fyers_quotes(list(historical_data.keys()))
            print(f"Got real-time quotes for {len(fyers_quotes)} stocks from Fyers")
        except Exception as e:
            print(f"Fyers quotes failed: {e}")
    
    # Step 3: Compute features combining historical + real-time data
    for symbol, hist in historical_data.items():
        # Update latest price with Fyers real-time if available
        if symbol in fyers_quotes:
            fyers_data = fyers_quotes[symbol]
            # Update the last row with real-time data
            hist.iloc[-1, hist.columns.get_loc('Close')] = fyers_data['ltp']
            hist.iloc[-1, hist.columns.get_loc('Volume')] = fyers_data['volume']
            hist.iloc[-1, hist.columns.get_loc('High')] = fyers_data['high']
            hist.iloc[-1, hist.columns.get_loc('Low')] = fyers_data['low']
        
        features = compute_features(symbol, hist)
        if features:
            # Add data source info
            features['data_source'] = 'fyers' if symbol in fyers_quotes else 'database'
            all_features.append(features)
    
    print(f"Successfully computed features for {len(all_features)} stocks")
    print(f"Real-time data: {sum(1 for f in all_features if f.get('data_source') == 'fyers')} stocks")
    
    # Rank and filter for intraday - Top 50
    intraday = rank_and_filter(
        [f.copy() for f in all_features],
        'intraday',
        config.MIN_INTRADAY_SCORE
    )[:50]  # Top 50 intraday
    
    # Rank and filter for swing - Top 50
    swing = rank_and_filter(
        [f.copy() for f in all_features],
        'swing',
        config.MIN_SWING_SCORE
    )[:50]  # Top 50 swing
    
    # Combined list is now just for reference (top from both)
    combined = []
    seen = set()
    
    for item in intraday + swing:
        if item['symbol'] in seen:
            continue
        combined.append(item)
        seen.add(item['symbol'])
        if len(combined) >= config.MAX_TICKERS:
            break
    
    return {
        'intraday': intraday,
        'swing': swing,
        'combined': combined,
        'stats': {
            'total_screened': len(universe),
            'features_computed': len(all_features),
            'intraday_count': len(intraday),
            'swing_count': len(swing),
            'combined_count': len(combined),
            'fyers_realtime_count': sum(1 for f in all_features if f.get('data_source') == 'fyers')
        }
    }
