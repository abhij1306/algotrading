# SCREENER MODULE - Technical Documentation

**Version:** 2.0.0  
**Last Updated:** 2025-12-22  
**Module:** Stock Screener & Technical Analysis

---

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Data Pipeline](#data-pipeline)
4. [Technical Logic](#technical-logic)
5. [Assumptions](#assumptions)
6. [Module Interactions](#module-interactions)
7. [API Reference](#api-reference)
8. [Database Schema](#database-schema)
9. [Indicator Calculations](#indicator-calculations)
10. [Performance Optimizations](#performance-optimizations)

---

## 1. Overview

### Purpose
The Screener module provides real-time and historical stock screening capabilities based on technical indicators, fundamentals, and custom filters. It enables users to discover trading opportunities across NSE equities.

### Key Features
- **Index-based filtering**: Filter by NIFTY50, BANKNIFTY, NIFTY200, etc.
- **Technical screening**: RSI, MACD, Bollinger Bands, ADX, Stochastic
- **Volume analysis**: Volume shockers, OBV tracking
- **Trend detection**: 7-day and 30-day trend analysis
- **Financial screening**: P/E, ROE, EPS, Debt/Equity ratios
- **Real-time updates**: 3-second polling for live data

### Technology Stack
- **Backend**: FastAPI (Python)
- **Database**: PostgreSQL
- **Frontend**: Next.js (React)
- **Data Source**: Fyers API v3, NSE Bhavcopy

---

## 2. Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                     SCREENER MODULE                          │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐      ┌──────────────┐    ┌─────────────┐ │
│  │   Frontend   │◄────►│   API Layer  │◄──►│  Database   │ │
│  │  (Next.js)   │      │   (FastAPI)  │    │ (Postgres)  │ │
│  └──────────────┘      └──────────────┘    └─────────────┘ │
│         │                      │                    │        │
│         │                      ▼                    │        │
│         │            ┌──────────────────┐          │        │
│         │            │ Indicator Engine │          │        │
│         │            │  (TA-Lib/Pandas) │          │        │
│         │            └──────────────────┘          │        │
│         │                                           │        │
│         └───────────────┬───────────────────────────┘        │
│                         ▼                                     │
│              ┌────────────────────┐                          │
│              │   Data Sources     │                          │
│              │  • Fyers API       │                          │
│              │  • NSE Bhavcopy    │                          │
│              │  • Cached DB       │                          │
│              └────────────────────┘                          │
└─────────────────────────────────────────────────────────────┘
```

### Data Flow

```
User Request
    ↓
[Index Filter] → [Sector Filter] → [Scanner Filter]
    ↓
Backend API (/api/screener/)
    ↓
Query Builder (applies filters)
    ↓
Database Query (historical_prices + companies)
    ↓
Indicator Calculation (if needed)
    ↓
Score Calculation (technical_score)
    ↓
Sorting & Pagination
    ↓
JSON Response → Frontend → Table Render
```

---

## 3. Data Pipeline

### 3.1 Data Ingestion

#### Daily Bhavcopy Update
**File:** `backend/scripts/update_bhavcopy.py`

```python
# Runs daily at market close (3:45 PM IST)
1. Download NSE bhavcopy (EQ + FO)
2. Parse CSV → Extract OHLCV
3. INSERT INTO historical_prices
4. ON CONFLICT UPDATE (upsert)
5. Calculate indicators for new data
```

**Assumptions:**
- NSE bhavcopy is published by 3:30 PM IST
- Data is final and won't be revised
- Corporate actions are handled separately

#### Intraday Data (Fyers API)
**File:** `backend/app/data_fetcher.py`

```python
# Fetches on-demand for selected symbols
fetch_fyers_quotes(symbols: List[str]) → Dict
    ↓
Returns: { symbol: { ltp, volume, change_pct, ... } }
    ↓
Merged with cached DB data for display
```

**Rate Limits:**
- 10 requests/second
- 100 symbols/request
- Cached for 3 seconds

### 3.2 Indicator Calculation

**File:** `backend/app/indicators.py`

Indicators are **pre-calculated** and stored in `historical_prices` table during daily updates.

#### Calculation Trigger Points:
1. **Daily Batch:** After bhavcopy import (3:45 PM)
2. **On-Demand:** When new symbol data is added
3. **Backfill:** Manual script for historical data

#### Calculation Logic:

```python
def calculate_indicators(df: DataFrame) → DataFrame:
    """
    Input: OHLCV DataFrame (sorted by date ASC)
    Output: Same DF + indicator columns
    """
    # Moving Averages
    df['ema_20'] = df['close'].ewm(span=20).mean()
    df['ema_50'] = df['close'].ewm(span=50).mean()
    
    # RSI (14-period)
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).ewm(span=14).mean()
    loss = -delta.where(delta < 0, 0).ewm(span=14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    
    # ATR (14-period)
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift())
    low_close = abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr_14'] = tr.ewm(span=14).mean()
    
    # MACD (12, 26, 9)
    exp12 = df['close'].ewm(span=12).mean()
    exp26 = df['close'].ewm(span=26).mean()
    df['macd'] = exp12 - exp26
    df['macd_signal'] = df['macd'].ewm(span=9).mean()
    df['macd_histogram'] = df['macd'] - df['macd_signal']
    
    # Stochastic Oscillator (14, 3)
    low_14 = df['low'].rolling(14).min()
    high_14 = df['high'].rolling(14).max()
    df['stoch_k'] = 100 * (df['close'] - low_14) / (high_14 - low_14)
    df['stoch_d'] = df['stoch_k'].rolling(3).mean()
    
    # Bollinger Bands (20, 2)
    sma_20 = df['close'].rolling(20).mean()
    std_20 = df['close'].rolling(20).std()
    df['bb_middle'] = sma_20
    df['bb_upper'] = sma_20 + (2 * std_20)
    df['bb_lower'] = sma_20 - (2 * std_20)
    
    # ADX (14-period)
    # Complex calculation - see full implementation
    df['adx'] = calculate_adx(df)
    
    # OBV (On-Balance Volume)
    obv = [0]
    for i in range(1, len(df)):
        if df['close'].iloc[i] > df['close'].iloc[i-1]:
            obv.append(obv[-1] + df['volume'].iloc[i])
        elif df['close'].iloc[i] < df['close'].iloc[i-1]:
            obv.append(obv[-1] - df['volume'].iloc[i])
        else:
            obv.append(obv[-1])
    df['obv'] = obv
    
    return df
```

**Assumptions:**
- Minimum 50 days of data for reliable indicators
- Data is clean (no missing OHLC values)
- Splits/bonuses adjusted before calculation
- Forward-fill for missing dates (holidays)

---

## 4. Technical Logic

### 4.1 Index Filtering

**File:** `backend/app/constants/indices.py`

```python
STOCK_INDICES = {
    "NIFTY50": {
        "name": "NIFTY 50",
        "symbols": ["RELIANCE", "TCS", ...],  # 50 symbols
        "rebalance": "Semi-annual"
    },
    "BANKNIFTY": {
        "symbols": ["HDFCBANK", "ICICIBANK", ...],  # 12 symbols
    },
    "NIFTY200": {
        "count": 200,  # Dynamic from DB
        "used_for": "Trend filters"
    }
}
```

**Logic:**
1. User selects index from dropdown
2. Frontend sends `?index=NIFTY50`
3. Backend filters `WHERE symbol IN (index_symbols)`
4. Only NIFTY200 symbols get trend filters applied

**Why NIFTY200 for trend filters?**
- Sufficient liquidity
- Reduces noise from illiquid stocks
- Prevents data overload
- Meaningful trend signals

### 4.2 Scanner Filters

#### Volume Shocker
```python
# Condition: Today's volume > 3x (20-day avg volume)
current_volume > (avg_volume_20d * 3)
```

#### Price Shocker
```python
# Condition: Absolute price change > 5% in single day
abs(change_pct) > 5.0
```

#### 52-Week High
```python
# Condition: Current price within 2% of 52-week high
close >= (high_52w * 0.98)
```

#### 20-Day Breakout
```python
# Condition: Close >= highest high in last 20 days
close >= max(high[-20:])
```

### 4.3 Technical Score Calculation

**File:** `backend/app/routers/screener.py`

```python
def calculate_technical_score(features: dict) → int:
    """
    Returns: 0-100 score
    Components:
    - Trend (30 points)
    - Momentum (30 points)
    - Volume (20 points)
    - Volatility (20 points)
    """
    score = 50  # Base
    
    # Trend (30 pts)
    if ema20 > ema50:  # Golden cross
        score += 15
    if close > ema50:  # Price above long-term MA
        score += 15
    
    # Momentum (30 pts)
    if 50 <= rsi <= 70:  # Healthy momentum
        score += 20
    elif 40 <= rsi < 50:  # Neutral
        score += 10
    elif rsi > 70:  # Overbought but strong
        score += 10
    
    # Volume (20 pts)
    if vol_percentile > 80:  # High volume day
        score += 20
    elif vol_percentile > 50:
        score += 10
    
    # Volatility (20 pts)
    if atr_pct < 3:  # Low volatility = stable
        score += 20
    elif atr_pct < 5:
        score += 10
    
    return min(100, max(0, score))
```

**Interpretation:**
- 80-100: Strong buy signal
- 60-79: Moderate buy
- 40-59: Neutral
- 20-39: Weak/caution
- 0-19: Avoid

---

## 5. Assumptions

### Data Assumptions
1. **Market Hours:** 9:15 AM - 3:30 PM IST
2. **Bhavcopy Availability:** By 3:45 PM daily
3. **Data Accuracy:** NSE data is final and accurate
4. **Corporate Actions:** Adjusted prices used
5. **Holidays:** Market closed dates handled via NSE calendar

### Technical Assumptions
1. **Indicator Periods:**
   - RSI: 14 days (industry standard)
   - MACD: 12, 26, 9 (default)
   - Bollinger: 20-day, 2 std dev
   - ADX: 14 days

2. **Volume Analysis:**
   - 20-day average = meaningful baseline
   - 3x volume = significant event
   - OBV cumulative from data start

3. **Trend Analysis:**
   - 7-day trend = short-term (5 trading days)
   - 30-day trend = medium-term (21 trading days)
   - EMA preferred over SMA (more reactive)

### Performance Assumptions
1. **Pagination:** 50 stocks/page (optimal UX)
2. **Cache TTL:** 3 seconds (balance fresh/load)
3. **Max Concurrent:** 100 users (current infra)
4. **Query Time:** <500ms target

---

## 6. Module Interactions

### 6.1 With Market Data Module
```
Screener ◄──► Market Data
    │           │
    │           ├─ Fetches OHLCV data
    │           ├─ Gets company metadata
    │           └─ Receives indicator values
    │
    └─ Triggers: Data refresh, indicator recalc
```

### 6.2 With Analyst Module
```
Screener ──→ Analyst
    │
    ├─ Selected stock → Add to portfolio
    ├─ Symbol → Backtest parameters
    └─ Screener results → Universe definition
```

### 6.3 With Quant Module
```
Screener ◄──► Quant
    │           │
    │           ├─ Universe symbols (NIFTY50, etc.)
    │           ├─ Indicator data for strategies
    │           └─ Receives strategy signals
    │
    └─ Provides: Stock universe for backtesting
```

### 6.4 External Dependencies
```
Screener
    ├─ Fyers API: Live quotes
    ├─ NSE Website: Bhavcopy download
    ├─ PostgreSQL: Data storage
    └─ Redis (optional): Caching layer
```

---

## 7. API Reference

### GET /api/screener/
**List stocks with filters**

**Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `index` | string | NIFTY50 | Filter by index (NIFTY50, BANKNIFTY, etc.) |
| `sector` | string | all | Filter by sector |
| `filter_type` | string | ALL | Scanner filter (VOLUME_SHOCKER, etc.) |
| `symbol` | string | - | Search specific symbol |
| `view` | string | technical | technical or financial |
| `page` | int | 1 | Page number |
| `limit` | int | 50 | Results per page |

**Response:**
```json
{
  "data": [
    {
      "symbol": "RELIANCE",
      "close": 2450.50,
      "volume": 12500000,
      "ema20": 2440.20,
      "ema50": 2420.00,
      "rsi_14": 65.4,
      "atr_pct": 2.1,
      "macd": 5.6,
      "stoch_k": 72.3,
      "adx": 25.8,
      "technical_score": 78,
      "trend_7d": 3.2,
      "trend_30d": 8.5
    }
  ],
  "meta": {
    "total": 50,
    "page": 1,
    "pages": 1
  }
}
```

### GET /api/screener/indices
**List available indices**

**Response:**
```json
{
  "indices": [
    {"id": "NIFTY50", "name": "NIFTY 50", "description": "Top 50 liquid stocks"},
    {"id": "BANKNIFTY", "name": "BANK NIFTY", "description": "Banking sector index"}
  ],
  "default": "NIFTY50"
}
```

---

## 8. Database Schema

### Table: historical_prices
**Primary data table**

```sql
CREATE TABLE historical_prices (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    date DATE NOT NULL,
    
    -- OHLCV
    open FLOAT NOT NULL,
    high FLOAT NOT NULL,
    low FLOAT NOT NULL,
    close FLOAT NOT NULL,
    volume BIGINT NOT NULL,
    
    -- Basic Indicators
    ema_20 FLOAT,
    ema_50 FLOAT,
    rsi_14 FLOAT,
    atr_14 FLOAT,
    
    -- Advanced Indicators
    macd FLOAT,
    macd_signal FLOAT,
    macd_histogram FLOAT,
    stoch_k FLOAT,
    stoch_d FLOAT,
    bb_upper FLOAT,
    bb_middle FLOAT,
    bb_lower FLOAT,
    adx FLOAT,
    obv BIGINT,
    
    -- Trend Metrics
    trend_7d FLOAT,
    trend_30d FLOAT,
    high_20d FLOAT,
    is_breakout BOOLEAN,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(company_id, date)
);

CREATE INDEX idx_company_date ON historical_prices(company_id, date);
CREATE INDEX idx_date ON historical_prices(date);
```

### Table: companies
**Master company data**

```sql
CREATE TABLE companies (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(200),
    sector VARCHAR(100),
    industry VARCHAR(100),
    market_cap FLOAT,
    is_fno BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX idx_symbol ON companies(symbol);
CREATE INDEX idx_sector ON companies(sector);
```

---

## 9. Indicator Calculations

### ADX (Average Directional Index)
**Measures trend strength (0-100)**

```python
def calculate_adx(df, period=14):
    """
    Steps:
    1. Calculate +DM and -DM (directional movement)
    2. Calculate TR (true range)
    3. Smooth +DM, -DM, TR over period
    4. Calculate +DI and -DI
    5. Calculate DX = |+DI - -DI| / (+DI + -DI) * 100
    6. ADX = smoothed DX over period
    """
    # +DM
    df['high_diff'] = df['high'].diff()
    df['low_diff'] = -df['low'].diff()
    df['+dm'] = np.where((df['high_diff'] > df['low_diff']) & (df['high_diff'] > 0), df['high_diff'], 0)
    df['-dm'] = np.where((df['low_diff'] > df['high_diff']) & (df['low_diff'] > 0), df['low_diff'], 0)
    
    # TR (True Range)
    df['tr'] = pd.concat([
        df['high'] - df['low'],
        abs(df['high'] - df['close'].shift()),
        abs(df['low'] - df['close'].shift())
    ], axis=1).max(axis=1)
    
    # Smooth
    df['+di'] = 100 * (df['+dm'].ewm(span=period).mean() / df['tr'].ewm(span=period).mean())
    df['-di'] = 100 * (df['-dm'].ewm(span=period).mean() / df['tr'].ewm(span=period).mean())
    
    # DX
    df['dx'] = 100 * abs(df['+di'] - df['-di']) / (df['+di'] + df['-di'])
    
    # ADX
    df['adx'] = df['dx'].ewm(span=period).mean()
    
    return df['adx']
```

**Interpretation:**
- 0-25: Weak trend
- 25-50: Strong trend
- 50-75: Very strong trend
- 75-100: Extremely strong trend

### OBV Divergence Detection
**Identifies price/volume divergence**

```python
def detect_obv_divergence(df):
    """
    Bullish Divergence: Price makes lower low, OBV makes higher low
    Bearish Divergence: Price makes higher high, OBV makes lower high
    """
    price_trend = df['close'].diff(5)  # 5-day price change
    obv_trend = df['obv'].diff(5)  # 5-day OBV change
    
    # Bullish divergence
    bullish = (price_trend < 0) & (obv_trend > 0)
    
    # Bearish divergence
    bearish = (price_trend > 0) & (obv_trend < 0)
    
    return {
        'bullish_div': bullish,
        'bearish_div': bearish
    }
```

---

## 10. Performance Optimizations

### Database Optimizations
1. **Indexes:**
   - `(company_id, date)` for time-series queries
   - `(date)` for daily aggregations
   - `(symbol)` for lookups

2. **Partitioning:**
   ```sql
   -- Partition by year for historical_prices
   CREATE TABLE historical_prices_2024 PARTITION OF historical_prices
   FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
   ```

3. **Materialized Views:**
   ```sql
   -- Pre-compute latest data for fast queries
   CREATE MATERIALIZED VIEW latest_screener_data AS
   SELECT DISTINCT ON (company_id)
       company_id, date, close, volume, rsi_14, macd, adx
   FROM historical_prices
   ORDER BY company_id, date DESC;
   
   REFRESH MATERIALIZED VIEW latest_screener_data;
   ```

### Query Optimizations
1. **Limit Data Fetched:**
   ```python
   # Only fetch needed columns
   query = select(
       HistoricalPrice.symbol,
       HistoricalPrice.close,
       HistoricalPrice.rsi_14
   ).where(...)
   ```

2. **Batch Indicator Calculations:**
   ```python
   # Calculate for all symbols in single pass
   for symbol in symbols:
       calculate_indicators(symbol)
   # vs. one-by-one queries
   ```

3. **Caching Strategy:**
   ```python
   # Cache key: index_sector_filter_page
   cache_key = f"screener_{index}_{sector}_{filter}_{page}"
   if cache.exists(cache_key):
       return cache.get(cache_key)
   ```

### Frontend Optimizations
1. **Virtual Scrolling:** For large tables
2. **Debounced Search:** 300ms delay
3. **Polling Optimization:** Only when tab active
4. **Local Storage:** Cache previous queries

---

## Conclusion

The Screener module is a **pre-calculated, index-optimized** stock screening system that balances real-time updates with performance. All indicators are stored in the database to avoid runtime calculations, and index-based filtering ensures users only see relevant stocks (NIFTY50 default = no overload).

**Key Principles:**
- Database as source of truth
- Pre-compute all indicators
- Index-first filtering
- No mock data
- Fail gracefully with empty states

**Maintenance:**
- Daily bhavcopy import at 3:45 PM
- Indicator recalculation post-import
- Quarterly index rebalancing
- Monthly cleanup of old data (>2 years)
