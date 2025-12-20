# NSE Data Directory - Unified Data Structure

## Overview
This directory contains all historical market data for NSE (National Stock Exchange) equities and indices. Data is sourced from **NSE Archives** and **Fyers API** only.

## Directory Structure
```
nse_data/
├── raw/
│   ├── equities/              # Daily OHLCV data for all NSE stocks
│   ├── indices/               # Daily OHLCV data for indices (Nifty, Bank Nifty, etc.)
│   ├── intraday/              # 5-minute intraday data (Nifty, Bank Nifty)
│   └── corporate_actions/     # Corporate actions (splits, bonuses, dividends)
├── processed/
│   ├── equities_clean/        # Cleaned and consolidated equity data (Parquet)
│   ├── indices_clean/         # Cleaned and consolidated index data (Parquet)
│   └── adjusted_prices/       # Corporate action adjusted prices (Parquet)
└── metadata/
    ├── equity_list.csv
    ├── symbol_sector_map.csv
    └── index_master.csv
```

## Data Sources

### Primary Sources (Active)
1. **NSE Archives** - Historical bhavcopy data (2016-2023)
   - Source: https://archives.nseindia.com
   - Format: Daily ZIP files containing CSV data
   - Coverage: All NSE equities and indices

2. **Fyers API v3** - Recent and real-time data (2024-present)
   - Source: Fyers trading platform API
   - Format: REST API with JSON responses
   - Coverage: Equities, indices, intraday (5-minute candles)
   - Documentation: `docs/FYERS_API_REFERENCE.md`

### Data Coverage

| Data Type | Source | Period | Resolution | Location |
|-----------|--------|--------|------------|----------|
| Equities Daily | NSE Archives | 2016-2023 | Daily | `raw/equities/` |
| Equities Daily | Fyers API | 2024-present | Daily | `raw/equities/` |
| Indices Daily | NSE Archives | 2016-2023 | Daily | `raw/indices/` |
| Indices Daily | Fyers API | 2024-present | Daily | `raw/indices/` |
| Intraday (Nifty/BankNifty) | Fyers API | 2016-present | 5-minute | `raw/intraday/` |
| Corporate Actions | NSE Archives | All periods | Event-based | `raw/corporate_actions/` |

## Data Status

### Raw Data

#### Equities (Daily)
- **Total Symbols**: 2,426 active companies
- **Date Range**: 2016-01-01 to 2025-12-20
- **Format**: CSV files (one per symbol)
- **Status**: ✅ Complete
- **Location**: `raw/equities/`

#### Indices (Daily)
- **Indices**: Nifty 50, Bank Nifty, Nifty 100, 200, 500, IT, Midcap, Smallcap
- **Date Range**: 2016-01-01 to 2025-12-20
- **Format**: CSV files (one per index)
- **Status**: ✅ Complete
- **Location**: `raw/indices/`

#### Intraday (5-Minute Candles)
- **Indices**: Nifty 50, Bank Nifty
- **Date Range**: 2016-01-01 to 2025-12-20 (9 years)
- **Total Candles**: ~165,000 per index
- **Format**: CSV files
- **Status**: ✅ Complete
- **Location**: `raw/intraday/`
- **Files**:
  - `NIFTY50_5min_complete.csv` (11.28 MB)
  - `BANKNIFTY_5min_complete.csv` (11.29 MB)

### Processed Data

| Dataset | Status | Records | Format | Last Updated |
|---------|--------|---------|--------|--------------|
| equities_clean | ✅ Complete | 2,835 symbols | Parquet | 2025-12-17 |
| indices_clean | ✅ Complete | 10+ indices | Parquet | 2025-12-20 |
| adjusted_prices | ✅ Complete | 307 symbols | Parquet | 2025-12-17 |

## Data Processing Pipeline

### Stage 1: Download Raw Data ✅ Complete

**NSE Archives (2016-2023)**
- Script: `nse_bhavcopy_downloader.py`
- Status: ✅ Complete (2016-2023 downloaded)

**Fyers API (2024-present)**
- Scripts:
  - `download_nifty_banknifty.py` - Daily index data
  - `download_fyers_equities.py` - Daily equity data
  - `download_intraday_5min.py` - 5-minute intraday data
- Status: ✅ Complete

### Stage 2: Data Consolidation ✅ Complete

**Script**: `consolidate_all_data.py`

**Actions**:
1. Merged Fyers data with NSE data
2. Consolidated duplicate folders
3. Removed Yahoo data (deprecated)
4. Created unified structure

**Output**:
- All equity CSVs in `raw/equities/`
- All index CSVs in `raw/indices/`
- Intraday data in `raw/intraday/`

### Stage 3: Data Cleaning & Normalization ⏳ Ready

**Objective**: Convert raw CSVs → clean Parquet files

**Processing Steps**:
1. **Filter Valid Equities**
   - Keep only: `SERIES == 'EQ'`
   - Drop: BE, BL, IL, SM, etc.

2. **Standardize Schema**
   ```
   date        DATE
   symbol      TEXT
   open        FLOAT
   high        FLOAT
   low         FLOAT
   close       FLOAT
   volume      BIGINT
   ```

3. **Consolidate into Parquet**
   - Tool: DuckDB (fast, columnar)
   - Output: `processed/equities_clean/equity_ohlcv.parquet`

### Stage 4: Corporate Actions Adjustment ⏳ Ready

**Objective**: Produce backward-adjusted prices for accurate backtesting

**What This Fixes**:
- Stock splits (e.g., 1:5 split)
- Bonus issues (e.g., 1:1 bonus)
- Dividends (optional)
- Rights issues (optional)

