# AlgoTrading Data Architecture - Final Design

## Executive Summary

**3-Tier Architecture** (Cold → Warm → Hot)

| Layer | Purpose | Technology | Data Retention |
|-------|---------|------------|----------------|
| **Cold** | Immutable historical truth | NSE Parquet + DuckDB | 2016-2023+ (all time) |
| **Warm** | Operational queries | Postgres | Last 90-180 days |
| **Hot** | Real-time data | Fyers API + Cache | Current session |

**Key Principle**: Never duplicate Cold data in Warm layer. Use on-demand merging.

---

## Critical Design Decisions

### ✅ What's Locked In

1. **3-Tier Separation**: Cold/Warm/Hot layers remain distinct
2. **UnifiedDataService**: Single entry point for all queries
3. **NSE as Historical Truth**: Official source for backtests
4. **DuckDB for Parquet**: Fast, zero-infra reads
5. **No Full Backfill**: Postgres stays lean (90-180 days only)

### ❌ What's Removed

1. ~~Full NSE import to Postgres~~ → Use DuckDB on-demand
2. ~~Overloaded `historical_prices` schema~~ → Keep simple
3. ~~UnifiedDataService writes data~~ → Read-only service

---

## Data Source Priority (Final)

| Date Range | Source of Truth | Fallback |
|------------|----------------|----------|
| Today (market hours) | Fyers API | Postgres latest |
| Today (after hours) | Postgres | NSE Parquet |
| Last 30-60 days | Fyers → Postgres | NSE Parquet |
| Older than 60 days | **NSE Parquet** | Postgres (if exists) |
| **Backtests** | **NSE Adjusted Only** | - |

**Rule**: Never overwrite Fyers data with NSE for recent dates.

---

## Architecture Diagram

```
┌────────────────────────────────────────────┐
│         APPLICATIONS                        │
│  Screener │ Trader │ Portfolio │ Backtest  │
└──────────────────┬─────────────────────────┘
                   │
    ┌──────────────▼──────────────┐
    │   UnifiedDataService         │
    │  (routing + caching + merge) │
    │  READ-ONLY                   │
    └───┬─────────┬────────────┬───┘
        │         │            │
┌───────▼───┐ ┌──▼────┐  ┌────▼─────┐
│  Postgres │ │DuckDB │  │Fyers API │
│   (Warm)  │ │(Cold) │  │  (Hot)   │
│           │ │       │  │          │
│ Last 180d │ │  NSE  │  │Real-time │
│ Intraday  │ │Parquet│  │ Quotes   │
│ Signals   │ │       │  │          │
└───────────┘ └───────┘  └──────────┘
```

---

## Implementation Plan

### Phase 1: Database Schema (Simplified)

#### File: `backend/app/database.py`

**Keep `HistoricalPrice` simple** (Warm layer only):
```python
class HistoricalPrice(Base):
    """Daily OHLC - Recent data only (last 180 days)"""
    __tablename__ = "historical_prices"
    
    # ... existing fields ...
    source = Column(String(20), default='fyers')  # 'fyers' or 'yfinance'
    
    # NO adj_close needed here (use NSE Parquet for adjusted)
    # NO is_adjusted flag needed
```

**Migration**: No changes needed! Keep existing schema.

---

### Phase 2: NSE Data Reader (DuckDB)

#### New File: `backend/app/nse_data_reader.py`

```python
import duckdb
import pandas as pd
from datetime import date
from pathlib import Path

class NSEDataReader:
    """
    Read NSE Parquet files on-demand using DuckDB
    
    NO CACHING - DuckDB is fast enough
    NO POSTGRES WRITES - Pure read layer
    """
    
    def __init__(self, data_path: str = "nse_data/processed"):
        self.data_path = Path(data_path)
        self.con = duckdb.connect()  # In-memory
    
    def get_equity_data(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool = True  # Default to adjusted for backtests
    ) -> pd.DataFrame:
        """
        Read OHLCV from NSE Parquet
        
        Returns:
            DataFrame with index=date, columns=[open,high,low,close,volume]
        """
        subdir = "equities_adjusted" if adjusted else "equities_clean"
        path = self.data_path / subdir / "year=*" / "equity_ohlcv*.parquet"
        
        query = f"""
            SELECT 
                trade_date,
                open, high, low, close, volume
            FROM read_parquet('{path}')
            WHERE symbol = '{symbol}'
              AND trade_date BETWEEN '{start_date}' AND '{end_date}'
            ORDER BY trade_date
        """
        
        df = self.con.execute(query).df()
        
        if not df.empty:
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df = df.set_index('trade_date')
            df.index.name = 'date'
        
        return df
    
    def get_index_data(
        self,
        index_name: str,
        start_date: date,
        end_date: date
    ) -> pd.DataFrame:
        """Read index OHLC from NSE Parquet"""
        path = self.data_path / "indices_clean" / f"index={index_name}" / "index_ohlc.parquet"
        
        query = f"""
            SELECT trade_date, open, high, low, close
            FROM read_parquet('{path}')
            WHERE trade_date BETWEEN '{start_date}' AND '{end_date}'
            ORDER BY trade_date
        """
        
        df = self.con.execute(query).df()
        
        if not df.empty:
            df['trade_date'] = pd.to_datetime(df['trade_date'])
            df = df.set_index('trade_date')
            df.index.name = 'date'
        
        return df
    
    def check_symbol_exists(self, symbol: str) -> bool:
        """Quick check if symbol exists in NSE data"""
        path = self.data_path / "equities_clean" / "year=*" / "equity_ohlcv*.parquet"
        
        query = f"""
            SELECT COUNT(*) as cnt
            FROM read_parquet('{path}')
            WHERE symbol = '{symbol}'
            LIMIT 1
        """
        
        result = self.con.execute(query).fetchone()
        return result[0] > 0 if result else False
```

