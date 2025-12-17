# AlgoTrading Data Architecture

**Last Updated:** December 17, 2025  
**Version:** 2.0 (Post-Yahoo Finance Integration)

---

## Overview

The AlgoTrading system uses a **3-tier data architecture** to efficiently manage historical, operational, and real-time market data:

1. **Cold Layer** - Historical data (2016-present) in Parquet files
2. **Warm Layer** - Operational data (90-180 days) in PostgreSQL
3. **Hot Layer** - Real-time quotes via Fyers API

---

## Data Sources

### 1. NSE Bhavcopy (Historical - 2016-2023)
- **Source**: NSE Archives (https://archives.nseindia.com/)
- **Coverage**: 2016-01-01 to 2023-12-31
- **Format**: Daily CSV files (bhavcopy)
- **Storage**: Parquet files via DuckDB
- **Update Frequency**: Historical only (no updates)

### 2. Yahoo Finance (Gap Fill - 2024-Present)
- **Source**: Yahoo Finance API via `yfinance` library
- **Coverage**: 2024-01-01 to present
- **Data Types**: 
  - Equities: 2,426 NSE stocks (.NS suffix)
  - Indices: 10 major indices (^NSEI, ^NSEBANK, etc.)
- **Storage**: Merged into NSE Parquet files
- **Update Frequency**: Can be run daily to fill gaps

### 3. Fyers API (Real-time & Recent Historical)
- **Source**: Fyers broker API
- **Coverage**: 
  - Real-time quotes (live)
  - Historical: Last 365 days (daily), Last 100 days (intraday 5m)
- **Data Types**:
  - Daily OHLCV
  - Intraday candles (1m, 5m, 15m, 30m, 60m)
  - Live quotes (LTP, bid/ask)
- **Storage**: PostgreSQL `historical_prices` and `intraday_candles` tables
- **Update Frequency**: Real-time + daily batch updates

### 4. Screener.in (Fundamentals)
- **Source**: Manual Excel upload from Screener.in
- **Coverage**: Latest quarterly/annual financials
- **Data Types**:
  - Income statement
  - Balance sheet
  - Cash flow
  - Financial ratios
- **Storage**: PostgreSQL `financial_statements` table
- **Update Frequency**: Manual (quarterly)

### 5. NSE Corporate Actions
- **Source**: NSE website (manual CSV download)
- **Coverage**: Stock splits, bonuses, dividends
- **Storage**: PostgreSQL `corporate_actions` table
- **Usage**: Backward price adjustment for historical data

---

## Storage Architecture

### Cold Layer: Parquet Files (Historical Data)

**Location:** `c:\AlgoTrading\nse_data\processed\`

**Structure:**
```
nse_data/
├── raw/
│   ├── equities/           # Raw NSE bhavcopy CSVs (gitignored)
│   └── yahoo/
│       ├── equities/       # Yahoo equity CSVs
│       └── indices/        # Yahoo index CSVs
├── processed/
│   ├── equities_clean/
│   │   └── equity_ohlcv.parquet          # ⭐ Main equity data (NSE + Yahoo)
│   ├── indices_clean/
│   │   └── index_ohlcv.parquet           # ⭐ Main index data (NSE + Yahoo)
│   ├── adjusted_prices/
│   │   └── equity_ohlcv_adj.parquet      # Corporate action adjusted prices
│   └── backups/                          # Timestamped backups
└── metadata/
    ├── equity_list.csv                   # All NSE symbols
    ├── index_master.csv                  # Index metadata
    ├── symbol_sector_map.csv             # Sector classifications
    └── index_constituents/               # Index membership data
```

**Schema (equity_ohlcv.parquet):**
```
Columns:
- trade_date (DATE)         # Trading date
- symbol (STRING)           # Stock symbol (e.g., "RELIANCE")
- open (DOUBLE)             # Opening price
- high (DOUBLE)             # Highest price
- low (DOUBLE)              # Lowest price
- close (DOUBLE)            # Closing price
- volume (BIGINT)           # Trading volume
- turnover (DOUBLE)         # Total turnover
```

**Access Method:** DuckDB in-memory queries via `NSEDataReader`

**Coverage:**
- NSE Data: 2016-01-01 to 2023-12-31
- Yahoo Data: 2024-01-01 to present
- **Combined**: 2016-01-01 to present (seamless)

---

### Warm Layer: PostgreSQL (Operational Data)

**Database:** `algotrading` (PostgreSQL 14+)  
**Connection:** Via SQLAlchemy ORM

**Tables:**

#### 1. `companies`
Master table for all NSE companies
- **Columns**: id, symbol, name, sector, industry, market_cap, is_fno, is_active
- **Records**: ~2,426 active companies
- **Purpose**: Symbol lookup, filtering

#### 2. `historical_prices`
Daily OHLCV data from Fyers API
- **Columns**: id, company_id, date, open, high, low, close, volume, adj_close, source, created_at
- **Coverage**: Last 90-180 days (rolling window)
- **Source Tags**: `'fyers'` or `'yahoo'` (if backfilled)
- **Purpose**: Recent price data for live analysis

#### 3. `intraday_candles`
Intraday OHLCV candles for backtesting
- **Columns**: id, company_id, timestamp, timeframe, open, high, low, close, volume
- **Timeframes**: 1m, 5m, 15m, 30m, 60m
- **Coverage**: Last 100 days
- **Purpose**: Intraday strategy backtesting

#### 4. `financial_statements`
Quarterly/annual financial data
- **Columns**: id, company_id, period_end, period_type, revenue, net_income, eps, total_assets, debt_to_equity, roe, etc.
- **Source**: Screener.in Excel uploads
- **Purpose**: Fundamental analysis, screening

#### 5. `corporate_actions`
Stock splits, bonuses, dividends
- **Columns**: id, company_id, ex_date, action_type, ratio, adjustment_factor
- **Purpose**: Price adjustment calculations

#### 6. `smart_trader_signals`
AI-generated trading signals
- **Columns**: id, symbol, signal_type, confidence, entry_price, stop_loss, target, reasoning
- **Purpose**: Smart Trader system

**Auto-Cleanup:** Historical prices older than 180 days are automatically archived/deleted to keep database lean.

---

### Hot Layer: Fyers API (Real-time)

**Access:** Via `data_fetcher.py` and `fyers` package

**Capabilities:**
- Live quotes (LTP, volume, bid/ask)
- Real-time market depth
- Order execution (paper trading)
- Historical data fetch (last 365 days)

**Usage:**
- Live scanner updates
- Real-time P&L calculation
- Trade execution
- Latest price lookups

---

## Data Flow & Integration

### 1. Historical Data Query Flow

```
User Request
    ↓
UnifiedDataService.get_historical_data()
    ↓
┌─────────────────────────────────────┐
│ Date Range Analysis                 │
│ - Recent (< 90 days): Warm Layer    │
│ - Historical (> 90 days): Cold Layer│
│ - Real-time: Hot Layer              │
└─────────────────────────────────────┘
    ↓
┌──────────┬──────────┬──────────┐
│   Warm   │   Cold   │   Hot    │
│ (Postgres)│ (Parquet)│ (Fyers)  │
└──────────┴──────────┴──────────┘
    ↓
Merged DataFrame (pandas)
    ↓
Return to User
```

**Implementation:** `backend/app/unified_data_service.py`

### 2. Data Update Flow

#### Daily NSE Data Update (Automated)
```
1. Download latest bhavcopy → nse_data/raw/equities/
2. Clean & validate → nse_data_cleaner.py
3. Merge with Parquet → equity_ohlcv.parquet
4. Apply corporate actions → equity_ohlcv_adj.parquet
5. Update metadata → index_master.csv
```

#### Yahoo Finance Gap Fill (On-demand)
```
1. Download 2024-present → yahoo_downloader.py
2. Clean to NSE schema → yahoo_data_cleaner.py
3. Merge with existing Parquet → equity_ohlcv.parquet
4. Apply corporate actions → nse_adjust_prices.py
```

#### Fyers Data Update (Automated)
```
1. Fetch last 365 days → update_fyers_data.py
2. Save to Postgres → historical_prices table
3. Calculate indicators → indicators.py
4. Auto-cleanup old data (> 180 days)
```

---

## Module-wise Data Access

### 1. Stock Screener (`frontend/components/StockScreener.tsx`)
**Data Sources:**
- **PostgreSQL**: `companies`, `historical_prices`, `financial_statements`
- **API Endpoint**: `/api/screener`
- **Access Pattern**: Fetch all companies with latest price + financials

### 2. Portfolio Risk Analyzer (`backend/app/portfolio_risk.py`)
**Data Sources:**
- **Cold Layer**: Historical prices (1-3 years) via `NSEDataReader`
- **Warm Layer**: Recent prices via `DataRepository`
- **Nifty 50 Benchmark**: From `index_ohlcv.parquet`
- **Access Pattern**: Multi-symbol historical data for correlation/beta calculation

### 3. Strategy Backtester (`backend/app/strategies/backtest_engine.py`)
**Data Sources:**
- **Warm Layer**: Intraday candles (5m) from `intraday_candles` table
- **Fyers API**: Fetch missing intraday data
- **Access Pattern**: Sequential candle-by-candle processing

### 4. Smart Trader (`backend/app/smart_trader/orchestrator.py`)
**Data Sources:**
- **Hot Layer**: Live quotes from Fyers API
- **Warm Layer**: Recent 50-day history for indicator calculation
- **PostgreSQL**: Save signals to `smart_trader_signals` table
- **Access Pattern**: Real-time snapshot + historical context

### 5. Trending Scanner (`backend/app/trending_scanner.py`)
**Data Sources:**
- **Hot Layer**: Live LTP from Fyers API
- **Warm Layer**: Yesterday's close, 52-week high/low
- **Access Pattern**: Bulk quote fetch for all F&O stocks

### 6. NSE Data Reader (`backend/app/nse_data_reader.py`)
**Data Sources:**
- **Cold Layer**: Direct DuckDB queries on Parquet files
- **Access Pattern**: SQL-like queries for date ranges and symbols
- **Usage**: Historical backtesting, long-term analysis

---

## Data Validation & Quality

### OHLCV Constraints
All data must satisfy:
- `high >= low`
- `high >= open`
- `high >= close`
- `low <= open`
- `low <= close`
- `volume > 0`
- `turnover > 0`

**Validation Scripts:**
- `nse_data_validator.py` - Validates Parquet files
- `nse_validate_adjusted.py` - Validates corporate action adjustments

### Corporate Action Adjustments
- **Method**: Backward adjustment (industry standard)
- **Formula**: 
  - Adjusted Price = Raw Price / Adjustment Factor
  - Adjusted Volume = Raw Volume × Adjustment Factor
- **Storage**: Separate file `equity_ohlcv_adj.parquet`

---

## Performance Considerations

### Cold Layer (Parquet + DuckDB)
- **Read Speed**: ~100ms for 1 year of single symbol data
- **Storage**: ~500MB for 10 years of 2,400 symbols
- **Advantages**: 
  - No database overhead
  - Columnar compression
  - On-demand loading

### Warm Layer (PostgreSQL)
- **Query Speed**: ~50ms for recent data (indexed)
- **Storage**: ~200MB for 180 days of 2,400 symbols
- **Advantages**:
  - ACID compliance
  - Concurrent access
  - Relational queries

### Hot Layer (Fyers API)
- **Latency**: ~200-500ms per request
- **Rate Limits**: 
  - Historical: 1 req/sec
  - Live quotes: 10 req/sec
- **Advantages**:
  - Real-time data
  - No storage needed

---

## Backup & Disaster Recovery

### Automated Backups
- **Parquet Files**: Timestamped backups in `nse_data/processed/backups/`
- **PostgreSQL**: Daily pg_dump (configured separately)

### Recovery Procedures
1. **Parquet Corruption**: Restore from `backups/` directory
2. **PostgreSQL Failure**: Restore from pg_dump + re-fetch Fyers data
3. **Complete Data Loss**: Re-run entire NSE pipeline + Yahoo downloader

---

## Future Enhancements

1. **Real-time Parquet Updates**: Stream daily NSE data to Parquet
2. **Data Lake**: Migrate to cloud storage (S3/GCS) for scalability
3. **Caching Layer**: Redis for frequently accessed data
4. **Data Versioning**: Track schema changes and migrations
5. **Alternative Data**: Integrate news, sentiment, options chain

---

## Quick Reference

### Get Historical Data (Any Date Range)
```python
from backend.app.unified_data_service import get_data_service

service = get_data_service()
df = service.get_historical_data(
    symbol="RELIANCE",
    start_date="2020-01-01",
    end_date="2024-12-17",
    intent="backtest"  # or "analysis", "live"
)
```

### Get Live Quote
```python
from backend.app.data_fetcher import get_live_quote

quote = get_live_quote("RELIANCE")
print(f"LTP: {quote['ltp']}, Volume: {quote['volume']}")
```

### Query Parquet Directly
```python
from backend.app.nse_data_reader import NSEDataReader

reader = NSEDataReader()
df = reader.get_historical_data(
    symbol="RELIANCE",
    start_date="2024-01-01",
    end_date="2024-12-17"
)
```

### Get Financials
```python
from backend.app.database import SessionLocal
from backend.app.data_repository import DataRepository

db = SessionLocal()
repo = DataRepository(db)
financials = repo.get_latest_financials("RELIANCE")
db.close()
```

---

## Contact & Maintenance

**Data Pipeline Owner**: AlgoTrading Team  
**Last Major Update**: December 17, 2025 (Yahoo Finance Integration)  
**Next Review**: Quarterly (March 2026)

For issues or questions, refer to:
- `SESSION_HANDOFF.md` - Latest session notes
- `nse_data_readme.md` - NSE pipeline documentation
- `docs/` - Detailed technical documentation