**Design Principles**:
- ✅ Backward adjustment only (industry standard)
- ✅ Price × factor, Volume ÷ factor
- ✅ Adjustments applied before ex-date
- ✅ Raw data remains immutable

**Scripts**:
- `nse_load_corporate_actions.py` - Downloads CA data
- `nse_adjust_prices.py` - Applies adjustments
- `nse_validate_adjusted.py` - Validates continuity

**Output**: `processed/adjusted_prices/equity_ohlcv_adj.parquet`

## Usage

### Downloading Data

**Daily Updates (Automated)**:
```bash
# Run daily at 4:00 PM (after market close)
scripts/setup/run_fyers_daily_update.bat
```

**Manual Downloads**:
```bash
# Download Nifty & Bank Nifty daily data
python scripts/data_pipeline/download_nifty_banknifty.py

# Download all equities
python scripts/data_pipeline/download_fyers_equities.py

# Download 5-minute intraday data
python scripts/data_pipeline/download_intraday_5min.py
```

### Processing Data

```bash
# Consolidate all data sources
python scripts/data_pipeline/consolidate_all_data.py

# Clean equity data
python scripts/data_pipeline/clean_equity_data.py

# Apply corporate actions
python scripts/data_pipeline/apply_corporate_actions.py
```

### Accessing Data

**From Python**:
```python
import pandas as pd

# Read equity data
df = pd.read_parquet('nse_data/processed/equities_clean/equity_ohlcv.parquet')

# Read index data
df_idx = pd.read_parquet('nse_data/processed/indices_clean/index_ohlcv.parquet')

# Read intraday data
df_intraday = pd.read_csv('nse_data/raw/intraday/NIFTY50_5min_complete.csv')
```

**From Database** (PostgreSQL):
```sql
-- Query historical prices
SELECT * FROM historical_prices 
WHERE symbol = 'SBIN' 
AND date >= '2024-01-01';

-- Query with technical indicators
SELECT date, symbol, close, ema_20, rsi_14 
FROM historical_prices 
WHERE symbol = 'RELIANCE';
```

## Data Quality

### Validation Checks
- ✅ No duplicate dates per symbol
- ✅ OHLC relationships (High ≥ Low, etc.)
- ✅ Volume > 0 for traded days
- ✅ Price continuity (no gaps > 50%)
- ✅ Corporate action adjustments validated

### Known Limitations
1. **Historical Availability**: NSE archive data starts from 2016
2. **Market Holidays**: No data for market holidays
3. **Intraday Coverage**: 5-minute data only for Nifty 50 and Bank Nifty
4. **Corporate Actions**: Manual validation recommended for critical trades

## File Naming Conventions

**Daily Data**:
- Equities: `{SYMBOL}.csv` (e.g., `SBIN.csv`, `RELIANCE.csv`)
- Indices: `{INDEX}.csv` (e.g., `NIFTY50.csv`, `BANKNIFTY.csv`)

**Intraday Data**:
- Format: `{INDEX}_5min_complete.csv`
- Example: `NIFTY50_5min_complete.csv`

**Processed Data**:
- Equities: `equity_ohlcv.parquet`
- Indices: `index_ohlcv.parquet`
- Adjusted: `equity_ohlcv_adj.parquet`

## Maintenance

### Daily Updates
- Automated via Windows Task Scheduler
- Runs at 4:00 PM (after market close at 3:30 PM)
- Updates equities, indices, and intraday data
- Logs to `logs/fyers_daily_update.log`

### Weekly Tasks
- Validate data quality
- Check for missing dates
- Update corporate actions
- Backup processed Parquet files

### Monthly Tasks
- Download updated symbol master from NSE
- Update sector classifications
- Reprocess adjusted prices if needed
- Archive old raw CSV files

## Troubleshooting

### Missing Data
```bash
# Check for gaps in data
python scripts/validation/check_data_gaps.py

# Re-download specific date range
python scripts/data_pipeline/download_fyers_equities.py --start 2024-01-01 --end 2024-12-31
```

### Fyers API Issues
```bash
# Re-login to Fyers
cd fyers
python fyers_login.py

# Validate token
python fyers_client.py --validate
```

### Data Corruption
```bash
# Restore from backup
python scripts/maintenance/restore_backup.py --date 2024-12-19

# Reprocess from raw
python scripts/data_pipeline/consolidate_all_data.py
```

## Documentation

- **Fyers API Reference**: `docs/FYERS_API_REFERENCE.md`
- **Data Setup Guide**: `docs/FYERS_DATA_SETUP.md`
- **Download Status**: `docs/FYERS_DOWNLOAD_STATUS.md`
- **Scripts Documentation**: `scripts/README.md`

## Change Log

### 2025-12-20
- ✅ **Consolidated all data sources** (Fyers + NSE)
- ✅ **Removed Yahoo Finance** (deprecated)
- ✅ **Downloaded 9 years of intraday data** (2016-2025)
- ✅ **Unified directory structure**
- ✅ **Updated all documentation**

### 2025-12-17
- ✅ Created all Stage 2-5 processing scripts
- ✅ Completed equity and index data cleaning
- ✅ Applied corporate actions adjustments

### 2024-12-16
- ✅ Created directory structure
- ✅ Implemented NSE bhavcopy downloader
- ✅ Downloaded 2016-2023 historical data

---

**Data Sources**: NSE Archives + Fyers API v3  
**Last Updated**: December 20, 2025  
**Status**: ✅ Production Ready
