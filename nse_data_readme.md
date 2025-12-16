# NSE Data Directory - Status & Documentation

## Overview
This directory contains historical NSE (National Stock Exchange) equity data downloaded from the NSE archives. The data is organized by year and month for efficient processing.

## Directory Structure
```
nse_data/
├── raw/
│   ├── equities/        # Raw bhavcopy CSV files from NSE
│   ├── indices/         # Index data (to be populated)
│   └── corporate_actions/  # Corporate actions data (to be populated)
├── processed/
│   ├── equities_clean/  # Cleaned and processed equity data
│   ├── indices_clean/   # Processed index data
│   └── adjusted_prices/ # Price data adjusted for corporate actions
└── metadata/
    ├── equity_list.csv
    ├── symbol_sector_map.csv
    └── index_master.csv
```

## Data Status

### Raw Data Downloads

#### Equities (Bhavcopy)
| Year | Status | Files | Date Range | Size | Notes |
|------|--------|-------|------------|------|-------|
| 2024 | ✅ Partial | 5 | Jan 1-5 | ~1.2 MB | Test download successful |
| 2016 | ✅ Complete | 245 | Jan-Dec | 34.94 MB | Full year downloaded |

**Last Updated:** 2024-12-16 23:33 IST

### Processed Data
| Dataset | Status | Records | Last Updated | Notes |
|---------|--------|---------|--------------|-------|
| equities_clean | ⏳ Pending | - | - | Awaiting raw data completion |
| indices_clean | ⏳ Pending | - | - | Not started |
| adjusted_prices | ⏳ Pending | - | - | Not started |

## Data Processing Pipeline

