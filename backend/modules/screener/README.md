# Screener Module

## Overview
Advanced stock screening engine with technical and fundamental filters.

## Purpose
- Stock filtering by multiple criteria
- Technical indicator calculation (RSI, MACD, ADX, Stochastic, Bollinger Bands)
- Momentum and volume scoring
- Multi-timeframe analysis

## APIs Exposed

### GET `/api/screener/`
Main screener endpoint with filters.

**Query Parameters**:
- `page`, `limit` - Pagination
- `index` - Filter by index (NIFTY50, NIFTY500, etc.)
- `filter_type` - Preset filters (VOLUME_SHOCKER, PRICE_SHOCKER, 52W_HIGH, etc.)
- `symbol` - Search specific symbol
- `sector` - Filter by sector
- `view` - `technical` or `financial`

### GET `/api/screener/indices`
Get available indices for filtering.

## Dependencies
- Market Data Module (read-only for quotes)

## Internal Structure
```
screener/
├── services/
│   ├── screener.py        # Main screening logic
│   ├── scoring.py         # Intraday/swing scoring
│   ├── indicators.py      # Technical indicators
│   └── scanner_helpers.py # Helper functions
├── routers/
│   └── screener_routes.py
└── models/
    └── stock_data.py
```

## Owner
- TBD
- Last Updated: 2025-12-25
