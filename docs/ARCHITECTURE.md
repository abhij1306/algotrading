# SmartTrader 3.0 - System Architecture

**Version:** 3.0
**Date:** February 13, 2026
**Status:** Active Development

---

## Overview

SmartTrader 3.0 is organized into 5 clear layers with strict boundaries. The platform follows a **dual-universe data model**:

- **Stock Universe (Daily):** NSE Bhavcopy uploads → Company + HistoricalPrice tables
- **Index Universe (Monthly):** NSE CSV uploads → index_universe_loader (33 indices)
- **Symbol Lifecycle:** Tracks mergers, renames, delistings for historical accuracy

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        FRONTEND                              │
│  Next.js | React | TypeScript | WebSocket Client            │
│  Symbols: DB_FORMAT (SBIN) - Display                        │
│  Subscribes: DB_FORMAT → Backend converts → FYERS_FORMAT    │
└────────────────────┬────────────────────────────────────────┘
                      │ HTTP/WebSocket
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                      API LAYER                               │
│  FastAPI Routers | Request Validation | Symbol Conversion   │
│  Input: DB_FORMAT | Output: DB_FORMAT                       │
└────────────────────┬────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   BACKEND SERVICES                           │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ Symbol Master    │  │ Live Market Service            │  │
│  │ - to_fyers()     │  │ - Converts symbols             │  │
│  │ - to_db()        │  │ - Manages subscriptions        │  │
│  │ - validate()      │  │ - Throttles ticks             │  │
│  └──────────────────┘  └────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ Index Universe   │  │ Symbol Lifecycle              │  │
│  │ Loader (CSV)     │  │ - Mergers, renames            │  │
│  │ - 33 indices     │  │ - Historical resolution       │  │
│  └──────────────────┘  └────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│                   ENGINES LAYER                              │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ Backtest Engine  │  │ Portfolio Constructor         │  │
│  │ - Historical     │  │ - Multi-strategy               │  │
│  │ - No survivorship│ │ - Risk-based allocation        │  │
│  └──────────────────┘  └────────────────────────────────┘  │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ Strategy Executor│  │ Risk State Engine             │  │
│  │ - Date iteration │  │ - Regime detection            │  │
│  │ - Universe lookup│  │ - Threshold management        │  │
│  └──────────────────┘  └────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              DATA STORAGE (Read-Only)                        │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ PostgreSQL       │  │ nse_data/index_universe/       │  │
│  │ - companies      │  │ - CSV files (33 indices)      │  │
│  │ - historical_prices│ - NIFTY50.csv, NIFTY500.csv   │  │
│  │ - symbol_lifecycle│                                │  │
│  │ Stores: DB_FORMAT│  │                                │  │
│  └──────────────────┘  └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## Data Flow

### Daily Flow (Stock Universe)
```
User uploads bhavcopy.csv (daily)
         │
         ▼
/api/upload/bhavcopy endpoint
         │
         ▼
Company + HistoricalPrice tables updated
```

### Monthly Flow (Index Universe)
```
User uploads index CSV (monthly)
         │
         ▼
/api/universe/upload endpoint
         │
         ▼
CSV saved to nse_data/index_universe/
         │
         ▼
index_universe_loader.load_all() loads into memory
         │
         ▼
Screener queries: get_index_symbols('NIFTY500')
```

### Backtest Flow
```
User requests backtest
         │
         ▼
Strategy Executor validates contracts
         │
         ▼
For each day:
  1. Get universe (index_universe_loader)
  2. Resolve symbols (symbol_lifecycle)
  3. Fetch data (HistoricalPrice)
  4. Run strategy
         │
         ▼
Results saved to backtest_daily_results
```

## Symbol Format Flow

```
User enters: "SBIN"
    ↓
Frontend displays: "SBIN" (DB_FORMAT)
    ↓
User clicks subscribe
    ↓
Frontend sends WebSocket: {"action": "subscribe", "symbols": ["SBIN"]}
    ↓
Backend receives: ["SBIN"]
    ↓
Symbol Master converts: ["SBIN"] → ["NSE:SBIN-EQ"]
    ↓
Fyers WebSocket subscribes: ["NSE:SBIN-EQ"]
    ↓
Fyers sends tick: {"symbol": "NSE:SBIN-EQ", "ltp": 500.50}
    ↓
Symbol Master converts: "NSE:SBIN-EQ" → "SBIN"
    ↓
Backend broadcasts: {"symbol": "SBIN", "ltp": 500.50}
    ↓
Frontend updates: Shows "SBIN" with new price
```

## Key Components

### Symbol Master (`backend/app/services/symbol_master.py`)
- **Single source of truth** for symbol formats
- Handles all conversions bidirectionally
- Validates symbol formats
- Caches symbol information

### Index Universe Loader (`backend/app/services/index_universe_loader.py`)
- Loads 33 indices from CSV files
- Located in: `nse_data/index_universe/constituents/`
- Methods: `load_all()`, `get_index_symbols(index)`, `get_index_info(index)`

### Symbol Lifecycle (`backend/app/services/symbol_lifecycle.py`)
- Tracks symbol changes over time
- Events: MERGER, RENAME, DELISTING
- Enables historical backtests with no survivorship bias

### Live Market Service (`backend/app/services/live_market_service.py`)
- Manages Fyers WebSocket connection
- Converts symbols at API boundaries
- Throttles tick data (1-second batching)
- Broadcasts to frontend clients

### Universe Service (`backend/app/services/universe/universe_service.py`)
- Orchestrates universe management
- Bridges database + CSV data sources

## Critical Rules

1. **Symbol Storage:** Always use DB_FORMAT in database and parquet files
2. **Symbol Display:** Always show DB_FORMAT to users
3. **Symbol API:** Always use FYERS_FORMAT when calling Fyers API
4. **Symbol Conversion:** Always use `symbol_master` service, never ad-hoc
5. **Index Filtering:** Use `index_universe_loader` (CSV), not database columns
6. **Historical Backtests:** Always resolve symbols via `symbol_lifecycle`

## API Routers

| Router | Prefix | Purpose |
|--------|--------|---------|
| `/api/screener` | | Stock screening with index filtering |
| `/api/universe` | | Index CSV upload & management |
| `/api/upload` | | Bhavcopy daily upload |
| `/api/backtest` | | Strategy backtesting |
| `/api/portfolio` | | Portfolio management |
| `/api/market` | | Market data queries |
| `/api/websocket` | | Real-time data stream |

## Future Capabilities

- Historical index composition storage (for date-based lookups)
- Corporate actions tracking (splits, bonuses, dividends)
- Multi-broker support (Fyers, Dhan)
- Event-based backtests
- Monte Carlo simulations

---

**Last Updated:** February 13, 2026
