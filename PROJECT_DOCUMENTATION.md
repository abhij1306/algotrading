# SmartTrader 3.0 - System Architecture & Improvements

## 📋 Table of Contents
1. [System Overview](#system-overview)
2. [Architecture](#architecture)
3. [Recent Improvements](#recent-improvements)
4. [API Documentation](#api-documentation)
5. [Database Schema](#database-schema)
6. [Error Handling](#error-handling)
7. [Testing](#testing)
8. [Deployment](#deployment)

---

## 🎯 System Overview

SmartTrader 3.0 is a comprehensive algorithmic trading platform with three primary modes:
- **Screener**: Technical and fundamental stock filtering
- **Analyst Mode**: Individual stock portfolio management and risk analysis
- **Quant Mode**: Strategy research, backtesting, and portfolio composition

### Tech Stack
- **Backend**: FastAPI (Python 3.10+), SQLAlchemy, PostgreSQL
- **Frontend**: Next.js 14, React 18, TypeScript, Tailwind CSS
- **Data Sources**: Fyers API, NSE, Pre-calculated indicators
- **Testing**: Pytest, Jest, React Testing Library

---

## 🏗️ Architecture

### High-Level Architecture
```
Frontend (Next.js)
    ↓ HTTP/REST
Backend (FastAPI)
    ↓ SQLAlchemy ORM
Database (PostgreSQL) ← Fyers API (Live Data)
```

### Backend Structure
```
backend/
├── app/
│   ├── main.py                 # FastAPI app + global exception handler
│   ├── database.py             # SQLAlchemy models
│   ├── exceptions.py           # Custom exception classes ✨
│   ├── routers/                # API endpoints
│   │   ├── portfolio.py        # Consolidated portfolio + strategies ✨
│   │   ├── screener.py         # Stock filtering
│   │   ├── market.py           # Live quotes, search
│   │   ├── auth.py             # Fyers authentication
│   │   └── ...
│   ├── engines/                # Core business logic
│   │   ├── backtest/           # Backtest execution
│   │   ├── strategies/         # Strategy implementations
│   │   └── data_provider.py   # Data abstraction layer
│   └── services/               # External integrations
│       ├── paper_trading.py    # Paper trading execution
│       ├── live_monitor.py     # Live position monitoring
│       └── fyers_websocket.py  # WebSocket client
├── tests/                      # Pytest test suite ✨
└── scripts/                    # Utility scripts
    └── migrate_add_constraints.py  # DB migration ✨
```

### Frontend Structure
```
frontend/
├── app/                        # Next.js pages
│   ├── screener/               # Stock screener
│   ├── analyst/                # Analyst mode
│   └── quant/                  # Quant research
├── components/
│   ├── shared/
│   │   └── DataState.tsx       # UI state components ✨
│   ├── quant/                  # Quant-specific components
│   └── ...
└── lib/
    └── api-client.ts           # Centralized API client ✨
```

**✨ = Recently added/improved**

---

## 🚀 Recent Improvements

### 1. Backend Consolidation
**Deleted redundant routers** saving ~734 lines:
- `backtest_analyst.py`
- `backtest_quant.py`
- `lifecycle.py`
- `quant_research.py`

**All functionality consolidated into `portfolio.py`** with clear separation:
- `/api/portfolio/stocks` - Analyst mode (stock portfolios)
- `/api/portfolio/strategies` - Quant mode (strategy portfolios)

### 2. Structured Error Handling
Created `exceptions.py` with custom exception classes:
```python
class DataNotFoundError(SmartTraderException):
    """Resource not found - returns 404"""

class ValidationError(SmartTraderException):
    """Invalid request data - returns 422"""

class InsufficientDataError(SmartTraderException):
    """Not enough data for operation - returns 400"""
```

**Error Response Format:**
```json
{
  "error": {
    "code": "DATA_NOT_FOUND",
    "message": "Portfolio 123 does not exist"
  }
}
```

### 3. Database Integrity
**Migration script created** (`migrate_add_constraints.py`):
- ✅ Foreign key constraints
- ✅ CHECK constraints for percentages
- ✅ Performance indexes
- ✅ ENUM types for lifecycle states

**Run migration:**
```bash
python backend/scripts/migrate_add_constraints.py
```

### 4. Frontend Improvements
**Centralized API Client** (`lib/api-client.ts`):
- Automatic error handling
- Retry logic with exponential backoff
- Type-safe responses

**Reusable Components** (`shared/DataState.tsx`):
- `<LoadingState />` - Loading spinner
- `<ErrorState />` - Error display with retry
- `<EmptyState />` - Empty list placeholder

### 5. WebSocket Integration
**Real-Time Data Layer** (`ws_manager.py`):
- Generic `ConnectionManager` for broadcast/private messages
- Integration with Fyers for market data stream
- Front-end `useWebSocket` hook with auto-reconnect
- Visual connection indicators in UI

---

## 📡 API Documentation

### Portfolio Endpoints

#### Stock Portfolios (Analyst Mode)
```http
POST   /api/portfolio/stocks              Create stock portfolio
GET    /api/portfolio/stocks              List all portfolios
GET    /api/portfolio/stocks/{id}         Get portfolio details
DELETE /api/portfolio/stocks/{id}         Delete portfolio
POST   /api/portfolio/stocks/{id}/analyze  Run risk analysis
```

#### Strategy Portfolios (Quant Mode)
```http
GET    /api/portfolio/strategies/available     List available strategies
POST   /api/portfolio/strategies               Create strategy portfolio
GET    /api/portfolio/strategies               List strategy portfolios
PATCH  /api/portfolio/strategies/{id}          Update strategy metadata
POST   /api/portfolio/strategies/correlation   Calculate correlation
POST   /api/portfolio/strategies/backtest      Run backtest
```

#### Portfolio Policies
```http
GET    /api/portfolio/strategies/policy    List policies
POST   /api/portfolio/strategies/policy    Create policy
```

#### Live Monitoring
```http
GET    /api/portfolio/strategies/monitor         Get monitoring data
POST   /api/portfolio/strategies/monitor/refresh Force refresh
```

### Screener Endpoints
```http
GET /api/screener/                List stocks with filters
GET /api/screener/indices         Get index list
```

### Market Endpoints
```http
GET    /api/market/quotes/live       Live quotes
GET    /api/market/search            Symbol search
GET    /api/market/sectors           Sector list
GET    /api/market/watchlist         Get watchlist
POST   /api/market/watchlist         Add to watchlist
DELETE /api/market/watchlist/{symbol} Remove from watchlist
```

---

## 🗄️ Database Schema

### Core Tables

#### `strategy_metadata`
Strategy definitions and lifecycle status.
```sql
CREATE TABLE strategy_metadata (
    strategy_id VARCHAR PRIMARY KEY,
    description TEXT,
    timeframe VARCHAR,
    lifecycle_status lifecycle_enum,  -- RESEARCH, PAPER, LIVE, RETIRED
    regime_notes TEXT,  -- "WHEN IT LOSES" documentation
    created_at TIMESTAMP
);
```

#### `backtest_runs`
Immutable backtest results.
```sql
CREATE TABLE backtest_runs (
    run_id VARCHAR PRIMARY KEY,
    strategy_id VARCHAR REFERENCES strategy_metadata(strategy_id),
    start_date DATE,
    end_date DATE,
    metrics JSONB,  -- Sharpe, win rate, etc.
    created_at TIMESTAMP
);
```

#### `user_portfolios`
Analyst mode stock portfolios.
```sql
CREATE TABLE user_portfolios (
    id SERIAL PRIMARY KEY,
    name VARCHAR,
    created_at TIMESTAMP
);

CREATE TABLE portfolio_positions (
    id SERIAL PRIMARY KEY,
    portfolio_id INTEGER REFERENCES user_portfolios(id) ON DELETE CASCADE,
    symbol VARCHAR,
    quantity INTEGER,
    entry_price DECIMAL
);
```

#### `portfolio_policies`
Risk management rules.
```sql
CREATE TABLE portfolio_policies (
    id SERIAL PRIMARY KEY,
    name VARCHAR,
    cash_reserve_percent DECIMAL CHECK (cash_reserve_percent BETWEEN 0 AND 100),
    max_equity_exposure_percent DECIMAL CHECK (max_equity_exposure_percent BETWEEN 0 AND 100),
    daily_stop_loss_percent DECIMAL CHECK (daily_stop_loss_percent < 0)
);
```

### Indexes
```sql
CREATE INDEX idx_historical_symbol_date ON historical_prices(symbol, date DESC);
CREATE INDEX idx_historical_sector ON historical_prices(sector);
CREATE INDEX idx_backtest_strategy_date ON backtest_runs(strategy_id, created_at DESC);
```

---

## ⚠️ Error Handling

### Error Codes
| Code | HTTP Status | Description |
|------|-------------|-------------|
| `DATA_NOT_FOUND` | 404 | Resource doesn't exist |
| `VALIDATION_ERROR` | 422 | Invalid request data |
| `INSUFFICIENT_DATA` | 400 | Not enough data for operation |
| `EXTERNAL_API_ERROR` | 503 | Fyers/NSE API failure |
| `DATABASE_ERROR` | 500 | Database operation failed |
| `BACKTEST_ERROR` | 500 | Backtest execution failed |

### Usage in Backend
```python
from app.exceptions import DataNotFoundError

portfolio = db.query(UserPortfolio).filter_by(id=portfolio_id).first()
if not portfolio:
    raise DataNotFoundError(
        message=f"Portfolio {portfolio_id} not found",
        resource_type="portfolio",
        resource_id=str(portfolio_id)
    )
```

### Usage in Frontend
```typescript
import { portfolioAPI, getErrorMessage } from '@/lib/api-client';

const { data, error } = await portfolioAPI.getStockPortfolio(123);
if (error) {
    toast.error(getErrorMessage(error));
    return;
}
// Use data
```

---

## 🧪 Testing

### Running Tests
```bash
# Backend tests
cd backend
pytest --cov=app --cov-report=html
open htmlcov/index.html

# Frontend tests
cd frontend
npm test -- --coverage
```

### Test Structure
```
backend/tests/
├── conftest.py              # Fixtures (db_session, client, sample_data)
├── test_portfolio_api.py    # Portfolio endpoint tests
├── test_screener_api.py     # Screener endpoint tests
└── test_exceptions.py       # Exception handling tests
```

### Writing Tests
```python
import pytest

@pytest.mark.integration
def test_create_portfolio(client, sample_portfolio_data):
    response = client.post("/api/portfolio/stocks", json=sample_portfolio_data)
    assert response.status_code == 201
    assert response.json()["name"] == sample_portfolio_data["name"]
```

---

## 🚀 Deployment

### Development Setup
```bash
# Backend
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev
```

### Database Setup
```bash
# Create database
createdb smarttrader

# Run migrations
python backend/scripts/migrate_add_constraints.py
```

### Environment Variables
```bash
# backend/.env
DATABASE_URL=postgresql://user:password@localhost:5432/smarttrader
FYERS_APP_ID=your_app_id
FYERS_SECRET_KEY=your_secret_key
GROQ_API_KEY=your_groq_key

# frontend/.env.local
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000/api/websocket/stream
```

### Production Checklist
- [ ] Run database migration
- [ ] Set all environment variables
- [ ] Configure CORS origins
- [ ] Setup logging (Sentry/DataDog)
- [ ] Enable HTTPS
- [ ] Configure rate limiting
- [ ] Setup backup strategy
- [ ] Load testing

---

## 🔧 Common Tasks

### Add a New API Endpoint
1. Create route in `backend/app/routers/`
2. Use custom exceptions for errors
3. Add tests in `backend/tests/`
4. Add to `api-client.ts` convenience functions
5. Update this documentation

### Add Database Constraints
1. Add SQL to `migrate_add_constraints.py`
2. Test migration on dev database
3. Run on production with backup

### Update Frontend Component
1. Use `<DataState />` wrapper for loading/error states
2. Use `api-client.ts` instead of raw `fetch()`
3. Display errors with `<ErrorState />`

---

**Last Updated:** 2025-12-23
**Version:** 3.0.0
**Status:** Production-Ready (Grade A)