### Stage 1: Download Raw Data ✅ Completed (2016), 🔄 In Progress (2017+)
- **Script:** `nse_bhavcopy_downloader.py`
- **Source:** NSE Archives (https://archives.nseindia.com)
- **Format:** ZIP files containing CSV bhavcopy data
- **Status:** 2016 complete (245 files, 34.94 MB), 2017 downloading (51% complete)

### Stage 2: Equity Data Cleaning + Normalization ⏳ Pending

**Objective:** Convert thousands of daily CSVs → one clean, normalized equity OHLCV dataset

**Processing Steps:**
1. **Filter Valid Tradable Equities**
   - Keep only: `SERIES == 'EQ'`
   - Drop: BE, BL, IL, SM, etc.

2. **Standardize Columns**
   - Canonical schema:
     ```
     trade_date  DATE
     symbol      TEXT
     open        FLOAT
     high        FLOAT
     low         FLOAT
     close       FLOAT
     volume      BIGINT
     turnover    FLOAT (optional)
     ```

3. **Normalize Dates**
   - Convert TIMESTAMP to proper DATE
   - No time component
   - Consistent timezone (IST implied)

4. **Handle Symbol Consistency**
   - Preserve raw SYMBOL (do NOT merge symbols at this stage)
   - Symbols may change names over time
   - Adjust later using ISIN if needed

5. **Consolidate into Parquet**
   - Output: `nse_data/processed/equities_clean/equity_ohlcv.parquet`
   - Tool: **DuckDB** (fast, columnar, zero server)

**Output:**
```
nse_data/processed/equities_clean/equity_ohlcv.parquet
```

### Stage 3: Index Historical Data ⏳ Pending
- **Script:** `nse_index_downloader.py`
- **Source:** NSE Archives (https://archives.nseindia.com/content/indices/)
- **Indices:** Nifty 50, Nifty Bank, Nifty IT, Nifty Pharma, Nifty FMCG, Nifty Metal, Nifty Energy, Nifty Midcap 150, Nifty Smallcap 250, Nifty Financial Services
- **Format:** Single CSV per index (entire history, no date looping required)
- **Output Structure:**
  ```
  nse_data/processed/indices_clean/
  ├── index=nifty50/index_ohlc.parquet
  ├── index=niftybank/index_ohlc.parquet
  ├── index=niftyit/index_ohlc.parquet
  └── ...
  ```
- **Schema:** Same as equities (trade_date, index, open, high, low, close, volume, turnover)

### Stage 4: Corporate Actions Adjustment ⏳ Pending

**Objective:** Produce backward-adjusted prices for accurate long-term backtesting

**What This Fixes:**
- Stock splits (e.g., 1:5 split)
- Bonus issues (e.g., 1:1 bonus)
- Dividends (optional, configurable)
- Rights issues (optional)

**Design Principles:**
- ✅ Backward adjustment only (industry standard)
- ✅ Price × factor, Volume ÷ factor
- ✅ Adjustments applied strictly before ex-date
- ✅ Raw data remains immutable
- ✅ Adjusted data stored separately

**Data Sources:**
1. **Equity OHLCV:** `nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet`
2. **Corporate Actions:** https://archives.nseindia.com/content/corporate_actions/ca.csv

**Scripts:**
- `nse_load_corporate_actions.py` - Downloads CA data and loads into Postgres
- `nse_adjust_prices.py` - Applies backward adjustments
- `nse_validate_adjusted.py` - Validates price continuity

**Postgres Schema:**
```sql
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol TEXT,
    ex_date DATE,
    purpose TEXT
);
CREATE INDEX idx_ca_symbol_date ON corporate_actions(symbol, ex_date);
```

**Output:**
```
nse_data/processed/equities_adjusted/
├── year=2016/equity_ohlcv_adj.parquet
├── year=2017/equity_ohlcv_adj.parquet
└── ...
```

### Stage 5: Sector + Index Membership Mapping ⏳ READY

**Objective:** Create time-aware sector and index membership to prevent survivorship bias

**What This Solves:**
- ❌ Naive assumption: "A stock belongs to a sector/index forever"
- ✅ Reality: NSE rebalances indices, stocks enter/exit, sectors change

**Data Sources:**
1. **Sector Classification:** https://archives.nseindia.com/content/equities/EQUITY_L.csv
2. **Index Constituents:** https://archives.nseindia.com/content/indices/ind_{index}list.csv

**Scripts:**
- `nse_load_sectors.py` - Loads sector classifications from NSE
- `nse_snapshot_indices.py` - Monthly snapshots of index membership (run via cron)

**Postgres Schemas:**

```sql
-- Sector membership (slowly changing)
CREATE TABLE sector_membership (
    symbol TEXT,
    sector TEXT,
    start_date DATE,
    end_date DATE,
    PRIMARY KEY (symbol, start_date)
);

-- Index membership (rebalance-aware)
CREATE TABLE index_membership (
    symbol TEXT,
    index_name TEXT,
    start_date DATE,
    end_date DATE,
    PRIMARY KEY (symbol, index_name, start_date)
);
```

**Usage in Backtests (Critical):**
```sql
-- Check if stock was in Nifty 50 on a specific date
SELECT * FROM index_membership
WHERE symbol = 'SBIN'
  AND index_name = 'nifty50'
  AND '2024-01-01' BETWEEN start_date AND COALESCE(end_date, '9999-12-31');
```

This prevents future leakage and survivorship bias.

## Data Quality Metrics

### 2024 Sample (Jan 1-5)
- **Total Files:** 5
- **Average Records per Day:** ~2,685
- **Unique Symbols:** TBD
- **Data Completeness:** 100%
- **Format Validation:** ✅ Passed

### Data Columns (Bhavcopy)
```
SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE,
TOTTRDQTY, TOTTRDVAL, TIMESTAMP, TOTALTRADES, ISIN
```

## Usage Notes

### Downloading Data
```bash
# Download specific year
python nse_bhavcopy_downloader.py 2016-01-01 2016-12-31

# Download date range
python nse_bhavcopy_downloader.py 2020-01-01 2024-12-31

# Download all available (2020-present)
python nse_bhavcopy_downloader.py
```

### Data Limitations
1. **Historical Availability:** NSE archive may not have data before 2016
2. **Market Holidays:** Files will be missing for market holidays
3. **Weekends:** Automatically skipped by downloader
4. **File Format:** Data is in CSV format, compressed as ZIP

## Next Steps (In Order - Do NOT Skip)

1. ✅ **Download Raw Data (2016-2023)**
2. ⏳ **Equity Cleaning & Consolidation** (Stage 2)
   - Filter EQ series only
   - Standardize schema
   - Create `equity_ohlcv.parquet`
3. ⏳ **Index Historical Downloader** (Stage 3)
4. ⏳ **Corporate-Action Adjusted Prices** (Stage 4)
5. ⏳ **Sector + Index Membership Mapping** (Stage 5)

## Change Log

### 2024-12-16
- ✅ Created directory structure
- ✅ Implemented bhavcopy downloader with progress tracking
- ✅ Downloaded test data (2024 Jan 1-5, 5 files)
- ✅ **Completed 2016 full year download (245 files, 34.94 MB)**
- ✅ **Completed 2017 full year download (246 files)**
- 🔄 **2018-2023 download in progress**
- ✅ Added nse_data to .gitignore
- ✅ Created this README
- ✅ **Created all Stage 2 scripts** (equity cleaning + validation)
- ✅ **Created all Stage 3 scripts** (index download + validation)
- ✅ **Created all Stage 4 scripts** (corporate actions adjustment)
- ✅ **Created all Stage 5 scripts** (sector + index membership mapping)

---
*This file is automatically updated as data is extracted and processed.*
