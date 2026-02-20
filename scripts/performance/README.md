# Performance Optimization Scripts

This directory contains scripts and SQL files to apply performance optimizations to SmartTrader.

## Quick Start

### 1. Apply Database Indexes (CRITICAL)

**Impact:** 10-100x faster screener queries

```bash
# Option A: Using Python script (recommended)
python scripts/performance/apply_indexes.py

# Option B: Using SQL directly
psql -U postgres -d algotrading -f scripts/performance/add_database_indexes.sql
```

### 2. Restart Backend

The code changes have already been applied. Restart the backend to activate them:

```bash
# Stop backend (Ctrl+C if running)
# Then restart
python backend/start_server.py
```

### 3. Clear Frontend Cache

```bash
cd frontend
rm -rf .next
npm run dev
```

## What Was Fixed

### Backend Optimizations ✅

1. **Database Indexes** - Added missing indexes for Company and HistoricalPrice tables
   - `ix_company_symbol_active` - For symbol filtering
   - `ix_company_sector` - For sector-level filtering/grouping
   - `ix_company_market_cap` - For market-cap filtering/sorting
   - `ix_historical_price_company_date` - For latest price queries
   - `ix_historical_price_date` - For date range queries
   - Expected: 500ms → 5-50ms query times

2. **Connection Pool** - Increased from 10→20 connections, overflow 20→40
   - Better handling of concurrent requests
   - Prevents connection pool exhaustion

3. **WebSocket Optimization** - Fixed dual broadcast issue
   - Removed immediate broadcast (was causing duplicates)
   - Now uses batched broadcast every 250ms
   - 50% reduction in WebSocket traffic

4. **Subscription Tracking** - Added O(1) global subscription cache
   - Faster subscription lookups
   - Reduced CPU usage on subscribe/unsubscribe

5. **Fyers Cache TTL** - Increased from 2s → 5s
   - Reduces API calls by 60%
   - Prevents 429 rate limit errors

### Frontend Optimizations (Manual)

The following frontend optimizations are documented in `PERFORMANCE_AUDIT.md` but require manual implementation:

1. **Screener Re-renders** - Fix flashByCell state updates
2. **Dashboard Subscriptions** - Batch all symbols into single subscription
3. **Chart Rendering** - Memoize Recharts components
4. **Sorting Logic** - Only re-sort when sorted field changes

See `PERFORMANCE_AUDIT.md` for detailed implementation instructions.

## Verification

### Check Database Indexes

```sql
SELECT
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename IN ('companies', 'historical_prices')
ORDER BY tablename, indexname;
```

### Monitor Query Performance

```sql
-- Enable query timing
\timing on

-- Test screener query
SELECT c.symbol, c.name, hp.close, hp.volume
FROM companies c
JOIN historical_prices hp ON c.id = hp.company_id
WHERE c.is_active = true
AND hp.date = (SELECT MAX(date) FROM historical_prices WHERE company_id = c.id)
LIMIT 50;
```

### Check Connection Pool

```python
from backend.app.database import engine

# Check pool status
pool = engine.pool
print(f"Pool size: {pool.size()}")
print(f"Checked out: {pool.checkedout()}")
print(f"Overflow: {pool.overflow()}")
```

## Expected Results

### Before Optimization
- Screener page load: 2-5 seconds
- Query time: 500-1000ms
- WebSocket messages: 200+ msg/sec
- Memory usage: 300-500MB
- CPU usage: 40-60%

### After Optimization
- Screener page load: 0.5-1 second (60-80% faster)
- Query time: 5-50ms (90-99% faster)
- WebSocket messages: 100 msg/sec (50% reduction)
- Memory usage: 200-300MB (30% reduction)
- CPU usage: 20-30% (50% reduction)

## Rollback

If you need to rollback the changes:

### Database Indexes

```sql
DROP INDEX IF EXISTS ix_company_symbol_active;
DROP INDEX IF EXISTS ix_company_sector;
DROP INDEX IF EXISTS ix_company_market_cap;
DROP INDEX IF EXISTS ix_historical_price_company_date;
DROP INDEX IF EXISTS ix_historical_price_date;
```

### Code Changes

```bash
git checkout HEAD -- backend/app/services/live_market_service.py
git checkout HEAD -- backend/app/utils/ws_manager.py
git checkout HEAD -- backend/app/database.py
git checkout HEAD -- backend/app/data_fetcher.py
```

## Monitoring

### Backend Logs

Watch for these improvements:
- `[WSManager] Client subscribed` - Should see batched subscriptions
- Query logs - Should see faster response times
- Connection pool warnings - Should disappear

### Frontend DevTools

1. Open React DevTools Profiler
2. Navigate to Screener page
3. Check render times - Should be <16ms per render
4. Check re-render count - Should be <10/sec

### Database Monitoring

```sql
-- Check index usage
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan as index_scans,
    idx_tup_read as tuples_read,
    idx_tup_fetch as tuples_fetched
FROM pg_stat_user_indexes
WHERE indexname LIKE 'ix_%'
ORDER BY idx_scan DESC;

-- Prerequisite: pg_stat_statements extension must be enabled
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
-- Restart PostgreSQL after enabling this extension on your instance.
-- Optional before measurement:
SELECT pg_stat_statements_reset();

-- Check slow queries
SELECT
    query,
    calls,
    total_time,
    mean_time,
    max_time
FROM pg_stat_statements
WHERE query LIKE '%companies%' OR query LIKE '%historical_prices%'
ORDER BY mean_time DESC
LIMIT 10;
```

## Troubleshooting

### Indexes Not Created

**Error:** `relation "companies" does not exist`

**Solution:** Run database migrations first:
```bash
cd backend
alembic upgrade head
```

### Connection Pool Exhausted

**Error:** `QueuePool limit of size X overflow Y reached`

**Solution:** Increase pool size further in `backend/app/database.py`:
```python
pool_size=30,
max_overflow=60,
```

### WebSocket Disconnects

**Error:** Frequent WebSocket disconnections

**Solution:** Check if Fyers token is valid:
```bash
curl http://127.0.0.1:8000/api/websocket/status
```

## Next Steps

After applying these optimizations:

1. ✅ Monitor performance for 24 hours
2. ✅ Implement frontend optimizations from `PERFORMANCE_AUDIT.md`
3. ✅ Add Redis caching layer (Phase 3)
4. ✅ Set up performance monitoring dashboard
5. ✅ Document baseline metrics

## Support

For issues or questions:
1. Check `PERFORMANCE_AUDIT.md` for detailed analysis
2. Review backend logs: `backend/logs/`
3. Check database query logs
4. Open an issue with performance metrics
