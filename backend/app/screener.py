"""
Core screening logic
"""

from .config import config
from .indicators import compute_features
from .scoring import rank_and_filter


def load_universe() -> list[str]:
    """Load NSE F&O universe using index_universe_loader"""
    try:
        from .services.index_universe_loader import index_universe_loader

        # Use NIFTY500 as the primary F&O universe (broadest index)
        # This covers most F&O-eligible stocks
        universe = index_universe_loader.get_index_symbols("NIFTY500")

        if universe:
            print(f"Loaded {len(universe)} stocks from NIFTY500 universe")
            return universe

        # Fallback to NIFTY50 if NIFTY500 is not available
        print("NIFTY500 not available, falling back to NIFTY50")
        universe = index_universe_loader.get_index_symbols("NIFTY50")
        if universe:
            print(f"Loaded {len(universe)} stocks from NIFTY50 universe")
            return universe

        raise ValueError("No index universe available")
    except Exception as e:
        print(f"Error loading universe: {e}")
        # Fallback to small test universe
        print("Using fallback test universe (5 stocks)")
        return ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK"]


def run_screener() -> dict:
    """
    Run the main screening logic

    Returns:
        Dictionary with intraday, swing, and combined lists
    """
    universe = load_universe()
    print(f"Screening {len(universe)} stocks...")

    all_features = []

    # Step 1: Fetch historical data for all symbols in bulk
    from .data_repository import DataRepository
    from .database import SessionLocal

    db = SessionLocal()
    repo = DataRepository(db)

    print(f"Fetching historical data for {len(universe)} stocks in bulk...")
    historical_data = repo.get_bulk_historical_prices(universe, days=200)
    db.close()

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
            hist.iloc[-1, hist.columns.get_loc("Close")] = fyers_data["ltp"]
            hist.iloc[-1, hist.columns.get_loc("Volume")] = fyers_data["volume"]
            hist.iloc[-1, hist.columns.get_loc("High")] = fyers_data["high"]
            hist.iloc[-1, hist.columns.get_loc("Low")] = fyers_data["low"]

        features = compute_features(symbol, hist)
        if features:
            # Add data source info
            features["data_source"] = "fyers" if symbol in fyers_quotes else "database"
            all_features.append(features)

    print(f"Successfully computed features for {len(all_features)} stocks")
    print(
        f"Real-time data: {sum(1 for f in all_features if f.get('data_source') == 'fyers')} stocks"
    )

    # Rank and filter for intraday - Top 50
    intraday = rank_and_filter(
        [f.copy() for f in all_features], "intraday", config.MIN_INTRADAY_SCORE
    )[:50]  # Top 50 intraday

    # Rank and filter for swing - Top 50
    swing = rank_and_filter([f.copy() for f in all_features], "swing", config.MIN_SWING_SCORE)[
        :50
    ]  # Top 50 swing

    # Combined list is now just for reference (top from both)
    combined = []
    seen = set()

    for item in intraday + swing:
        if item["symbol"] in seen:
            continue
        combined.append(item)
        seen.add(item["symbol"])
        if len(combined) >= config.MAX_TICKERS:
            break

    return {
        "intraday": intraday,
        "swing": swing,
        "combined": combined,
        "stats": {
            "total_screened": len(universe),
            "features_computed": len(all_features),
            "intraday_count": len(intraday),
            "swing_count": len(swing),
            "combined_count": len(combined),
            "fyers_realtime_count": sum(1 for f in all_features if f.get("data_source") == "fyers"),
        },
    }