---

### Phase 3: Data Cache (Intent-Based Keys)

#### New File: `backend/app/data_cache.py`

```python
from datetime import datetime, timedelta
from typing import Dict, Optional, Any
import hashlib

class DataCache:
    """
    Intent-based TTL cache
    
    Cache Keys: {intent}:{symbol}:{params_hash}
    Examples:
      - screener:RELIANCE:daily
      - backtest:RELIANCE:5y_adjusted
      - live:RELIANCE:quote
    """
    
    def __init__(self):
        self.cache: Dict[str, tuple[Any, datetime]] = {}
        self.ttls = {
            'live': 30,           # 30 seconds
            'screener': 300,      # 5 minutes
            'backtest': 3600,     # 1 hour
            'intraday': 60        # 1 minute
        }
    
    def _make_key(self, intent: str, symbol: str, **params) -> str:
        """Generate cache key with intent prefix"""
        param_str = "_".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{intent}:{symbol}:{param_str}"
    
    def get(self, intent: str, symbol: str, **params) -> Optional[Any]:
        """Get cached value if not expired"""
        key = self._make_key(intent, symbol, **params)
        
        if key not in self.cache:
            return None
        
        value, cached_at = self.cache[key]
        ttl = self.ttls.get(intent, 300)
        
        if (datetime.now() - cached_at).total_seconds() > ttl:
            del self.cache[key]
            return None
        
        return value
    
    def set(self, intent: str, symbol: str, value: Any, **params):
        """Set cache with TTL"""
        key = self._make_key(intent, symbol, **params)
        self.cache[key] = (value, datetime.now())
    
    def invalidate(self, intent: str = None, symbol: str = None):
        """Invalidate cache entries"""
        if intent is None and symbol is None:
            self.cache.clear()
            return
        
        keys_to_delete = []
        for key in self.cache.keys():
            if intent and not key.startswith(f"{intent}:"):
                continue
            if symbol and f":{symbol}:" not in key:
                continue
            keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self.cache[key]
```

---

### Phase 4: Unified Data Service (Read-Only)

#### New File: `backend/app/unified_data_service.py`

```python
from datetime import date, timedelta
from typing import Optional
import pandas as pd
from sqlalchemy.orm import Session

from .data_repository import DataRepository
from .nse_data_reader import NSEDataReader
from .data_cache import DataCache
from .fyers_direct import get_fyers_quotes

class UnifiedDataService:
    """
    Smart data router - READ ONLY
    
    Responsibilities:
    - Route queries to appropriate layer
    - Merge data from multiple sources
    - Cache management
    - NO WRITES (writes happen in specific jobs only)
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.repo = DataRepository(db)
        self.nse_reader = NSEDataReader()
        self.cache = DataCache()
    
    def get_historical_prices(
        self,
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        days: Optional[int] = None,
        intent: str = 'screener',  # 'screener', 'backtest', etc.
        prefer_adjusted: bool = False
    ) -> pd.DataFrame:
        """
        Smart historical data fetch with layer routing
        
        Logic:
        1. Check cache (intent-based)
        2. Determine date range
        3. Route to appropriate layer(s)
        4. Merge if needed
        5. Cache result
        """
        
        # Calculate date range
        if days:
            end_date = end_date or date.today()
            start_date = end_date - timedelta(days=days)
        
        # Check cache
        cache_params = {
            'start': start_date,
            'end': end_date,
            'adjusted': prefer_adjusted
        }
        cached = self.cache.get(intent, symbol, **cache_params)
        if cached is not None:
            return cached
        
        # Determine cutoff (90 days ago)
        cutoff = date.today() - timedelta(days=90)
        
        df_warm = None
        df_cold = None
        
        # Get from Warm layer (Postgres) if date range overlaps recent data
        if end_date >= cutoff:
            df_warm = self.repo.get_historical_prices(
                symbol,
                start_date=max(start_date, cutoff) if start_date else cutoff,
                end_date=end_date
            )
        
        # Get from Cold layer (NSE Parquet) if date range includes old data
        if start_date < cutoff:
            df_cold = self.nse_reader.get_equity_data(
                symbol,
                start_date=start_date,
                end_date=min(end_date, cutoff),
                adjusted=prefer_adjusted
            )
        
        # Merge
        if df_warm is not None and df_cold is not None:
            # Combine, Warm takes priority for overlapping dates
            df = pd.concat([df_cold, df_warm])
            df = df[~df.index.duplicated(keep='last')]  # Keep Warm data
            df = df.sort_index()
        elif df_warm is not None:
            df = df_warm
        elif df_cold is not None:
            df = df_cold
        else:
            df = pd.DataFrame()
        
        # Cache result
        if not df.empty:
            self.cache.set(intent, symbol, df, **cache_params)
        
        return df
    
    def get_live_quote(self, symbol: str) -> dict:
        """
        Get real-time quote with fallback
        
        Priority:
        1. Fyers API (if available)
        2. Postgres latest
        3. NSE latest
        """
        
        # Check cache (30s TTL)
        cached = self.cache.get('live', symbol)
        if cached:
            return cached
        
        # Try Fyers
        try:
            quotes = get_fyers_quotes([symbol])
            if symbol in quotes:
                quote = quotes[symbol]
                self.cache.set('live', symbol, quote)
                return quote
        except:
            pass
        
        # Fallback to Postgres
        df = self.repo.get_historical_prices(symbol, days=1)
        if not df.empty:
            latest = df.iloc[-1]
            quote = {
                'ltp': float(latest['close']),
                'volume': int(latest['volume']),
                'source': 'postgres'
            }
            return quote
        
        # Fallback to NSE
        df = self.nse_reader.get_equity_data(
            symbol,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today()
        )
        if not df.empty:
            latest = df.iloc[-1]
            quote = {
                'ltp': float(latest['close']),
                'volume': int(latest['volume']),
                'source': 'nse'
            }
            return quote
        
        return {}
    
    def get_intraday_data(
        self,
        symbol: str,
        timeframe: str,  # '5m', '15m', '1h'
        days: int = 7
    ) -> pd.DataFrame:
        """
        Get intraday candles
        
        Priority:
        1. Postgres (if available)
        2. Fyers API (fetch but don't cache to DB)
        """
        
        # Check cache
        cached = self.cache.get('intraday', symbol, timeframe=timeframe, days=days)
        if cached is not None:
            return cached
        
        # Try Postgres
        df = self.repo.get_intraday_candles(symbol, timeframe, days=days)
        
        if df.empty:
            # Fetch from Fyers (but don't write to DB here)
            # Let dedicated intraday fetcher handle persistence
            pass
        
        # Cache result
        if not df.empty:
            self.cache.set('intraday', symbol, df, timeframe=timeframe, days=days)
        
        return df
```

