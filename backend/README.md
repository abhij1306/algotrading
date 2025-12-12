# NSE Trading Screener - Backend

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment (Optional)
Copy `env.example` to `.env` and add Fyers credentials if available:
```bash
FYERS_CLIENT_ID=your_client_id
FYERS_SECRET_KEY=your_secret_key
FYERS_ACCESS_TOKEN=your_access_token
```

### 3. Run Server
```bash
# From AlgoTrading directory
cd backend
uvicorn app.main:app --reload --port 8000
```

### 4. Test API
Open browser: http://localhost:8000

**Endpoints:**
- `GET /` - API info
- `GET /api/health` - Health check
- `GET /api/screener` - Main screener (intraday + swing)
- `GET /api/history/{symbol}` - Historical data

## Project Structure
```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app
│   ├── config.py         # Configuration
│   ├── models.py         # Pydantic models
│   ├── indicators.py     # Technical indicators
│   ├── scoring.py        # Scoring algorithms
│   ├── data_fetcher.py   # Data fetching
│   └── screener.py       # Core screening logic
├── data/
│   └── nse_fno_universe.json  # F&O stock list
├── requirements.txt
└── env.example
```

## Features
- ✅ NSE F&O universe (~200 stocks)
- ✅ Technical indicators (EMA, ATR, RSI, z-scores)
- ✅ Intraday & swing scoring algorithms
- ✅ Futures preference logic
- ✅ 50-ticker cap with dynamic merging
- ✅ Caching (1-hour TTL)
- ⏳ Fyers API integration (Phase 3)

## Testing
```bash
# Test screener endpoint
curl http://localhost:8000/api/screener

# Test specific stock history
curl http://localhost:8000/api/history/RELIANCE
```
