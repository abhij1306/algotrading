# AlgoTrading Scripts

Operational scripts for data pipeline, system maintenance, and initial setup.

---

## 📁 Directory Structure

```
scripts/
├── data_pipeline/      # Data fetching, cleaning, and processing
├── maintenance/        # System health and diagnostics
└── setup/             # Initial setup and configuration
```

---

## 🔄 Data Pipeline Scripts

### Yahoo Finance Integration

**`yahoo_downloader.py`**
- Downloads OHLCV data from Yahoo Finance for NSE equities and indices
- Coverage: 2024-present
- Symbols: 2,426 equities + 10 indices
- Rate limiting: 1.5s per symbol

```bash
# Download all data
python data_pipeline/yahoo_downloader.py

# Download only indices
python data_pipeline/yahoo_downloader.py --indices-only

# Download only equities (with limit for testing)
python data_pipeline/yahoo_downloader.py --equities-only --limit 10
```

**`yahoo_data_cleaner.py`**
- Converts Yahoo CSVs to NSE schema
- Merges with existing Parquet files
- Creates automatic backups
- Validates OHLCV constraints

```bash
# Clean and merge all data
python data_pipeline/yahoo_data_cleaner.py

# Clean only equities
python data_pipeline/yahoo_data_cleaner.py --equities-only

# Clean only indices
python data_pipeline/yahoo_data_cleaner.py --indices-only
```

**`yahoo_financials_fetcher.py`**
- Fetches quarterly/annual financial statements from Yahoo Finance
- Extracts: Income statement, balance sheet, cash flow
- Stores in PostgreSQL `financial_statements` table
- Coverage: 2,180 companies missing financial data

```bash
# Fetch all missing financials
python data_pipeline/yahoo_financials_fetcher.py

# Test with limited companies
python data_pipeline/yahoo_financials_fetcher.py --limit 10

# Fetch from specific year
python data_pipeline/yahoo_financials_fetcher.py --year 2023
```

### Daily Updates

**`update_daily_data.py`**
- Automated daily data update script
- Downloads latest NSE bhavcopy
- Updates PostgreSQL with Fyers data
- Calculates technical indicators

```bash
# Run manual update
python data_pipeline/update_daily_data.py
```

**`apply_corporate_actions.py`**
- Applies backward price adjustments for splits/bonuses
- Updates `equity_ohlcv_adj.parquet`
- Reads corporate actions from PostgreSQL

```bash
# Apply corporate actions
python data_pipeline/apply_corporate_actions.py
```

---

## 🔧 Maintenance Scripts

**`health_check.py`**
- System health monitoring
- Checks database connectivity
- Verifies Parquet files
- Tests Fyers API access
- Generates health report JSON

```bash
# Run health check
python maintenance/health_check.py
```

**`audit_data_management.py`**
- Audits data coverage and quality
- Identifies missing data
- Generates audit report
- Checks data continuity

```bash
# Run data audit
python maintenance/audit_data_management.py
```

**`verify_symbols.py`**
- Verifies symbol consistency across data sources
- Checks for missing symbols
- Validates symbol mappings

```bash
# Verify symbols
python maintenance/verify_symbols.py
```

---

## ⚙️ Setup Scripts

**`init_database.py`**
- Initializes PostgreSQL database
- Creates all tables
- Sets up indexes
- Loads initial data

```bash
# Initialize database
python setup/init_database.py
```

**`run_daily_update.bat`**
- Windows batch file for Task Scheduler
- Wrapper for `update_daily_data.py`
- Logs output to file
- Scheduled for 3:45 PM weekdays

```bash
# Run manually
setup/run_daily_update.bat
```

---

## 📋 Execution Order

### Initial Setup
```bash
1. python setup/init_database.py
2. python data_pipeline/yahoo_downloader.py
3. python data_pipeline/yahoo_data_cleaner.py
4. python data_pipeline/apply_corporate_actions.py
5. python data_pipeline/yahoo_financials_fetcher.py
```

### Daily Maintenance
```bash
# Automated via Task Scheduler
setup/run_daily_update.bat

# Manual health check (weekly)
python maintenance/health_check.py
```

---

## 🚨 Troubleshooting

### Script Fails to Import Modules
```bash
# Ensure you're in the AlgoTrading root directory
cd c:\AlgoTrading

# Run with python -m if needed
python -m scripts.data_pipeline.yahoo_downloader
```

### Database Connection Errors
```bash
# Check .env file exists
# Verify PostgreSQL is running
# Test connection
psql -U postgres -d algotrading
```

### Parquet File Errors
```bash
# Install required packages
pip install pyarrow duckdb

# Check file permissions
# Verify disk space
```

---

## 📊 Expected Runtimes

| Script | Duration | Notes |
|--------|----------|-------|
| `yahoo_downloader.py` (full) | ~60 min | 2,426 symbols @ 1.5s each |
| `yahoo_downloader.py` (indices) | ~30 sec | 10 indices |
| `yahoo_data_cleaner.py` | ~10 min | Processing all CSVs |
| `yahoo_financials_fetcher.py` | ~2 hours | 2,180 companies @ 2s each |
| `update_daily_data.py` | ~5 min | Daily incremental |
| `apply_corporate_actions.py` | ~5 min | Full dataset |
| `health_check.py` | ~30 sec | All checks |

---

## 📝 Notes

- All scripts use absolute paths (safe to run from any directory)
- Rate limiting prevents API blocking
- Automatic backups created before modifications
- Progress tracking for long-running operations
- Error handling with detailed logging

---

**Last Updated**: December 17, 2025  
**Total Scripts**: 9  
**Categories**: Data Pipeline (5), Maintenance (3), Setup (2)
