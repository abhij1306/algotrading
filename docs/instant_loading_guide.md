# Screener Instant Loading - Implementation Guide

Created: December 14, 2024

---

## 🎯 Current State
- **Page Size:** 50 companies (reduced from 100)
- **Load Time:** ~150-250ms
- **Goal:** Make it feel/be instant

---

## 🚀 Level 1: Perceived Instant (Quick Wins)

### 1. Skeleton Loading Screen ⭐ RECOMMENDED FIRST
**Impact:** Makes page feel 10x faster  
**Time:** 30 minutes  
**Difficulty:** Easy

**What it does:**
- Shows table structure immediately (<50ms)
- Animated placeholder rows while data loads
- User sees layout instantly, data fills in

**Implementation:**
```tsx
// In page.tsx, show skeleton when loading=true
{loading ? (
  <SkeletonTable rows={50} />
) : (
  <ActualTable data={stocks} />
)}
```

**Files to modify:**
- `frontend/app/page.tsx` - Add skeleton component
- `frontend/components/SkeletonTable.tsx` - Create new component

---

### 2. localStorage Cache
**Impact:** 0ms load for repeat visits  
**Time:** 45 minutes  
**Difficulty:** Easy

**What it does:**
- Saves last fetched data in browser
- Shows cached data instantly on page load
- Refreshes in background

**Implementation:**
```tsx
// Save to cache after fetch
localStorage.setItem('screenerData', JSON.stringify(data))

// Load from cache on mount
const cached = localStorage.getItem('screenerData')
if (cached) {
  setStocks(JSON.parse(cached))
  // Then fetch fresh data in background
}
```

**Considerations:**
- Cache expiry: 5-10 minutes
- Clear on sector/symbol filter change
- Show "Last updated: X mins ago" indicator

---

## ⚡ Level 2: Optimize Backend (Medium Effort)

### 3. Database Indexes
**Impact:** 90% faster queries (500ms → 50ms)  
**Time:** 1 hour  
**Difficulty:** Medium

**What it does:**
- Adds indexes to frequently queried columns
- PostgreSQL can find rows 10-100x faster

**Implementation:**
```sql
-- Add these indexes to PostgreSQL
CREATE INDEX idx_company_symbol ON companies(symbol);
CREATE INDEX idx_company_sector ON companies(sector);
CREATE INDEX idx_company_market_cap ON companies(market_cap);
CREATE INDEX idx_candle_symbol_timestamp ON candles(company_id, timestamp);
CREATE INDEX idx_financials_company ON financial_statements(company_id);
```

**Files to modify:**
- New migration file in `backend/` or run directly in pgAdmin

**Verification:**
```sql
EXPLAIN ANALYZE SELECT * FROM companies WHERE symbol = 'RELIANCE';
-- Should show "Index Scan" instead of "Seq Scan"
```

---

### 4. Redis Cache Layer
**Impact:** 95% faster (500ms → 1-5ms from memory cache)  
**Time:** 2 hours  
**Difficulty:** Medium-Hard

**What it does:**
- Caches screener results in Redis (in-memory database)
- Serves from RAM instead of PostgreSQL
- Auto-expires after 5-10 minutes

**Requirements:**
```bash
pip install redis
# Install Redis server (Windows: https://github.com/microsoftarchive/redis/releases)
```

**Implementation:**
```python
# backend/app/main.py
import redis
r = redis.Redis(host='localhost', port=6379, decode_responses=True)

@app.get("/api/screener")
async def get_screener(page: int = 1, limit: int = 50):
    cache_key = f"screener:{page}:{limit}"
    
    # Check cache first
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # Fetch from DB if not cached
    data = fetch_from_database()
    
    # Cache for 5 minutes
    r.setex(cache_key, 300, json.dumps(data))
    
    return data
```

**Pros:**
- Extremely fast for all users
- Reduces database load

**Cons:**
- Adds infrastructure dependency
- Data slightly stale (5-10 min old)

---

### 5. Parallel Queries
**Impact:** 50% faster tab switching  
**Time:** 1 hour  
**Difficulty:** Medium

