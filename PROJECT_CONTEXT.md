# AlgoTrading - Project Context & Critical Information

**Last Updated:** December 24, 2025  
**Status:** ✅ Stable Production System

## 🎯 Project Overview

SmartTrader is a full-stack algorithmic trading application for Indian stock markets (NSE). It combines real-time market monitoring, technical analysis, portfolio backtesting, and autonomous signal generation.

**Tech Stack:**
- **Backend:** Python (FastAPI, SQLAlchemy, PostgreSQL)
- **Frontend:** Next.js 16, React, TypeScript, TailwindCSS
- **Database:** PostgreSQL (Primary) - **CRITICAL: NEVER DELETE**
- **Data Sources:** Fyers API (real-time), yFinance (historical)

---

## 🚨 CRITICAL: DATABASE

**Database Type:** PostgreSQL  
**Connection:** `localhost:5432/algotrading`  
**Current Data:**
- **4,166,496** historical price records (2016-2025, ~10 years)
- **3,278** active companies
- **1,199,945** intraday 5-min candles (recent 3 months)
- **13** indices with historical data

**⚠️ NEVER DELETE DATABASE**
- Database is configured in `backend/app/database.py`
- Defaults to PostgreSQL (avoid SQLite - causes data loss on restart)
- To check config: `USE_SQLITE_TEST` must be `"False"` (default)

---

## 📁 Project Structure

```
AlgoTrading/
├── backend/                  # Python FastAPI application
│   ├── app/
│   │   ├── database.py      # DB models, PostgreSQL connection
│   │   ├── main.py          # FastAPI app, router registration
│   │   ├── routers/         # API endpoints
│   │   ├── services/        # Business logic, data fetchers
│   │   └── utils/           # Helpers, WebSocket manager
│   └── scripts/
│       ├── daily_update_master.py  # EOD data update script
│       └── precompute_indicators.py # Technical calculations
├── frontend/                # Next.js 16 application  
│   ├── app/                 # App router pages
│   │   ├── dashboard/       # Market overview (default landing)
│   │   ├── screener/        # Stock screening
│   │   ├── quant/           # Portfolio backtesting
│   │   └── terminal/        # Live trading terminal
│   ├── components/          # React components
│   └── hooks/               # WebSocket, custom hooks
├── nse_data/                # Raw CSV data (indices, equities)
│   └── raw/
│       ├── indices/         # Index historical CSVs
│       └── intraday/        # 5-min candle data
└── docs/                    # Technical documentation

```

---

## 🔧 Development Commands

### Starting the Application

**Backend:**
```bash
cd backend
python run_entry.py  # Uvicorn on port 8000
```

**Frontend:**
```bash
cd frontend
npm run dev  # Next.js on port 3000
```

**Full Stack (Development):**
```bash
python start_dev.py  # Starts both backend + frontend
```

### Daily Update Process⚠️ **CRITICAL: Run Daily After Market Close**

**Script:** `run_daily_update.bat` (root directory) **Time:** 4:00 PM IST (after market close at 3:30 PM)**Duration:** ~5-10 minutes for 3,278 stocks

### What It Does:
1. **EOD Price Update** - Fetches latest prices from Fyers for all active stocks
2. **Index Data Update** - Updates NIFTY 50, BANK NIFTY, and 10 other indices (yfinance)
3. **✅ Technical Indicators** - **MOST CRITICAL STEP**
   - Recalculates ATR%, RSI, MACD, ADX, Stochastic, Bollinger Bands
   - Updates 3000+ stocks with fresh indicators
   - **Without this, screener shows 0% ATR and default RSI (50)**

### How to Run:
```cmd
# Option 1: Double-click
run_daily_update.bat

# Option 2: Command line
cd C:\AlgoTrading\backend
python scripts/daily_update_master.py
```

### What Gets Calculated:
- **Moving Averages:** EMA 20, EMA 50
- **Volatility:** ATR (14), ATR% = (ATR / Close) × 100
- **Momentum:** RSI (14), MACD, Stochastic K/D
- **Trend:** ADX, Bollinger Bands (Upper/Middle/Lower)
- **Volume:** Volume Percentile

### Troubleshooting:
**Problem:** Screener shows ATR% = 0.0%, RSI = 50 for all stocks
**Solution:** Run `python scripts/precompute_indicators.py` manually
**Cause:** Daily update not run or failed silently

**Log File:** `backend/logs/daily_update_YYYYMMDD.log` Run daily at 4:00 PM IST (after market close)

---

## 🌐 Application Features

### 1. **Dashboard** (`/dashboard`) - Default Landing Page
- US Fear & Greed Index
- India Sentiment (derived from India VIX)
- Global Markets (S&P 500, NASDAQ, Dow, VIX)
- Indian Markets (NIFTY 50, BANK NIFTY, India VIX)

### 2. **Screener** (`/screener`)
- Real-time stock screening with technical indicators
- Filters: Index, Sector, Volume Shockers, Breakouts
- **Endpoint:** `/api/screener?index=NIFTY50&limit=50`

### 3. **Quant Research** (`/quant`)
- Portfolio backtesting engine
- Strategy combinations and allocation
- Performance metrics (Sharpe, max drawdown, CAGR)

### 4. **Live Monitor** (`/quant/monitoring`)
- System health monitoring
- Real-time WebSocket connections

### 5. **Terminal** (`/terminal`)
- Live trading interface
- Order execution (paper/live)

---

## 🔌 API Endpoints

### Market Data
- `GET /api/market/overview` - Dashboard data (sentiment, indices)
- `GET /api/market/search?query=RELIANCE` - Symbol search

### Screener
- `GET /api/screener?index=NIFTY50&limit=50` - Stock screener
- `GET /api/screener/indices` - Available index filters

