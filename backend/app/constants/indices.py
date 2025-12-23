"""
Indices and Universe Constants
Defines stock universe filters for Screener
"""

# Index Universes
STOCK_INDICES = {
    "NIFTY50": {
        "name": "NIFTY 50",
        "description": "Top 50 liquid stocks",
        "symbols": [
            "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
            "HDFC", "BHARTIARTL", "ITC", "KOTAKBANK", "LT",
            "SBIN", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
            "BAJFINANCE", "WIPRO", "ULTRACEMCO", "NESTLEIND", "ONGC",
            "SUNPHARMA", "TATASTEEL", "M&M", "NTPC", "POWERGRID",
            "JSWSTEEL", "GRASIM", "COALINDIA", "TATAMOTORS", "INDUSINDBK",
            "TECHM", "ADANIPORTS", "HINDALCO", "HCLTECH", "UPL",
            "HEROMOTOCO", "BAJAJFINSV", "DRREDDY", "CIPLA", "EICHERMOT",
            "DIVISLAB", "BRITANNIA", "SHREECEM", "BPCL", "TATACONSUM",
            "APOLLOHOSP", "ADANIENT", "SBILIFE", "HDFCLIFE", "BAJAJ-AUTO"
        ]
    },
    "BANKNIFTY": {
        "name": "BANK NIFTY",
        "description": "Banking sector index",
        "symbols": [
            "HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN", "AXISBANK",
            "INDUSINDBK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "AUBANK",
            "PNB", "BANKBARODA"
        ]
    },
    "NIFTY100": {
        "name": "NIFTY 100",
        "description": "Top 100 stocks",
        "count": 100
    },
    "NIFTY200": {
        "name": "NIFTY 200",
        "description": "Top 200 stocks for trend filters",
        "count": 200
    },
    "MIDCAPNIFTY": {
        "name": "MIDCAP NIFTY",
        "description": "Mid-cap index",
        "count": 150
    },
    "SMALLCAPNIFTY": {
        "name": "SMALLCAP NIFTY",
        "description": "Small-cap index",
        "count": 250
    },
    "ALL": {
        "name": "All Stocks",
        "description": "No filter",
        "symbols": []  # Empty means all
    }
}

# Trend filter universe (NIFTY200 only)
TREND_FILTER_UNIVERSE = "NIFTY200"

# Default universe for Screener
DEFAULT_SCREENER_UNIVERSE = "NIFTY50"
