# Screener Module Documentation

## Overview
The Screener is a real-time stock analysis tool that combines historical database data, live market quotes, and technical indicators to help identify trading opportunities across Indian equity markets.

## Architecture

### Frontend Components
**Location**: `frontend/app/screener/page.tsx` & `frontend/components/ScreenerTable.tsx`

**Features**:
- Multi-index filtering (NIFTY50, BANKNIFTY, NIFTY200, etc.)
- Symbol search with autocomplete
- Sector filtering
- Advanced scanner filters (Price/Volume Shockers, 52W High/Low)
- Technical/Financial view toggle
- Real-time data updates via WebSocket
- Pagination (50 records/page)

### Backend API
**Location**: `backend/app/routers/screener.py`

**Endpoints**:
- `GET /api/screener/`: Main screener endpoint with filters
- `GET /api/screener/indices`: Available index list
- `GET /api/screener/financials`: Financial-only view

## Data Flow

### Database Layer
```
HistoricalPrice (Latest date per symbol)
    ↓
Technical Indicators (Precomputed columns):
    - EMA20, EMA50
    - RSI, MACD, ADX
    - Stochastic (K/D)
    - Bollinger Bands
    - ATR, Volume Percentile
    - Trend indicators (7d, 30d)
    ↓
Optional: FinancialStatement (Latest period)
    - Revenue, EPS, ROE
    - P/E Ratio, Debt/Equity
```

### Live Data Enrichment
**Function**: `fetch_fyers_quotes()` in `backend/app/data_fetcher.py`

**Process**:
1. Fetch latest DB data for visible page
2. Call Fyers API for live LTP, Volume
3. Override DB values with live data
4. Recalculate change% using live prices

**Fallback**: If Fyers unavailable, serve DB data only

### WebSocket Real-Time Updates
**Integration**: `LiveMarketService` broadcasts tick data to clients

**Flow**:
```
Client subscribes to visible symbols
    ↓
Fyers WebSocket sends ticks
    ↓
LiveMarketService throttles (1s/symbol)
    ↓
Frontend receives {"type": "ticker", "data": {...}}
    ↓
UI updates LTP, Volume, Change%
```

## Filter Types

### 1. Index Filters
**Supported Indices**:
- NIFTY50: Top 50 large-cap stocks
- NIFTY100, NIFTY200, NIFTY500
- BANKNIFTY: Banking sector index
- NIFTYMIDCAP, NIFTYSMALLCAP
- ALL: Entire universe (~2000+ stocks)

**Implementation**: Symbol-based filtering using `STOCK_INDICES` constant

### 2. Scanner Filters

#### Price Shockers (`PRICE_SHOCKER`)
**Logic**: High volatility/ATR stocks  
**SQL**: `ORDER BY HistoricalPrice.atr_14 DESC`  
**Use Case**: Identify high-momentum breakout candidates

#### Volume Shockers (`VOLUME_SHOCKER`)
**Logic**: Abnormally high volume  
**SQL**: `ORDER BY HistoricalPrice.volume DESC LIMIT 50`  
**Use Case**: Detect unusual institutional activity

#### 52 Week High (`52W_HIGH`)
**Logic**: Stocks at/near 52-week highs  
**SQL**: `WHERE is_breakout = True ORDER BY close DESC`  
**Use Case**: Trend-following strategies

#### 52 Week Low (`52W_LOW`)
**Logic**: Oversold stocks (low RSI proxy)  
**SQL**: `ORDER BY HistoricalPrice.rsi_14 ASC`  
**Use Case**: Value/reversal plays

### 3. View Modes

**Technical View** (Default):
- Displays: RSI, MACD, ADX, EMA20/50, Stochastic, Bollinger Bands
- Use Case: Day trading, swing trading setups

**Financial View**:
- Displays: Market Cap, P/E, ROE, EPS, Revenue, Debt/Equity
- Use Case: Fundamental analysis, long-term investing

## Daily Data Update System

### Master Script
**Location**: `backend/scripts/daily_update_master.py`

**Schedule**: 4:00 PM IST daily (after market close at 3:30 PM)

**Execution Steps**:
1. **Update EOD Prices** (All active stocks)
   - Fetches last 5 days from Fyers
   - Handles missed days
   - ~2000+ symbols
   
2. **Update Index Data**
   - NIFTY 50, BANKNIFTY, IT, AUTO, PHARMA, FMCG, METAL, INFRA
   - Essential for benchmark comparisons

3. **Precompute Technical Indicators**
   - Runs `precompute_indicators.py`
   - Calculates EMA, RSI, MACD, etc.
   - Stores in `HistoricalPrice` columns