### WebSocket
- `WS /api/websocket/stream` - Real-time tick data
- Auto-reconnects every 5 seconds if disconnected

---

## 🛠️ Recent Fixes & Configuration

### Database Configuration (CRITICAL)
**File:** `backend/app/database.py`  
**Line 46:** `if os.getenv("USE_SQLITE_TEST", "False") == "True":`

The default is `"False"` which means **PostgreSQL is used**. This prevents data loss.

### India Sentiment Formula
**File:** `backend/app/services/market_data_service.py` (lines 162-195)

Uses India VIX to calculate sentiment score:
- VIX < 12: Greed (65-80 score)
- VIX 12-18: Neutral (40-65 score)
- VIX 18-25: Fear (20-40 score)
- VIX > 25: Extreme Fear (0-20 score)

### Sidebar Navigation
**File:** `frontend/components/layout/UnifiedSidebar.tsx`  
**Line 42:** Dashboard routes to `/dashboard` (market overview)

### Default Landing Page
**File:** `frontend/app/page.tsx`  
**Line 10:** Redirects to `/dashboard`

### WebSocket Reconnection
**File:** `frontend/hooks/useWebSocket.ts`  
Auto-reconnects with 5-second retry on connection failure

---

## 📊 Data Sources

### Indices (yFinance Symbols)
- `^NSEI` - NIFTY 50
- `^NSEBANK` - BANK NIFTY
- `^NSEFI` - FIN NIFTY
- `^CNX100` - NIFTY 100
- `^CNX200` - NIFTY 200
- `^CNXIT` - NIFTY IT
- `^CNXAUTO` - NIFTY AUTO
- `^CNXPHARMA` - NIFTY PHARMA
- `^CNXFMCG` - NIFTY FMCG
- `^CNXMETAL` - NIFTY METAL
- `^CNXENERGY` - NIFTY ENERGY
- `^CNXREALTY` - NIFTY REALTY

### Fyers Integration (Optional)
**Status:** Token not required for basic functionality  
**Files:**
- `backend/app/services/fyers_client.py`
- `backend/app/services/fyers_websocket.py`

Token file location: `fyers/config/access_token.json` (optional)  
Falls back to environment variables if not found.

---

## 🚀 Deployment Notes

### Environment Variables (.env)
```
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=algotrading
DB_USER=postgres
DB_PASSWORD=admin

# DO NOT SET THIS - defaults to PostgreSQL
# USE_SQLITE_TEST=False  

# Fyers (Optional)
FYERS_CLIENT_ID=
FYERS_SECRET_KEY=
FYERS_ACCESS_TOKEN=
```

### Port Configuration
- **Backend:** 8000
- **Frontend:** 3000
- **PostgreSQL:** 5432

---

## 🐛 Common Issues & Solutions

### 1. Screener Shows "No Stocks Found"
**Cause:** Database empty or daily update not run  
**Solution:** Run `python backend/scripts/daily_update_master.py`

### 2. PostgreSQL Connection Error
**Cause:** Database not running or wrong credentials  
**Solution:**
```bash
# Check if PostgreSQL is running
psql -U postgres -d algotrading
```

### 3. WebSocket Connection Fails
**Cause:** Backend not running or CORS issue  
**Solution:** WebSocket hook auto-reconnects. Check backend is running on port 8000.

### 4. India Sentiment Shows 100 (Maxed Out)
**Cause:** Old formula was inverted  
**Solution:** Fixed in `market_data_service.py` (lines 162-195)

---

## 📝 Important Files (Never Delete)

### Configuration
- `backend/app/database.py` - Database models & connection
- `backend/app/main.py` - FastAPI app initialization
- `frontend/app/page.tsx` - Default route redirect

### Data Scripts
- `backend/scripts/daily_update_master.py` - Daily data updater
- `backend/scripts/precompute_indicators.py` - Technical calculations
- `import_indices.py` - One-time index data import

### Documentation
- `README.md` - Project overview
- `PROJECT_CONTEXT.md` - This file (agent reference)
- `backend/DATABASE_SCHEMA.md` - DB structure

---

## 🎨 UI/UX Guidelines

**Design Philosophy:**
- Dark theme (#050505 background)
- Cyan accents for interactive elements
- Glass morphism effects
- Responsive and premium feel

**Key Files:**
- `docs/BRAND_GUIDELINES.md` - Design system
- `frontend/app/globals.css` - Global styles

---

## 📈 Performance Metrics

### Database Performance
- **Query Response:** < 100ms for screener (with 50 stocks)
- **Index Queries:** Optimized with composite indexes
- **Bulk Inserts:** ~1000 records/second

### Frontend Performance
- **Initial Load:** < 2s
- **Dashboard Render:** < 500ms
- **WebSocket Latency:** < 50ms

---

## 🔒 Security Notes

- All API keys stored in `.env` (excluded from git)
- PostgreSQL requires password authentication
- CORS configured for localhost in development
- Production deployment requires HTTPS

---

## 📞 Quick Reference Commands

```bash
# Check database connection
python -c "from backend.app.database import SessionLocal, Company; db = SessionLocal(); print(f'Companies: {db.query(Company).count()}'); db.close()"

# Run daily update
python backend/scripts/daily_update_master.py

# Start backend (foreground)
cd backend && python run_entry.py

# Start frontend
cd frontend && npm run dev

# Import indices (one-time)
python import_indices.py

# Check data coverage
python check_data.py
```

---

## 🎯 Future Enhancements

1. Real-time WebSocket data from Fyers
2. Live trading execution
3. Option chain analysis
4. ML-based signal generation
5. Automated daily deployment

---

**For Developers:** This file serves as the single source of truth for project context. Update it when making architectural changes or adding new features.
