# AlgoTrading Project Context

This file tracks successful workflows, configurations, and solutions for future reference.

## ✅ Successfully Completed Tasks

### 1. PostgreSQL Migration (2024-12-13)
**What worked:**
- Migrated from SQLite to PostgreSQL
- Used SQLAlchemy ORM (no model changes needed)
- Migration script: `database/migrate_sqlite_to_postgres.py`
- Database config: `backend/app/database.py` uses environment variables from `database/.env`

**Key learnings:**
- SQLAlchemy makes DB migration seamless
- Always backup SQLite before migration
- Use connection pooling for PostgreSQL

### 2. Fyers API Integration
**What worked:**
- Login script: `fyers/fyers_login.py`
- Config file: `fyers/config/access_token.json`
- Access token saved successfully
- Config detection: `backend/app/config.py` checks for token file

**Status:** ✅ Connected (Token verified: eyJhbGciOiJIUzI1NiIs...)

**How to use Fyers:**
```python
from backend.app.data_fetcher import fetch_fyers_historical
hist = fetch_fyers_historical("RELIANCE", days=365)
```

### 3. Database Schema
**Tables:**
- `companies` - Company master (170 companies currently)
- `historical_prices` - OHLCV data
- `financial_statements` - Financial data
- `quarterly_results` - Quarterly results
- `data_update_logs` - Update tracking

**Working pattern for data population:**
- Use `backend/app/data_repository.py` methods
- Commit after each company to avoid transaction issues
- Handle duplicates with try/except

### 4. NSE Full Data Fetch (2024-12-13)
**What worked:**
- Source: Fyers public symbol master (`NSE_CM.csv`)
- Script: `backend/scripts/fetch_nse_symbols.py` (extracts ~2300 EQ symbols)
- Population: `backend/scripts/populate_historicals.py`
  - Handles Fyers API limits and yfinance fallback
  - Checks for existing recent data to optimize updates
  - Transaction-safe (commits per company)

**How to run:**
```bash
python backend/scripts/fetch_nse_symbols.py
python backend/scripts/populate_historicals.py
```

### Data Fetching (Yesterday's Success)
**File:** `init_database.py`
**What worked:**
- Fetched data for 200 stocks successfully
- Used Fyers API with yfinance fallback
- Processed stocks one at a time with individual commits

**Key code pattern:**
```python
for symbol in universe:
    try:
        hist = fetch_fyers_historical(symbol, days=365)
        if hist is None or hist.empty:
            hist = fetch_yfinance_data(symbol, period='1y')
        
        if hist is not None and not hist.empty:
            records = repo.save_historical_prices(symbol, hist, source='fyers')
            # Commit happens inside save_historical_prices
    except Exception as e:
        # Continue to next stock
        pass
```

## ⚠️ Common Issues & Solutions

### Issue: Database Transaction Errors
**Error:** `IntegrityError: duplicate key value violates unique constraint`
**Solution:** 
- Don't try to add companies that already exist
- Use `get_or_create_company()` instead of direct `Company()` creation
- Commit after each operation, not in bulk

### Issue: Fyers Data Fetch Fails
**Possible causes:**
1. Token expired (re-run `fyers/fyers_login.py`)
2. Symbol format wrong (use "RELIANCE" not "RELIANCE.NS")
3. Market hours restriction (some data only during market hours)

**Solution:** Always have yfinance fallback

### Issue: yfinance Slow
**Solution:** Use Fyers API when available (10x faster)

## 📝 Environment Variables

### Database (.env in database/)
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=algotrading
DB_USER=postgres
DB_PASSWORD=your_password
```

### Fyers (fyers/config/keys.env)
```
CLIENT_ID=your_client_id
SECRET_KEY=your_secret_key
REDIRECT_URI=https://trade.fyers.in/api-login/redirect-index.html
```

## 🚀 Quick Commands

### Start Backend
```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Start Frontend
```bash
cd frontend
npm run dev
```

### Connect Fyers
```bash
python fyers/fyers_login.py
```

### Populate Database (Proven Method)
```bash
python init_database.py
# Select option 4 for all stocks
```

## 📊 Current Status

- **Backend:** Running on port 8000 ✅
- **Frontend:** Running on port 3000 ✅
- **Database:** PostgreSQL (algotrading) ✅
- **Fyers:** Connected ✅
- **Companies:** 2,386 in database
- **Financial Data:** 10 companies with screener.in data

## 🔍 Screener.in Integration (2024-12-13)

### What Worked
**Scraper Module:** `backend/app/screener_scraper.py`
- Scrapes company overview, Profit & Loss, Balance Sheet, Cash Flows, Ratios
- Extracts: Revenue, Net Income, EPS, ROE, Debt/Equity, P/E from tables
- Rate limiting: 2s delay between requests
- Source tracking: `source='screener'` in database

**Key Functions:**
```python
scrape_screener(symbol)  # Returns full scraped data
extract_financials(data)  # Extracts metrics for DB
```

**Data Extraction Logic:**
- Revenue, Net Income, EPS: Extracted from Profit & Loss table (latest column)
- ROE, P/E, Debt/Equity: Extracted from overview section
- Fallback to overview if table data missing

### Database Schema for Future Updates
**FinancialStatement table supports:**
- `source` field: 'screener', 'manual_upload', 'ai_bulk_upload'
- `period_end` + `company_id`: Composite unique constraint
- Easy updates: Just run scraper again, it updates existing records

**Update Pattern:**
```python
# Upsert logic - updates if exists, creates if new
existing = db.query(FinancialStatement).filter(
    FinancialStatement.company_id == company.id,
    FinancialStatement.source == 'screener'
).first()

if existing:
    existing.revenue = new_revenue
    # ... update other fields
else:
    fs = FinancialStatement(...)
    db.add(fs)
db.commit()
```

### Test Scripts
- `test_scraper.py` - Test single company (HDFCBANK)
- `test_scraper_db.py` - Scrape all 10 database companies
- `scrape_financial_sector.py` - Scrape financial sector companies

### Results (2024-12-13)
- ✅ 9/10 companies successfully scraped
- Companies: RELIANCE, TCS, HDFCBANK, WIPRO, TATASTEEEL, INFOSYS, DATAPATNS, NEULANDLAB, WAAREEENER
- Data visible in Financials tab

### Future Enhancements Ready
Schema supports adding:
- Quarterly time-series data
- Balance sheet metrics
- Cash flow data
- Additional ratios

Just need to expand `FinancialStatement` model or create new tables.

## 🎯 Next Steps

1. Monitor scraped data quality in webapp
2. Add periodic scraper runs (weekly/monthly updates)
3. Expand to more companies as needed
4. Consider adding quarterly data tables

---
*Last Updated: 2024-12-13 14:41 IST*
