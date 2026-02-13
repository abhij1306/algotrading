# AlgoTrading Platform - System Architecture

**Version:** 2.0
**Date:** February 10, 2026
**Status:** Post-Organization

## Overview

The AlgoTrading platform is organized into 5 clear layers with strict boundaries.

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
│  │ - validate()     │  │ - Throttles ticks              │  │
│  └──────────────────┘  └────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ Fyers WebSocket  │  │ Indicator Engine               │  │
│  │ - Connects       │  │ - Calculates RSI, MACD, etc.   │  │
│  │ - Subscribe      │  │ - Stores to DB                 │  │
│  └──────────────────┘  └────────────────────────────────┘  │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                   DATA PLATFORM                              │
│  Pipelines | Processors | Validators                        │
│  - Daily updates | Audit | Backfill | Health checks         │
│  Writes to: nse_data/ | Loads to: PostgreSQL               │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              DATA STORAGE (Read-Only)                        │
│  ┌──────────────────┐  ┌────────────────────────────────┐  │
│  │ PostgreSQL       │  │ nse_data/processed/            │  │
│  │ - companies      │  │ - equity_ohlcv_master.parquet  │  │
│  │ - historical_prices│ - index_ohlcv_master.parquet   │  │
│  │ Stores: DB_FORMAT│  │ Stores: DB_FORMAT              │  │
│  └──────────────────┘  └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
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

### Live Market Service (`backend/app/services/live_market_service.py`)
- Manages Fyers WebSocket connection
- Converts symbols at API boundaries
- Throttles tick data (1-second batching)
- Broadcasts to frontend clients

### Data Platform (`data_platform/`)
- ETL pipelines for market data
- Scheduled data updates
- Data quality validation
- Master store building

## Critical Rules

1. **Symbol Storage:** Always use DB_FORMAT in database and parquet files
2. **Symbol Display:** Always show DB_FORMAT to users
3. **Symbol API:** Always use FYERS_FORMAT when calling Fyers API
4. **Symbol Conversion:** Always use `symbol_master` service, never ad-hoc
