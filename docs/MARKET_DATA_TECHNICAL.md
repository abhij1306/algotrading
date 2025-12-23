# MARKET DATA MODULE - Technical Documentation

**Version:** 2.0.0  
**Last Updated:** 2025-12-22  
**Module:** Data Ingestion, Storage & Distribution

---

## Overview

The Market Data module is the **backbone** of SmartTrader 3.0, providing clean, adjusted,and reliable OHLCV data to all other modules. It implements the "Database as Source of Truth" principle.

---

## Data Sources

### 1. NSE Bhavcopy (Primary - EOD)
- **URL:** `https://www.nseindia.com/api/reports`
- **Frequency:** Daily, after market close (3:30 PM IST)
- **Format:** CSV
- **Content:** OHLCV, delivery data, corporate actions

### 2. Fyers API v3 (Secondary - Intraday)
- **Purpose:** Live quotes during market hours
- **Rate Limit:** 10 req/sec, 100 symbols/req
- **Data:** LTP, volume, OHLC, bid/ask

### 3. Manual Uploads
- Corporate actions (splits, bonuses)
- Index rebalancing
- F&O stock list updates

---

## Data Pipeline

```
External Sources → Ingestion → Validation → Storage → Distribution
        ↓              ↓            ↓            ↓            ↓
   NSE/Fyers    Download CSV   Check data   PostgreSQL   DataProvider
                Parse rows    quality      Store OHLCV   API layer
                Transform     Handle gaps   Indicators    Modules
```

### Daily Update Workflow

**File:** `backend/scripts/update_bhavcopy.py`

```
3:45 PM IST Daily:
1. Download NSE bhavcopy
2. Parse CSV → DataFrame
3. Validate data (no nulls, price > 0)
4. Upsert into historical_prices
5. Calculate indicators
6. Log completion
```

---

## Database Schema

### companies (Master)
- symbol, name, sector, industry
- market_cap, is_fno, is_active

### historical_prices (Time Series)
- OHLCV + 15 technical indicators
- Partitioned by year for performance

### data_update_logs (Audit)
- Tracks every data update
- Records source, timestamp, status

---

## Data Quality Assurance

### Validation Rules
1. **Price Checks:**
   - high >= low
   - open, close within [low, high]
   - All prices > 0

2. **Volume Checks:**
   - volume >= 0
   - Delivery <= volume

3. **Date Checks:**
   - No future dates
   - No duplicate (symbol, date)

### Error Handling
```python
if invalid_data:
    log_error()
    skip_row()  # Don't store bad data
    alert_admin()
```

---

## Performance Optimizations

1. **Partitioning:** historical_prices by year
2. **Indexes:** (company_id, date), (date), (symbol)
3. **Materialized Views:** latest_prices for fast queries
4. **Caching:** Redis for frequently accessed data

---

## Module Distribution

All modules access data via **DataProvider** API:
- `get_ohlcv(symbols, dates)` - Historical data
- `get_latest(symbols)` - Live quotes
- `get_indicators(symbols, indicators, dates)` - Technical data

**Never** direct database access from modules!

---

## Maintenance

- **Daily:** Bhavcopy import (automated)
- **Weekly:** Data quality validation script
- **Monthly:** Cleanup old data (>2 years)
- **Quarterly:** Index rebalancing

**Critical:** Database is source of truth. Never modify historical_prices manually!
