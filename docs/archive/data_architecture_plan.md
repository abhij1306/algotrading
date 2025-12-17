# NSE Data Architecture - Implementation Plan

## Executive Summary

**3-Tier Architecture** (Cold → Warm → Hot)

| Layer | Purpose | Technology | Data Retention |
|-------|---------|------------|----------------|
| **Cold** | Immutable historical truth | NSE Parquet + DuckDB | 2016-2023+ (all time) |
| **Warm** | Operational queries | Postgres | Last 90-180 days |
| **Hot** | Real-time data | Fyers API + Cache | Current session |

**Key Principle**: Never duplicate Cold data in Warm layer. Use on-demand merging via DuckDB.

---

## Critical Design Decisions

### ✅ Locked In
1. **3-Tier Separation**: Cold/Warm/Hot layers remain distinct
2. **UnifiedDataService**: Single entry point for all queries
3. **NSE as Historical Truth**: Official source for backtests
4. **DuckDB for Parquet**: Fast, zero-infra reads
5. **No Full Backfill**: Postgres stays lean (90-180 days only)

### ❌ Removed
1. ~~Full NSE import to Postgres~~ → Use DuckDB on-demand
2. ~~Overloaded schema~~ → Keep simple
3. ~~UnifiedDataService writes~~ → Read-only service

---

## Data Source Priority

| Date Range | Source of Truth | Fallback |
|------------|----------------|----------|
| Today (market hours) | Fyers API | Postgres latest |
| Today (after hours) | Postgres | NSE Parquet |
| Last 30-60 days | Fyers → Postgres | NSE Parquet |
| Older than 60 days | **NSE Parquet** | Postgres |
| **Backtests** | **NSE Adjusted Only** | - |

**Rule**: Never overwrite Fyers data with NSE for recent dates.

---

## Implementation Components

### 1. NSE Data Reader (DuckDB)
- **File**: `backend/app/nse_data_reader.py`
- **Purpose**: Read NSE Parquet on-demand
- **No caching**: DuckDB is fast enough
- **No writes**: Pure read layer

### 2. Data Cache (Intent-Based)
- **File**: `backend/app/data_cache.py`
- **Keys**: `{intent}:{symbol}:{params}`
- **TTLs**: live=30s, screener=5m, backtest=1h

### 3. Unified Data Service
- **File**: `backend/app/unified_data_service.py`
- **READ-ONLY**: No side effects
- **Smart routing**: Merges Warm + Cold layers
- **Fallback chain**: Fyers → Postgres → NSE

### 4. Data Retention Policy
- **File**: `backend/cleanup_old_data.py`
- **historical_prices**: Keep 180 days
- **intraday_candles**: Keep 30 days
- **Run**: Weekly cron job

---

## Execution Plan

### Week 1: Foundation
- [x] NSE data pipeline (COMPLETE)
- [ ] NSE Data Reader (DuckDB)
- [ ] Data Cache (intent-based)
- [ ] Cleanup script

### Week 2: Integration
- [ ] Unified Data Service
- [ ] Data Repository fallback method
- [ ] Testing & validation

### Week 3: Application Updates
- [ ] Update screener
- [ ] Update backtests (adjusted prices)
- [ ] Update Smart Trader
- [ ] Update Portfolio Risk

### Week 4: Validation
- [ ] Performance testing
- [ ] Cache monitoring
- [ ] Documentation

---

## Benefits

1. **Clean Separation**: No layer collapse, no duplication
2. **Performance**: DuckDB fast, Postgres lean, intent-based cache
3. **Maintainability**: Single entry point, additive approach
4. **Correctness**: NSE adjusted for backtests, Fyers for live

---

## Rollback Plan

1. Disable NSE: `ENABLE_NSE = False` in unified_data_service.py
2. No database changes needed (schema unchanged)
3. Existing code still works (additive approach)