---

### Phase 5: Update Data Repository (Add NSE Fallback)

#### File: `backend/app/data_repository.py`

**Add helper method** (don't modify existing methods):

```python
class DataRepository:
    # ... existing code ...
    
    def get_historical_prices_with_nse_fallback(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        prefer_adjusted: bool = False
    ) -> pd.DataFrame:
        """
        Get historical prices with automatic NSE fallback for gaps
        
        This is a NEW method - existing code unchanged
        """
        from .unified_data_service import UnifiedDataService
        
        service = UnifiedDataService(self.db)
        return service.get_historical_prices(
            symbol,
            start_date=start_date,
            end_date=end_date,
            prefer_adjusted=prefer_adjusted
        )
```

---

## Postgres Data Retention Policy

### Automated Cleanup (NEW)

#### File: `backend/cleanup_old_data.py`

```python
"""
Postgres Data Retention Policy

Keep Postgres lean by removing old data:
- historical_prices: Keep last 180 days only
- intraday_candles: Keep last 30 days only
"""

from datetime import date, timedelta
from app.database import SessionLocal, HistoricalPrice, IntradayCandle

def cleanup_old_historical_data(days_to_keep: int = 180):
    """Remove historical_prices older than N days"""
    db = SessionLocal()
    
    cutoff_date = date.today() - timedelta(days=days_to_keep)
    
    deleted = db.query(HistoricalPrice).filter(
        HistoricalPrice.date < cutoff_date
    ).delete()
    
    db.commit()
    db.close()
    
    print(f"Deleted {deleted:,} old historical price records (before {cutoff_date})")

def cleanup_old_intraday_data(days_to_keep: int = 30):
    """Remove intraday_candles older than N days"""
    db = SessionLocal()
    
    cutoff_date = date.today() - timedelta(days=days_to_keep)
    
    deleted = db.query(IntradayCandle).filter(
        IntradayCandle.timestamp < cutoff_date
    ).delete()
    
    db.commit()
    db.close()
    
    print(f"Deleted {deleted:,} old intraday candle records (before {cutoff_date})")

if __name__ == "__main__":
    cleanup_old_historical_data(days_to_keep=180)
    cleanup_old_intraday_data(days_to_keep=30)
```

**Cron**: Run weekly
```bash
# Add to crontab
0 2 * * 0 cd /path/to/AlgoTrading/backend && python cleanup_old_data.py
```

---

## Execution Plan

### Week 1: Foundation
- [x] ✅ NSE data pipeline (COMPLETE)
- [ ] Implement NSE Data Reader (DuckDB)
- [ ] Implement Data Cache (intent-based)
- [ ] Create cleanup script

### Week 2: Integration
- [ ] Implement Unified Data Service
- [ ] Add fallback method to Data Repository
- [ ] Test with sample queries
- [ ] Set up data retention policy

### Week 3: Application Updates
- [ ] Update screener to use UnifiedDataService
- [ ] Update backtests to use adjusted prices
- [ ] Update Smart Trader (keep existing intraday)
- [ ] Update Portfolio Risk

### Week 4: Validation
- [ ] Performance testing
- [ ] Cache hit rate monitoring
- [ ] Data quality validation
- [ ] Documentation

---

## Usage Examples

### Example 1: Screener (Last 200 Days)

```python
from app.unified_data_service import UnifiedDataService
from app.database import SessionLocal

db = SessionLocal()
service = UnifiedDataService(db)

# Automatic routing: Postgres (recent) + NSE (if gaps)
df = service.get_historical_prices(
    'RELIANCE',
    days=200,
    intent='screener'
)

# Behind the scenes:
# - Last 90 days from Postgres (Fyers data)
# - Days 91-200 from NSE Parquet
# - Merged seamlessly
```

### Example 2: Backtest (5 Years, Adjusted)

```python
# Always use adjusted prices for backtests
df = service.get_historical_prices(
    'RELIANCE',
    start_date=date(2019, 1, 1),
    end_date=date(2024, 1, 1),
    intent='backtest',
    prefer_adjusted=True  # Uses NSE adjusted Parquet
)

# Behind the scenes:
# - 2019-2023: NSE Parquet (adjusted)
# - 2024: Postgres (recent, unadjusted)
# - Merged with proper handling
```

### Example 3: Live Trading

```python
# Get live quote
quote = service.get_live_quote('RELIANCE')
# → Fyers API (if market hours)
# → Postgres (if after hours)
# → NSE (fallback)

# Get intraday (existing flow unchanged)
candles = service.get_intraday_data('RELIANCE', '5m', days=7)
# → Postgres intraday_candles
```

---

## Responsibilities Matrix

| Function | Reads | Writes | Source |
|----------|-------|--------|--------|
| Screener | ✅ | ❌ | UnifiedDataService |
| Backtest | ✅ | ❌ | UnifiedDataService |
| Smart Trader | ✅ | Signals only | UnifiedDataService + DB |
| Trade Terminal | ✅ | Trades only | UnifiedDataService + DB |
| UnifiedDataService | ✅ | ❌ | Multi-source |
| Fyers Update Job | ❌ | ✅ | Postgres only |
| Signal Persistence | ❌ | ✅ | Postgres only |

**Rule**: UnifiedDataService is READ-ONLY. Writes happen in specific jobs.

---

## Benefits

### 1. Clean Separation
- ✅ Cold layer stays immutable (NSE Parquet)
- ✅ Warm layer stays lean (90-180 days)
- ✅ No data duplication

### 2. Performance
- ✅ DuckDB is fast enough (no need to import)
- ✅ Intent-based caching prevents pollution
- ✅ Postgres stays small and fast

### 3. Maintainability
- ✅ Single entry point (UnifiedDataService)
- ✅ Existing code mostly unchanged
- ✅ Easy to add new sources

### 4. Correctness
- ✅ NSE adjusted prices for backtests
- ✅ Fyers for recent/live data
- ✅ Clear priority rules

---

## Migration Checklist

- [ ] Implement NSE Data Reader
- [ ] Implement Data Cache
- [ ] Implement Unified Data Service
- [ ] Add fallback method to Data Repository
- [ ] Create cleanup script
- [ ] Update screener to use UnifiedDataService
- [ ] Update backtests to use adjusted prices
- [ ] Set up weekly cleanup cron job
- [ ] Monitor cache hit rates
- [ ] Document for team

---

## Rollback Plan

If issues arise:

1. **Disable NSE integration**:
   ```python
   # In unified_data_service.py
   ENABLE_NSE = False
   ```

2. **Revert to existing DataRepository**:
   - All existing code still works
   - UnifiedDataService is additive, not replacement

3. **No database changes needed** (schema unchanged)

---

## Future Enhancements

1. **Daily NSE Updates**: Cron to download latest bhavcopy
2. **Data Quality Dashboard**: Monitor gaps, staleness
3. **Smart Prefetching**: Predict queries, warm cache
4. **Redis Cache**: For multi-instance deployments
5. **Materialized Views**: For common backtest queries


## Executive Summary

**Current State**: Multiple disconnected data sources
- Postgres: Daily OHLC + intraday candles
- Fyers API: Live quotes + historical fetch
- NSE Pipeline: Historical Parquet files (2016-2023)

**Target State**: Unified 3-tier data architecture
- **Layer 1 (Cold)**: NSE Historical Data (Parquet) - 2016-2023
- **Layer 2 (Warm)**: Postgres Operational Data - Last 2 years + intraday
- **Layer 3 (Hot)**: Fyers Live Data - Real-time quotes

---

## System Analysis

### Existing Data Sources

#### 1. Postgres Database (`algotrading`)

**Tables**:
```sql
companies              -- Master table (symbol, sector, is_fno)
historical_prices      -- Daily OHLC + indicators (source: fyers/yfinance)
intraday_candles       -- 5m/15m/1h candles (source: fyers)
financial_statements   -- Quarterly/Annual financials
sector_membership      -- Time-aware sector mapping (NEW from NSE)
index_membership       -- Time-aware index constituents (NEW from NSE)
corporate_actions      -- Splits/bonuses (NEW from NSE)
```

**Current Usage**:
- Screener: Reads `historical_prices` for indicators
- Smart Trader: Reads `intraday_candles` for 5m signals
- Portfolio Risk: Reads `historical_prices` for correlation
- Strategies: Reads both daily + intraday

#### 2. Fyers API

**Endpoints Used**:
- `quotes()` - Live LTP, volume, OHLC
- `history()` - Historical daily/intraday data
- `depth()` - Order book (not currently used)

**Current Usage**:
- Live quotes for trending scanner
- Historical backfill (last 365 days)
- Intraday data fetch (5m candles)
- Option premium fetch

#### 3. NSE Pipeline (NEW)

**Data Files**:
```
nse_data/processed/
├── equities_clean/year=*/equity_ohlcv.parquet
├── equities_adjusted/year=*/equity_ohlcv_adj.parquet
└── indices_clean/index=*/index_ohlc.parquet
```

**Coverage**: 2016-2023, ~1600 stocks, corporate-action adjusted

---

## Proposed Architecture

### 3-Tier Data Layer

```
┌─────────────────────────────────────────────────────────┐
│              APPLICATION LAYER                           │
│  (Screener, Strategies, Portfolio, Smart Trader)        │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────▼────────────┐
        │  UNIFIED DATA SERVICE   │
        │  (Smart Query Router)   │
        └────────┬────────────────┘
                 │
    ┌────────────┼────────────┐
    │            │            │
┌───▼────┐  ┌───▼────┐  ┌───▼────┐
│ LAYER 3│  │ LAYER 2│  │ LAYER 1│
│  HOT   │  │  WARM  │  │  COLD  │
│        │  │        │  │        │
│ Fyers  │  │Postgres│  │  NSE   │
│  API   │  │   DB   │  │Parquet │
│        │  │        │  │        │
│Real-time│ │Last 2yr│  │2016-23 │
│ Quotes │  │Intraday│  │Adjusted│
└────────┘  └────────┘  └────────┘
```

### Data Routing Logic

**Query Type → Data Source Priority**

| Query Type | Primary | Secondary | Tertiary |
|------------|---------|-----------|----------|
| Live Quote (today) | Fyers API | Postgres (latest) | - |
| Recent History (<30d) | Postgres | Fyers API | - |
| Historical (>30d, <2yr) | Postgres | NSE Parquet | - |
| Deep Historical (>2yr) | NSE Parquet | Postgres | - |
| Adjusted Prices | NSE Adjusted | Postgres (if available) | - |
| Intraday (5m/15m) | Postgres | Fyers API | - |
| Index Data | NSE Parquet | - | - |

---

## Implementation Plan

### Phase 1: Database Schema Updates

#### File: `backend/app/database.py`

**Modify `HistoricalPrice` model**:
```python
class HistoricalPrice(Base):
    # ... existing fields ...
    
    # NEW: Corporate action adjusted close
    adj_close = Column(Float, nullable=True)  # ✅ Already exists
    
    # NEW: Data source tracking
    source = Column(String(20), default='fyers')  # ✅ Already exists
    # Values: 'fyers', 'nse', 'yfinance'
    
    # NEW: Data quality flag
    is_adjusted = Column(Boolean, default=False)  # NEW
    # True if adj_close is populated from corporate actions
```

**Migration Script**: `backend/migrations/add_data_quality_flags.py`
```sql
ALTER TABLE historical_prices 
ADD COLUMN IF NOT EXISTS is_adjusted BOOLEAN DEFAULT FALSE;

-- Mark NSE data as adjusted (after import)
UPDATE historical_prices 
SET is_adjusted = TRUE 
WHERE source = 'nse' AND adj_close IS NOT NULL;
```

---

### Phase 2: Unified Data Service

#### New File: `backend/app/unified_data_service.py`

**Purpose**: Single entry point for all data queries

```python
class UnifiedDataService:
    """
    Unified data access layer with intelligent routing
    
    Responsibilities:
    - Route queries to appropriate data source
    - Handle fallbacks
    - Cache management
    - Data quality checks
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.repo = DataRepository(db)
        self.nse_reader = NSEDataReader()  # NEW
        self.fyers_client = FyersClient()  # Existing
        self.cache = DataCache()  # NEW
    
    def get_historical_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        prefer_adjusted: bool = False,
        include_live: bool = False
    ) -> pd.DataFrame:
        """
        Smart historical data fetch with source priority
        
        Logic:
        1. Check cache
        2. Determine date range age
        3. Route to appropriate source
        4. Merge live data if requested
        5. Cache result
        """
        
    def get_live_quote(self, symbol: str) -> Dict:
        """
        Get real-time quote with fallback
        
        Priority:
        1. Fyers API (if market hours)
        2. Postgres latest record
        3. NSE latest record
        """
        
    def get_intraday_data(
        self,
        symbol: str,
        timeframe: str,  # '5m', '15m', '1h'
        days: int = 7
    ) -> pd.DataFrame:
        """
        Get intraday candles
        
        Priority:
        1. Postgres (if available)
        2. Fyers API (fetch and cache)
        """
```

---

### Phase 3: NSE Data Reader

#### New File: `backend/app/nse_data_reader.py`

**Purpose**: Read NSE Parquet files efficiently

```python
import duckdb
import pandas as pd
from datetime import date
from typing import Optional

class NSEDataReader:
    """
    Efficient reader for NSE Parquet files using DuckDB
    """
    
    def __init__(self, data_path: str = "nse_data/processed"):
        self.data_path = data_path
        self.con = duckdb.connect()  # In-memory DuckDB
    
    def get_equity_data(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool = False
    ) -> pd.DataFrame:
        """
        Read equity OHLCV from Parquet
        
        Args:
            symbol: Stock symbol
            start_date: Start date
            end_date: End date
            adjusted: Use corporate-action adjusted prices
        
        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        path = f"{self.data_path}/equities_adjusted" if adjusted else f"{self.data_path}/equities_clean"
        
        query = f"""
            SELECT 
                trade_date as date,
                open, high, low, close, volume,
                turnover
            FROM read_parquet('{path}/year=*/equity_ohlcv*.parquet')
            WHERE symbol = '{symbol}'
              AND trade_date BETWEEN '{start_date}' AND '{end_date}'
            ORDER BY trade_date
        """
        
        return self.con.execute(query).df()
    
    def get_index_data(
        self,
        index_name: str,
        start_date: date,
        end_date: date
    ) -> pd.DataFrame:
        """Read index OHLC from Parquet"""
        
    def check_symbol_exists(self, symbol: str) -> bool:
        """Check if symbol exists in NSE data"""
        
    def get_available_date_range(self, symbol: str) -> tuple:
        """Get min/max dates available for symbol"""
```

---

### Phase 4: Data Cache Layer

#### New File: `backend/app/data_cache.py`

**Purpose**: In-memory cache for frequently accessed data

```python
from datetime import datetime, timedelta
from typing import Dict, Optional
import pandas as pd

class DataCache:
    """
    TTL-based cache for data queries
    
    Cache Strategy:
    - Live quotes: 30 seconds
    - Recent history (<7d): 5 minutes
    - Historical (>7d): 1 hour
    - Intraday: 1 minute
    """
    
    def __init__(self):
        self.cache = {}
        self.ttls = {
            'live_quote': 30,
            'recent_history': 300,
            'historical': 3600,
            'intraday': 60
        }
    
    def get(self, key: str, cache_type: str) -> Optional[any]:
        """Get cached value if not expired"""
        
    def set(self, key: str, value: any, cache_type: str):
        """Set cache with TTL"""
        
    def invalidate(self, pattern: str = None):
        """Invalidate cache entries matching pattern"""
```

---

### Phase 5: NSE Data Import

#### New File: `backend/import_nse_historical.py`

**Purpose**: One-time backfill from NSE Parquet → Postgres

```python
"""
NSE Historical Data Importer

Imports NSE bhavcopy data from Parquet files into Postgres.
Handles:
- Deduplication (skip if exists)
- Source tracking (mark as 'nse')
- Adjusted prices (populate adj_close)
- Batch inserts (1000 records at a time)
"""

import os
import pandas as pd
import duckdb
from datetime import date, timedelta
from app.database import SessionLocal, Company, HistoricalPrice
from app.data_repository import DataRepository
from dotenv import load_dotenv

load_dotenv()

# Configuration
NSE_DATA_PATH = "nse_data/processed"
CUTOFF_DAYS = 30  # Only import data older than N days
BATCH_SIZE = 1000
DRY_RUN = False  # Set to True for testing

def import_nse_data(
    start_year: int = 2016,
    end_year: int = 2023,
    symbols: list = None,  # None = all symbols
    use_adjusted: bool = True
):
    """
    Import NSE historical data into Postgres
    
    Args:
        start_year: Start year (inclusive)
        end_year: End year (inclusive)
        symbols: List of symbols to import (None = all)
        use_adjusted: Use corporate-action adjusted prices
    """
    
    db = SessionLocal()
    repo = DataRepository(db)
    con = duckdb.connect()
    
    # Determine cutoff date (don't import recent data)
    cutoff_date = date.today() - timedelta(days=CUTOFF_DAYS)
    
    # Read NSE data
    data_path = f"{NSE_DATA_PATH}/equities_adjusted" if use_adjusted else f"{NSE_DATA_PATH}/equities_clean"
    
    query = f"""
        SELECT 
            symbol,
            trade_date,
            open, high, low, close, volume, turnover
        FROM read_parquet('{data_path}/year=*/equity_ohlcv*.parquet')
        WHERE trade_date < '{cutoff_date}'
          AND trade_date >= '{start_year}-01-01'
          AND trade_date <= '{end_year}-12-31'
    """
    
    if symbols:
        symbols_str = "','".join(symbols)
        query += f" AND symbol IN ('{symbols_str}')"
    
    query += " ORDER BY symbol, trade_date"
    
    df = con.execute(query).df()
    
    print(f"Found {len(df):,} records to import")
    
    if DRY_RUN:
        print("DRY RUN - No data will be imported")
        print(df.head(10))
        return
    
    # Import in batches
    imported = 0
    skipped = 0
    
    for symbol, group in df.groupby('symbol'):
        # Get or create company
        company = repo.get_or_create_company(symbol)
        
        for _, row in group.iterrows():
            # Check if exists
            existing = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == company.id,
                HistoricalPrice.date == row['trade_date']
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            # Insert new record
            price = HistoricalPrice(
                company_id=company.id,
                date=row['trade_date'],
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                volume=int(row['volume']),
                adj_close=row['close'] if use_adjusted else None,
                source='nse',
                is_adjusted=use_adjusted
            )
            
            db.add(price)
            imported += 1
            
            # Commit in batches
            if imported % BATCH_SIZE == 0:
                db.commit()
                print(f"Imported: {imported:,} | Skipped: {skipped:,}")
    
    # Final commit
    db.commit()
    db.close()
    
    print(f"\n✅ Import complete!")
    print(f"   Imported: {imported:,}")
    print(f"   Skipped:  {skipped:,}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-year', type=int, default=2016)
    parser.add_argument('--end-year', type=int, default=2023)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--symbols', nargs='+', help='Specific symbols to import')
    parser.add_argument('--no-adjusted', action='store_true', help='Use raw prices instead of adjusted')
    
    args = parser.parse_args()
    
    DRY_RUN = args.dry_run
    
    import_nse_data(
        start_year=args.start_year,
        end_year=args.end_year,
        symbols=args.symbols,
        use_adjusted=not args.no_adjusted
    )
```

---

### Phase 6: Update Data Repository

#### File: `backend/app/data_repository.py`

**Modify existing methods to use Unified Data Service**:

```python
class DataRepository:
    # ... existing code ...
    
    def get_historical_prices(
        self, 
        symbol: str,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        days: Optional[int] = None,
        prefer_adjusted: bool = False,  # NEW
        include_nse: bool = True  # NEW
    ) -> pd.DataFrame:
        """
        Get historical prices with NSE fallback
        
        NEW LOGIC:
        1. Try Postgres first
        2. If gaps exist and include_nse=True, fill from NSE
        3. Merge and return
        """
        
        # Get from Postgres
        df_pg = self._get_from_postgres(symbol, start_date, end_date, days)
        
        if not include_nse or df_pg is None:
            return df_pg
        
        # Check for gaps
        if self._has_gaps(df_pg, start_date, end_date):
            # Fill gaps from NSE
            from .nse_data_reader import NSEDataReader
            nse_reader = NSEDataReader()
            df_nse = nse_reader.get_equity_data(
                symbol, 
                start_date or (date.today() - timedelta(days=days)),
                end_date or date.today(),
                adjusted=prefer_adjusted
            )
            
            # Merge (Postgres takes priority)
            df = self._merge_dataframes(df_pg, df_nse)
            return df
        
        return df_pg
```

---

## Execution Plan

### Week 1: Foundation
- [x] ✅ NSE data pipeline (COMPLETE)
- [ ] Database schema migration
- [ ] NSE Data Reader implementation
- [ ] Data Cache implementation

### Week 2: Integration
- [ ] Unified Data Service implementation
- [ ] Update Data Repository with NSE fallback
- [ ] NSE data import (one-time backfill)
- [ ] Testing & validation

### Week 3: Application Updates
- [ ] Update screener to use Unified Service
- [ ] Update Smart Trader to use Unified Service
- [ ] Update Portfolio Risk to use adjusted prices
- [ ] Update strategies to use Unified Service

### Week 4: Optimization
- [ ] Performance tuning
- [ ] Cache optimization
- [ ] Monitoring & logging
- [ ] Documentation

---

## Data Flow Examples

### Example 1: Screener Query (Last 200 days)

```python
# OLD CODE
df = repo.get_historical_prices('RELIANCE', days=200)

# NEW CODE (same interface, smarter routing)
df = repo.get_historical_prices('RELIANCE', days=200, include_nse=True)

# Behind the scenes:
# 1. Check Postgres (finds last 180 days from Fyers)
# 2. Detect 20-day gap
# 3. Fill gap from NSE Parquet
# 4. Merge and return 200 days
```

### Example 2: Backtest (5 years, adjusted prices)

```python
# NEW CODE
df = repo.get_historical_prices(
    'RELIANCE',
    start_date=date(2019, 1, 1),
    end_date=date(2024, 1, 1),
    prefer_adjusted=True,  # Use corporate-action adjusted
    include_nse=True
)

# Behind the scenes:
# 1. Postgres has 2023-2024 (Fyers)
# 2. NSE Parquet has 2019-2022 (adjusted)
# 3. Merge both sources
# 4. Return 5 years of adjusted prices
```

### Example 3: Live Trading (Smart Trader)

```python
# Get live quote
quote = unified_service.get_live_quote('RELIANCE')
# → Fyers API (if market hours)
# → Postgres latest (if after hours)

# Get 5m candles for signal
candles = unified_service.get_intraday_data('RELIANCE', '5m', days=7)
# → Postgres (if cached)
# → Fyers API (if not cached)
```

---

## Benefits

### 1. Data Completeness
- ✅ 8+ years of historical data (vs 1 year)
- ✅ Corporate-action adjusted prices
- ✅ Full NSE universe coverage

### 2. Performance
- ✅ Intelligent caching (30s for live, 1h for historical)
- ✅ DuckDB for fast Parquet queries
- ✅ Postgres for operational data

### 3. Reliability
- ✅ Multi-source fallback (NSE → Postgres → Fyers)
- ✅ No single point of failure
- ✅ Graceful degradation

### 4. Maintainability
- ✅ Single entry point (Unified Service)
- ✅ Clear separation of concerns
- ✅ Easy to add new sources

---

## Migration Checklist

- [ ] Run database migration (add `is_adjusted` column)
- [ ] Implement NSE Data Reader
- [ ] Implement Data Cache
- [ ] Implement Unified Data Service
- [ ] Run NSE data import (2016-2023)
- [ ] Update Data Repository
- [ ] Update all application code to use new service
- [ ] Run integration tests
- [ ] Deploy to production
- [ ] Monitor performance

---

## Rollback Plan

If issues arise:

1. **Disable NSE integration**:
   ```python
   # In unified_data_service.py
   ENABLE_NSE = False  # Fallback to Postgres only
   ```

2. **Revert database**:
   ```sql
   DELETE FROM historical_prices WHERE source = 'nse';
   ```

3. **Restore backup**:
   ```bash
   pg_restore -d algotrading backup.sql
   ```

---

## Future Enhancements

1. **Real-time NSE Updates**: Daily cron to import latest bhavcopy
2. **Data Quality Dashboard**: Monitor source health, gaps, staleness
3. **Smart Prefetching**: Predict queries and prefetch data
4. **Distributed Cache**: Redis for multi-instance deployments
5. **Data Versioning**: Track schema changes and migrations


## Problem Statement

We have two data sources that need to coexist:

1. **Existing System**: Fyers API → `historical_prices` table (Postgres/SQLite)
   - Daily updates for ~200 active stocks
   - Last 365 days of data
   - Used by: screener, strategies, portfolio analysis

2. **New System**: NSE Bhavcopy → Parquet files
   - Historical data (2016-2023+)
   - Complete NSE universe (~1600+ stocks)
   - Corporate-action adjusted prices

## User Review Required

> [!IMPORTANT]
> **Key Decision: Data Source Priority**
> 
> When both NSE and Fyers data exist for the same symbol/date, which should take precedence?
> 
> **Recommendation**: Use NSE as primary source for historical data (>30 days old), Fyers for recent data (<30 days)
> 
> **Rationale**:
> - NSE bhavcopy is official source
> - Fyers may have slight differences in OHLC
> - Corporate-action adjusted prices from NSE are more accurate

> [!WARNING]
> **Breaking Change: Database Schema**
> 
> Need to add `adj_close` column to `historical_prices` table if not already present.
> This will store corporate-action adjusted prices from NSE pipeline.

## Proposed Changes

### 1. Data Integration Strategy

**Approach**: Hybrid model with clear separation

```
┌─────────────────────────────────────────────┐
│         Application Layer                    │
│  (Screener, Strategies, Portfolio Analysis) │
└──────────────────┬──────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
┌───────▼────────┐   ┌────────▼────────┐
│ Postgres/SQLite│   │  NSE Parquet    │
│ (Recent Data)  │   │ (Historical)    │
│                │   │                 │
│ • Last 365 days│   │ • 2016-2023     │
│ • Fyers source │   │ • Full universe │
│ • Real-time LTP│   │ • CA-adjusted   │
└────────────────┘   └─────────────────┘
```

---

### 2. Database Schema Updates

#### Modify `historical_prices` table

**File**: `backend/app/database.py`

Add/verify columns:
```python
adj_close = Column(Float, nullable=True)  # Corporate-action adjusted
data_source = Column(String(20), default='fyers')  # 'fyers', 'nse', 'yfinance'
```

**Migration Script**: `backend/migrate_add_nse_support.py`

```sql
ALTER TABLE historical_prices 
ADD COLUMN IF NOT EXISTS adj_close FLOAT;

ALTER TABLE historical_prices 
ADD COLUMN IF NOT EXISTS data_source VARCHAR(20) DEFAULT 'fyers';
```

---

### 3. NSE Data Importer

**New File**: `backend/import_nse_historical.py`

**Purpose**: One-time backfill of historical data from NSE Parquet files into Postgres

**Logic**:
1. Read NSE Parquet files (`nse_data/processed/equities_clean/year=*/equity_ohlcv.parquet`)
2. For each symbol/date:
   - Check if record exists in `historical_prices`
   - If exists and source='fyers': **SKIP** (keep Fyers data for recent dates)
   - If not exists: **INSERT** with source='nse'
3. Batch insert for performance (1000 records at a time)

**Key Features**:
- Date range filter (only import data older than 30 days)
- Symbol filter (only import active companies)
- Progress tracking
- Dry-run mode for testing

---

### 4. Data Repository Updates

#### File: `backend/app/data_repository.py`

**Modify**: `get_historical_prices()` method

Add source priority logic:
```python
def get_historical_prices(
    self, 
    symbol: str,
    days: int = None,
    prefer_adjusted: bool = False  # NEW PARAMETER
) -> pd.DataFrame:
    """
    Get historical prices with source priority
    
    If prefer_adjusted=True:
      - Use adj_close from NSE data when available
      - Fall back to close price otherwise
    """
```

**Modify**: `save_historical_prices()` method

Add conflict resolution:
```python
def save_historical_prices(
    self, 
    symbol: str, 
    df: pd.DataFrame, 
    source: str = 'fyers',
    overwrite: bool = False  # NEW PARAMETER
):
    """
    Save with source tracking
    
    If overwrite=False (default):
      - Skip if record exists (preserve existing data)
    If overwrite=True:
      - Update existing record
    """
```

---

### 5. Adjusted Price Integration

#### File: `backend/app/adjusted_price_fetcher.py` (NEW)

**Purpose**: Fetch corporate-action adjusted prices for backtesting

```python
def get_adjusted_prices(
    symbol: str,
    start_date: date,
    end_date: date
) -> pd.DataFrame:
    """
    Get adjusted prices from NSE pipeline
    
    Priority:
    1. NSE adjusted data (nse_data/processed/equities_adjusted/)
    2. NSE raw data with adj_close from DB
    3. Fyers data (unadjusted)
    """
```

---

### 6. Configuration

#### File: `backend/config.py` or `.env`

Add configuration:
```python
# NSE Data Integration
NSE_DATA_PATH = "nse_data/processed"
NSE_IMPORT_CUTOFF_DAYS = 30  # Only import data older than N days
NSE_PREFER_ADJUSTED = True   # Use adjusted prices for backtesting
```

---

### 7. Execution Order

**Phase 1: Schema Migration** (5 mins)
```bash
python backend/migrate_add_nse_support.py
```

**Phase 2: NSE Data Import** (30-60 mins for 2016-2023)
```bash
# Dry run first
python backend/import_nse_historical.py --dry-run --start-year 2016 --end-year 2023

# Actual import
python backend/import_nse_historical.py --start-year 2016 --end-year 2023
```

**Phase 3: Verification**
```bash
python backend/verify_nse_integration.py
```

---

## Verification Plan

### 1. Schema Verification
```bash
# Check columns exist
python -c "from backend.app.database import HistoricalPrice; print(HistoricalPrice.__table__.columns.keys())"
```

**Expected**: Should include `adj_close` and `data_source`

### 2. Data Import Verification

**Script**: `backend/verify_nse_integration.py`

Checks:
- Record count before/after import
- Data source distribution (% from NSE vs Fyers)
- Date range coverage
- Sample data comparison (RELIANCE, TCS, INFY)

**Manual Check**:
```sql
-- Check NSE data was imported
SELECT data_source, COUNT(*), MIN(date), MAX(date)
FROM historical_prices
GROUP BY data_source;

-- Verify no duplicates
SELECT company_id, date, COUNT(*)
FROM historical_prices
GROUP BY company_id, date
HAVING COUNT(*) > 1;
```

### 3. Application Integration Test

**Test**: Run existing screener with NSE data
```bash
# Start backend
cd backend
python main.py

# Open frontend
cd ../frontend
npm run dev

# Manual test:
# 1. Navigate to screener
# 2. Check if historical data loads correctly
# 3. Verify date ranges extend to 2016 (if NSE data imported)
```

### 4. Backtest Verification

**Test**: Run a simple backtest strategy
```python
# backend/test_nse_backtest.py
from app.data_repository import DataRepository
from app.database import SessionLocal

db = SessionLocal()
repo = DataRepository(db)

# Get 5 years of data (should include NSE data)
df = repo.get_historical_prices('RELIANCE', days=1825)

print(f"Records: {len(df)}")
print(f"Date range: {df.index.min()} to {df.index.max()}")
print(f"Has adjusted close: {'adj_close' in df.columns}")
```

**Expected**:
- 1200+ records (5 years of trading days)
- Date range starting from 2019 or earlier
- Adjusted close column present

---

## Rollback Plan

If integration causes issues:

1. **Revert schema**:
```sql
ALTER TABLE historical_prices DROP COLUMN adj_close;
ALTER TABLE historical_prices DROP COLUMN data_source;
```

2. **Delete NSE data**:
```sql
DELETE FROM historical_prices WHERE data_source = 'nse';
```

3. **Restore from backup**:
```bash
cp backend/data/screener_backup.db backend/data/screener.db
```

---

## Future Enhancements

1. **Automated NSE Updates**: Daily cron job to import latest NSE bhavcopy
2. **Data Quality Checks**: Compare NSE vs Fyers for overlapping dates
3. **Corporate Actions UI**: Display splits/bonuses in stock detail page
4. **Index Membership**: Use NSE index membership for sector rotation strategies
5. **Sector Classification**: Update company sectors from NSE EQUITY_L.csv

---

## Summary

**What Changes**:
- Database schema (2 new columns)
- Data repository (source priority logic)
- New importer script (one-time backfill)
- New fetcher for adjusted prices

**What Stays Same**:
- Existing Fyers update workflow
- Frontend code (no changes needed)
- Strategy/backtest code (transparent upgrade)

**Benefits**:
- 8+ years of historical data (vs 1 year)
- Corporate-action adjusted prices
- Full NSE universe coverage
- Official NSE data source
