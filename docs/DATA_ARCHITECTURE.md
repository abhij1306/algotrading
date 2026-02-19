# SmartTrader 3.0 - Data Architecture

**Date:** February 13, 2026
**Status:** Active Development

---

## Core Data Model

The platform uses a **dual-universe data model**:

```
┌─────────────────────────────────────────────────────────────────┐
│                     STOCK UNIVERSE (DAILY)                       │
│  Source: NSE Bhavcopy (daily upload)                            │
│  Content: All listed stocks with OHLCV prices                   │
│  Frequency: Daily (market close ~4 PM)                          │
│  Storage: Company table + HistoricalPrice table                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Symbol Mapping
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   INDEX UNIVERSE (MONTHLY)                      │
│  Source: NSE CSV files (manual upload)                         │
│  Content: 33 indices with constituent symbols                  │
│  Frequency: Monthly (or as needed)                              │
│  Storage: CSV files in nse_data/index_universe/               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ Symbol Lifecycle
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   SYMBOL LIFECYCLE (HISTORICAL)                 │
│  Tracks symbol changes: mergers, renames, delistings           │
│  Enables accurate historical backtests                          │
│  Storage: symbol_lifecycle table                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Sources

### 1. NSE Bhavcopy (Daily - Stock Universe)

- **URL**: https://www.nseindia.com/ -> Capital Market -> Bhavcopy
- **Coverage**: All NSE listed stocks (~3200)
- **Format**: ZIP file containing CSV (CM - Cash Market)
- **Upload Endpoint**: `POST /api/upload/bhavcopy`
- **Frequency**: Every trading day (~4 PM IST)
- **Use Case**: Daily price updates, live screening

### 2. NSE Index Files (Monthly - Index Universe)

- **URLs**:
  - https://www.nseindia.com/ -> Indices -> Nifty 50 -> Constituents
  - https://www.niftyindices.com/ -> Resources -> Index Constituents
- **Coverage**: 33 indices
- **Format**: CSV with columns (Symbol, Company, Industry, Weightage)
- **Upload Endpoint**: `POST /api/universe/upload`
- **Frequency**: Monthly (or as index changes occur)
- **Use Case**: Index-based filtering in screener

### 3. Fyers API v3 (Real-time)

- **Coverage**: Live quotes, order placement
- **Use Case**: Real-time price ticks during market hours
- **Documentation**: `docs/FYERS_API_REFERENCE.md`

---

## Directory Structure

```
nse_data/
├── index_universe/
│   └── constituents/           # 33 index CSV files
│       ├── NIFTY50.csv         # 50 symbols
│       ├── NIFTY100.csv       # 100 symbols
│       ├── NIFTY500.csv       # 501 symbols
│       ├── NIFTYIT.csv        # Sector index
│       └── ...                # 33 total indices
│
├── bhavcopy/                  # Daily uploads (optional storage)
│   └── cm_YYYYMMDD.csv
│
└── processed/                  # Legacy - may be deprecated
    └── equity_ohlcv.parquet
```

---

## Data Flow

### Daily: Stock Universe Update
```
1. User downloads bhavcopy from NSE website
2. User uploads via /api/upload/bhavcopy
3. Backend parses CSV
4. For each symbol:
   - Upsert to Company table (if new)
   - Upsert to HistoricalPrice table (OHLCV + volume)
5. Response: {success: true, records_updated: N}
```

### Monthly: Index Universe Update
```
1. User downloads index CSV from NSE
2. User uploads via /api/universe/upload
3. Backend saves CSV to nse_data/index_universe/constituents/
4. index_universe_loader.load_all() rebuilds in-memory cache
5. Screener immediately uses new data
```

---

## Database Schema

### Core Tables

| Table | Purpose | Key Columns |
|-------|---------|--------------|
| `company` | Stock master data | symbol, company_name, is_active, listing_date |
| `historical_price` | Daily OHLCV + indicators | symbol, date, open, high, low, close, volume, rsi_14, ema_20, etc. |
| `symbol_lifecycle` | Historical symbol changes | old_symbol, new_symbol, event_date, event_type |
| `index_constituent_history` | Historical index composition | universe_id, symbol, effective_from, effective_to |

### Key Indexes

```sql
-- Company lookups
CREATE INDEX idx_company_symbol ON company(symbol);
CREATE INDEX idx_company_active ON company(is_active);

-- Historical price lookups
CREATE INDEX idx_price_symbol_date ON historical_price(symbol, date DESC);
CREATE INDEX idx_price_date ON historical_price(date);

-- Symbol lifecycle
CREATE INDEX idx_lifecycle_old ON symbol_lifecycle(old_symbol, event_date);
CREATE INDEX idx_lifecycle_new ON symbol_lifecycle(new_symbol, event_date);
```

---

## Key Services

### Index Universe Loader

```python
from app.services.index_universe_loader import index_universe_loader

# Load all indices from CSV files
index_universe_loader.load_all()

# Get symbols for specific index
symbols = index_universe_loader.get_index_symbols('NIFTY500')
# Returns: ['SBIN', 'RELIANCE', 'HDFCBANK', ...] - 501 symbols

# Get index metadata
info = index_universe_loader.get_index_info('NIFTY500')
# Returns: {'name': 'NIFTY 500', 'symbol_count': 501}
```

### Symbol Lifecycle

```python
from app.services.symbol_lifecycle import SymbolLifecycleService

service = SymbolLifecycleService(db)

# Resolve historical symbol
# On 2007-01-01, RELIANCE was RPL (pre-merger)
historical = service.resolve_symbol('RELIANCE', date(2007, 1, 1))
# Returns: 'RPL'

# Get all changes for a symbol
changes = service.get_history('SBIN')
# Returns: [{'date': '1995-01-01', 'old': 'SBI', 'new': 'SBIN', 'type': 'RENAME'}]
```

---

## API Endpoints

### Upload
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/upload/bhavcopy` | POST | Upload daily bhavcopy CSV |
| `/api/universe/upload` | POST | Upload index constituents CSV |

### Screener
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/screener/stocks` | GET | Get stocks with filtering |
| `/api/screener/indices` | GET | Get all available indices |

### Universe
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/universe/list` | GET | List available indices |
| `/api/universe/{index}` | GET | Get index constituents |

---

## Data Coverage

### Current Status (February 2026)

| Data Type | Status | Notes |
|-----------|--------|-------|
| Stock Universe | Partial | ~760 stocks categorized, needs complete bhavcopy |
| Index Universe | Ready | 33 indices loaded from CSV |
| Historical Data | Partial | Gaps exist, needs completion |
| Symbol Lifecycle | Partial | Needs comprehensive historical tracking |

### Required for Backtests

1. **Complete historical stock data** - Fill gaps in HistoricalPrice
2. **Index universe history** - Store monthly snapshots for date-based lookups

---

## Scripts

### Daily Update
```bash
# Run bhavcopy upload
# 1. Download from https://www.nseindia.com/product/equities/eq/security-description?symbol=ALL
# 2. Upload via API: POST /api/upload/bhavcopy
```

### Monthly Update
```bash
# Run index update
# 1. Download CSV from NSE website for each index
# 2. Upload via API: POST /api/universe/upload
```

---

## Future Enhancements

### Phase 1: Historical Index Storage
- Store monthly index snapshots in database
- Enable: `SELECT * FROM index_membership WHERE index_code='NIFTY50' AND date = '2020-01-01'`

### Phase 2: Corporate Actions
- Track splits, bonuses, dividends
- Generate adjusted price time series

### Phase 3: Multi-Broker
- Add Dhan API support
- Unified broker abstraction

---

**Last Updated:** February 13, 2026
