# Market Data Module

## Overview
Centralized market data service providing real-time and historical data for all modules.

## Purpose
- NSE data ingestion
- Fyers broker integration  
- Data caching and optimization
- Search and quote APIs

## APIs Exposed

### GET `/api/market/overview`
Returns dashboard market overview with indices, sentiment, and condition.

### GET `/api/market/search?query={symbol}`
Search for stocks/indices by symbol or name.

### GET `/api/market/quote/{symbol}`
Get latest quote for a symbol.

### GET `/api/market/indices`
Get list of available indices.

### GET `/api/market/sectors`
Get list of available sectors.

## Dependencies
- None (foundational module)

## Consumed By
- Screener Module
- Analyst Module
- Quant Module
- Trader Module
- Portfolio Module

## Internal Structure
```
market-data/
├── services/
│   ├── nse_data_reader.py     # NSE data fetching
│   ├── fyers_direct.py         # Fyers integration
│   ├── data_fetcher.py         # Unified data interface
│   └── cache_manager.py        # Caching layer
├── routers/
│   └── market_routes.py        # FastAPI endpoints
├── models/
│   └── market_data.py          # Pydantic models
└── tests/
    └── test_market_data.py
```

## Development
- Owner: TBD
- Status: In Progress
- Last Updated: 2025-12-25