4. **Update Financial Data** (Optional)
   - Quarterly company financials
   - Rate-limited to prevent API throttling
   - Typically run monthly

### Automation
**Recommended Setup**:
```bash
# Windows Task Scheduler
schtasks /create /tn "AlgoTrading Daily Update" /tr "python C:\AlgoTrading\backend\scripts\daily_update_master.py" /sc daily /st 16:00
```

**Logs**: Saved to `backend/logs/daily_update_YYYYMMDD.log`

## Robustness Features

### Market Hours Handling

**During Market Hours (9:15-15:30 IST)**:
- Live data from Fyers WebSocket
- Database as fallback if connection fails
- Change% calculated from prev_close

**Off Market Hours**:
- Pure database queries
- No WebSocket connections attempted
- `DEV_MODE=True` overrides for testing

### Error Handling

**Frontend**:
- Retry button on API failures
- Cached data served during network issues
- Graceful degradation (skeleton loaders)

**Backend**:
- Fallback to ALL filter if specific filter returns 0 results
- NaN/Inf sanitization before JSON serialization
- Transaction rollback on DB errors

### Performance Optimizations

**SQL Efficiency**:
- Latest price fetched via JOIN (not N+1 queries)
- Pagination limits data transfer
- Indices on: `company_id`, `date`, `symbol`

**Caching**:
- Frontend localStorage cache (5min TTL)
- Backend live quotes cached per request

**WebSocket Throttling**:
- Max 1 update/second/symbol
- Prevents UI saturation during volatility

## Testing & Validation

### API Health Checks
```bash
# Test screener endpoint
curl "http://localhost:8000/api/screener/?page=1&limit=5"

# Test filters
curl "http://localhost:8000/api/screener/?filter_type=PRICE_SHOCKER&limit=10"
curl "http://localhost:8000/api/screener/?filter_type=VOLUME_SHOCKER&limit=10"

# Test index filtering
curl "http://localhost:8000/api/screener/?index=BANKNIFTY&limit=20"

# Test financial view
curl "http://localhost:8000/api/screener/?view=financial&limit=10"
```

### Data Integrity Checks
```python
# Verify indicators exist
from app.database import SessionLocal, HistoricalPrice

db = SessionLocal()
sample = db.query(HistoricalPrice).first()
assert sample.ema_20 is not None
assert sample.rsi_14 is not None
```

### WebSocket Integration Test
```python
# Run test_ws_v2.py
python test_ws_v2.py

# Expected output:
# 1. Connected
# 1.5 Sent ping
# 1.5 Received: {"type":"pong"}
# 2. Sent subscribe
# [ACK RECEIVED]
# 3. Listening...
```

## Troubleshooting

### No Data Showing
**Symptoms**: Empty screener table  
**Solutions**:
1. Check daily update ran: `tail backend/logs/daily_update_*.log`
2. Verify database populated: `SELECT COUNT(*) FROM historical_prices;`
3. Check API health: `curl http://localhost:8000/api/system/health`

### Filters Not Working
**Symptoms**: PRICE_SHOCKER returns same as ALL  
**Solutions**:
1. Verify `filter_type` parameter correct (case-sensitive)
2. Check backend logs for query errors
3. Ensure indicators calculated: `SELECT rsi_14, atr_14 FROM historical_prices LIMIT 5;`

### Live Data Not Updating
**Symptoms**: WebSocket connected but prices static  
**Solutions**:
1. Check market hours (9:15-15:30 IST)
2. Verify Fyers token valid: `/api/websocket/status`
3. Check subscription sent from frontend
4. Inspect `LiveMarketService` logs for errors

### Slow Performance
**Symptoms**: Page load \u003e 3 seconds  
**Solutions**:
1. Reduce `limit` parameter (default: 50)
2. Use index filtering (NIFTY50 vs ALL)
3. Check database indices: `SHOW INDEX FROM historical_prices;`
4. Analyze slow query: Enable SQL logging

## Future Enhancements

### Planned Features
- [ ] Custom screener builder (user-defined filters)
- [ ] Alerts on filter criteria matches
- [ ] Export to CSV/Excel
- [ ] Backtesting scanner results
- [ ] Multi-timeframe analysis (1D, 1W, 1M)

### Performance Roadmap
- [ ] Redis caching layer
- [ ] Materialized views for complex queries
- [ ] WebSocket subscription pools
- [ ] Database read replicas

## Related Documentation
- [WebSocket Integration](./websocket_integration.md)
- [Database Schema](./database_schema.md)
- [Technical Indicators](./technical_indicators.md)
- [API Reference](./api_reference.md)