**What it does:**
- Fetches technicals + financials at same time
- Both tabs ready instantly

**Implementation:**
```tsx
// frontend/app/page.tsx
useEffect(() => {
  // Fetch both at once
  Promise.all([
    fetch('/api/screener'),
    fetch('/api/screener/financials')
  ]).then(([tech, fin]) => {
    setStocks(await tech.json())
    setFinancials(await fin.json())
  })
}, [])
```

**Benefit:** Tab switching becomes instant (0ms)

---

## 🔥 Level 3: Advanced (Complex)

### 6. Virtual Scrolling
**Impact:** Handle 10,000+ stocks with no lag  
**Time:** 3-4 hours  
**Difficulty:** Hard

**What it does:**
- Only renders ~20 visible rows in viewport
- Recycles DOM elements as you scroll
- Smooth with massive datasets

**Library:** `react-virtual` or `react-window`

**Implementation:**
```tsx
import { useVirtual } from 'react-virtual'

// Only renders visible rows
const rowVirtualizer = useVirtual({
  size: stocks.length,
  parentRef: tableRef,
  estimateSize: React.useCallback(() => 50, [])
})
```

**Pros:**
- Handles any dataset size
- Silky smooth scrolling

**Cons:**
- Complex implementation
- Harder to maintain

---

### 7. Server-Sent Events (SSE)
**Impact:** Progressive loading - see first results in 50ms  
**Time:** 4 hours  
**Difficulty:** Hard

**What it does:**
- Streams data as backend computes it
- Show first 10 stocks immediately
- More arrive as they're ready

**Implementation:**
```python
# backend/app/main.py
from fastapi.responses import StreamingResponse

@app.get("/api/screener/stream")
async def stream_screener():
    async def generator():
        for batch in fetch_in_batches(batch_size=10):
            yield json.dumps(batch) + "\n"
    
    return StreamingResponse(generator(), media_type="text/event-stream")
```

---

## 📊 Comparison Matrix

| Strategy | Impact | Time | Difficulty | Recommended |
|----------|--------|------|------------|-------------|
| Skeleton Loading | ⭐⭐⭐⭐⭐ | 30min | Easy | ✅ YES |
| localStorage Cache | ⭐⭐⭐⭐ | 45min | Easy | ✅ YES |
| DB Indexes | ⭐⭐⭐⭐⭐ | 1hr | Medium | ✅ YES |
| Redis Cache | ⭐⭐⭐⭐⭐ | 2hr | Medium | ⚠️ Maybe |
| Parallel Queries | ⭐⭐⭐ | 1hr | Medium | ⚠️ Maybe |
| Virtual Scrolling | ⭐⭐⭐ | 3-4hr | Hard | ❌ Later |
| SSE Streaming | ⭐⭐ | 4hr | Hard | ❌ Later |

---

## 🎯 Recommended Implementation Order

### Phase 1: Quick Wins (2 hours total)
1. **Skeleton Loading** (30 min)
2. **localStorage Cache** (45 min)
3. **Database Indexes** (45 min)

**Expected Result:**
- Feels instant on first load
- IS instant on subsequent loads
- 90% faster backend queries

---

### Phase 2: If Still Not Fast Enough (optional)
4. **Redis Cache** (2 hours)
5. **Parallel Queries** (1 hour)

**Expected Result:**
- Sub-50ms response times
- All users benefit from cache

---

### Phase 3: Future Scaling (optional)
6. **Virtual Scrolling** - Only if handling 10,000+ stocks
7. **SSE** - Only if real-time updates needed

---

## 🛠️ Testing Performance

### Before Changes
```bash
# Measure current load time
curl -w "@curl-format.txt" http://localhost:8000/api/screener?page=1&limit=50
```

### After Each Change
```bash
# Compare improvement
# Should see time_total decrease significantly
```

### Browser DevTools
1. Open Network tab
2. Refresh page
3. Look for "screener" API call
4. Note: Size + Time

**Target:**
- **Before:** 200-300ms
- **After Phase 1:** 50-100ms + instant perceived load
- **After Phase 2:** 1-10ms

---

**Status:** Ready to implement! 🚀
