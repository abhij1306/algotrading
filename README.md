# AlgoTrading System — Research & Backtesting Platform

**A personal, zero-cost algorithmic trading research system built to backtest strategies, analyze trades, and generate deterministic intraday and swing signals using clean data pipelines and modern tooling.**

[![Status](https://img.shields.io/badge/status-production-green)]()
[![Data Coverage](https://img.shields.io/badge/data-2016--present-blue)]()
[![Companies](https://img.shields.io/badge/companies-2426-orange)]()

This project focuses on **correct data modeling**, **reproducibility**, and **disciplined system design**, not black-box prediction.

---

## Design Principles

### Deterministic strategies first
All trade signals are rule-based and reproducible.

### Clear separation of concerns
Live trading, research, and historical analytics are isolated by design.

### LLMs as assistants, not decision-makers
Language models are used for reasoning, explanation, and confidence analysis — never for generating trades.

### Data correctness over convenience
Corporate actions, survivorship bias, and data gaps are handled explicitly.

---

## 🎯 Features

### 📊 Stock Screener
- **2,426 NSE Companies**: Complete database with technicals and financials
- **Dual View**: Technicals (LTP, Volume, RSI, MACD) + Financials (Revenue, EPS, ROE, P/E)
- **Advanced Filtering**: 
  - Symbol search with autocomplete
  - Sector filter (502 companies across 15+ sectors)
  - Price shockers, volume shockers, 52W highs
- **Smart Pagination**: Navigate all companies with working page controls
- **Real-time Data**: Live quotes from Fyers API
- **Export**: Download filtered results as CSV

### 📈 Portfolio Risk Analyzer
- **Comprehensive Risk Assessment**:
  - **Market Risk**: VaR (95%), CVaR, Max Drawdown, Volatility, Beta, Sharpe Ratio
  - **Portfolio Concentration**: HHI Index, position allocation breakdown
  - **Fundamental Risk**: Leverage ratios, Financial Fragility Score, ROE
- **Interactive Portfolio Building**:
  - Symbol search with autocomplete
  - Auto-calculation: Invested value = Quantity × Avg Buy Price
  - Real-time portfolio composition tracking
- **Visual Analytics**:
  - Portfolio vs Nifty 50 performance comparison
  - Sector allocation pie chart
  - Risk-return scatter plot
- **Save/Load Portfolios**: Export and import portfolio configurations

### 📉 Strategy Backtester
- **Event-Driven Engine**: Realistic backtesting for Equity and Options
- **Strategies**: 
  - **ORB (Opening Range Breakout)**: Intraday strategy with Black-Scholes pricing
- **Performance Metrics Dashboard**:
  - 4-Card Layout: Net Profit, Win Rate, Profit Factor, Max Drawdown
  - Real-time Calculations: CAGR, Sharpe Ratio, drawdown periods
  - Visual Design: Color-coded metrics (red for losses, green for profits)
- **Features**:
  - Option Pricing: Synthetic premiums using Black-Scholes model
  - ATR-Based Risk Management: Dynamic stop loss and take profit
  - Position Sizing: Kelly Criterion support
  - Analytics: Equity Curve, Trade Logs
- **Data**: Fyers API Integration (5-minute intraday, 100 days history)

### 🤖 Smart Trader (AI-Powered)
- **Deterministic Signal Generation**: Signals always generated, never blocked by LLM
- **Architecture**:
  1. MarketSnapshot: Immutable market data with indicators
  2. 5 Signal Generators: Momentum, Volume, Range Expansion, Reversal, Index Alignment
  3. Signal Aggregator: Merges signals with confluence counting
  4. LLM Signal Analyst: Enhances confidence and provides reasoning
  5. Confidence Engine: Computes LOW/MEDIUM/HIGH confidence
  6. Trade Constructor: Builds entry/SL/target with ATR-based risk
  7. Risk Agent: Hard execution gate (capital limits, R:R, cooldown)
- **Features**:
  - Confluence Signals: Multiple signal types combined per symbol
  - Transparent Reasoning: Deterministic + LLM narrative
  - Risk Flags: LLM identifies potential failure modes
  - Paper Trading: Simulate trades with realistic execution
  - Terminal: Track open positions, P&L, trade history

---

## ⛔ Intentional Limitations

**This system intentionally does NOT do the following:**

These are deliberate design choices to maintain focus, reliability, and realistic expectations:

### Data & Backtesting
- ❌ **No Tick-Level Backtesting**: Minimum granularity is 5-minute candles (realistic for retail traders)
- ❌ **No Real-Time NSE Scraping**: Uses official NSE archives only (legal and reliable)
- ❌ **No Intraday Corporate Action Adjustments**: CA adjustments are end-of-day only
- ❌ **No Bid-Ask Spread Modeling**: Uses close prices for simplicity

### Signal Generation
- ❌ **No Pure ML-Based Signals**: Deterministic technical analysis only (LLM enhances, doesn't decide)
- ❌ **No Sentiment Analysis**: No news/social media parsing (reduces noise)
- ❌ **No Options Greeks Calculation**: Uses Black-Scholes for synthetic premiums only

### Execution
- ❌ **No Real Money Execution**: Paper trading only (safety first)
- ❌ **No High-Frequency Trading**: Not designed for HFT (latency not optimized)
- ❌ **No Automated Order Placement**: Manual review required for all trades

### Infrastructure
- ❌ **No Cloud Deployment**: Local-first architecture (cost and control)
- ❌ **No Multi-User Support**: Single-user system (simplicity)
- ❌ **No Real-Time Streaming**: Batch updates only (sufficient for daily/intraday strategies)

**Why These Limitations Matter:**
- Prevents feature creep and scope expansion
- Sets realistic expectations for contributors
- Maintains system reliability and auditability
- Focuses on what retail traders actually need
- **These exclusions are deliberate and foundational to the design**

---

## 🏗️ System Architecture

### 3-Tier Data Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    DATA FLOW ARCHITECTURE                    │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐│
│  │  COLD LAYER  │────▶│  WARM LAYER  │────▶│  HOT LAYER   ││
│  │              │     │              │     │              ││
│  │ NSE + Yahoo  │     │  PostgreSQL  │     │  Fyers API   ││
│  │  Parquet     │     │  Recent Data │     │  Live Data   ││
│  │ 2016-present │     │  2,426 cos.  │     │  Real-time   ││
│  └──────────────┘     └──────────────┘     └──────────────┘│
│         ▲                     ▲                     ▲        │
│         │                     │                     │        │
│         └─────────────────────┴─────────────────────┘        │
│                  UnifiedDataService                          │
│              (Intelligent Query Routing)                     │
└─────────────────────────────────────────────────────────────┘
```

#### Cold Layer (Historical - Parquet Files)
- **Location**: `nse_data/processed/`
- **Data Sources**:
  - NSE Bhavcopy (2016-2023): 2,835 symbols
  - Yahoo Finance (2024-present): 2,426 symbols + 10 indices
- **Access**: `NSEDataReader` via DuckDB
- **Use Cases**: Backtesting, long-term analysis, research

#### Warm Layer (Operational - PostgreSQL)
- **Database**: `algotrading` (PostgreSQL 14+)
- **Tables**: 
  - `companies` (2,426 active)
  - `historical_prices` (Fyers + Yahoo, 90-180 days rolling)
  - `financial_statements` (246 companies with data)
  - `corporate_actions` (391 actions)
  - `smart_trader_signals`
- **Access**: `DataRepository` via SQLAlchemy
- **Use Cases**: Screener, scanner, portfolio analysis, live trading

#### Hot Layer (Real-time - Fyers API)
- **Coverage**: Live quotes, intraday candles (1m, 5m, 15m, 30m, 60m)
- **Access**: `data_fetcher.py`
- **Use Cases**: Live trading, real-time P&L, market monitoring

---

## 📁 Project Structure

```
AlgoTrading/
├── README.md                      # This file (complete documentation)
├── DATA_ARCHITECTURE.md           # Detailed data architecture guide
├── .env, .gitignore, LICENSE      # Configuration
├── requirements.txt
│
├── scripts/                       # All operational scripts
│   ├── README.md
│   ├── data_pipeline/            # Data fetching & processing
│   │   ├── yahoo_downloader.py
│   │   ├── yahoo_data_cleaner.py
│   │   ├── yahoo_financials_fetcher.py
│   │   ├── update_daily_data.py
│   │   └── apply_corporate_actions.py
│   ├── maintenance/              # System health & diagnostics
│   │   ├── health_check.py
│   │   ├── audit_data_management.py
│   │   └── verify_symbols.py
│   └── setup/                    # Initial setup
│       ├── init_database.py
│       └── run_daily_update.bat
│
├── backend/                       # FastAPI backend
│   ├── app/
│   │   ├── main.py               # API endpoints
│   │   ├── database.py           # SQLAlchemy models
│   │   ├── unified_data_service.py  # 3-tier routing
│   │   ├── nse_data_reader.py    # Parquet reader
│   │   ├── data_repository.py    # Database operations
│   │   ├── data_fetcher.py       # Fyers integration
│   │   ├── trending_scanner.py   # Live scanner
│   │   ├── portfolio_risk.py     # Risk analysis
│   │   ├── strategies/           # Backtesting engine
│   │   └── smart_trader/         # AI trading system
│   └── requirements.txt
│
├── frontend/                      # Next.js 14 frontend
│   ├── app/
│   │   └── page.tsx              # Main app (tabs)
│   └── components/
│       ├── StockScreener.tsx
│       ├── UnifiedPortfolioAnalyzer.tsx
│       └── strategies/
│
├── nse_data/                      # NSE data pipeline
│   ├── README.md
│   ├── raw/
│   │   ├── equities/             # NSE bhavcopy CSVs
│   │   ├── indices/              # NSE index CSVs
│   │   └── yahoo/                # Yahoo Finance CSVs
│   ├── processed/
│   │   ├── equities_clean/
│   │   │   └── equity_ohlcv.parquet  # ⭐ Main equity data
│   │   ├── indices_clean/
│   │   │   └── index_ohlcv.parquet   # ⭐ Index data
│   │   ├── adjusted_prices/
│   │   │   └── equity_ohlcv_adj.parquet  # CA-adjusted
│   │   └── backups/
│   └── metadata/
│       ├── equity_list.csv
│       ├── index_master.csv
│       └── symbol_sector_map.csv
│
├── docs/                          # Documentation
│   ├── README.md
│   ├── SMART_TRADER_README.md
│   └── DAILY_UPDATE_SETUP.md
│
├── fyers/                         # Fyers broker integration
├── KotakNeo/                      # Kotak broker (future)
├── Zerodha/                       # Zerodha broker (future)
│
└── logs/                          # Application logs
    ├── .gitignore
    └── README.md
```

---

## 📂 Data Setup & Disclaimer

**This repository does NOT include any historical or live market data.**

Market data is subject to licensing and redistribution restrictions imposed by data providers (e.g., NSE, brokers, third parties). Users are responsible for obtaining and using market data in compliance with applicable terms.

**To use this system, you must obtain market data independently:**
- **NSE Historical Data**: Download from [NSE Archives](https://archives.nseindia.com/) (free, registration required)
- **Yahoo Finance Data**: Fetch using the included `yahoo_downloader.py` script (requires `yfinance` library)
- **Live Data**: Requires Fyers API credentials (optional, for real-time features)

All data acquisition scripts are provided in `scripts/data_pipeline/`. Users are responsible for compliance with data provider terms of service.

**Data from different sources may vary slightly in OHLC values**; this system prioritizes continuity, transparency, and reproducibility over tick-level precision.

---

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- Node.js 18+
- PostgreSQL 14+
- Fyers API credentials (optional, for live data)

### Installation

1. **Clone the repository**
```bash
git clone https://github.com/yourusername/AlgoTrading.git
cd AlgoTrading
```

2. **Set up Python environment**
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

3. **Set up database**
```bash
# Create PostgreSQL database
createdb algotrading

# Initialize tables
python scripts/setup/init_database.py
```

4. **Configure environment**
```bash
# Create .env file
copy .env.example .env

# Edit .env with your credentials:
# - Database connection
# - Fyers API keys (optional)
# - OpenRouter API key (for AI features)
```

5. **Download historical data**
```bash
# Download Yahoo Finance data (2024-present)
python scripts/data_pipeline/yahoo_downloader.py

# Clean and merge with NSE data
python scripts/data_pipeline/yahoo_data_cleaner.py

# Apply corporate actions
python scripts/data_pipeline/apply_corporate_actions.py
```

6. **Install frontend dependencies**
```bash
cd frontend
npm install
```

### Running the Application

**Backend:**
```bash
cd backend
python -m app.main
# Backend: http://localhost:8000
# API Docs: http://localhost:8000/docs
```

**Frontend:**
```bash
cd frontend
npm run dev
# Frontend: http://localhost:3000
```

---

## 💾 Database Schema

### Core Tables

#### `companies`
- Master table for all NSE companies
- Columns: id, symbol, name, sector, industry, market_cap, is_fno, is_active
- Records: 2,426 active companies

#### `historical_prices`
- Daily OHLCV data with technical indicators
- Columns: company_id, date, open, high, low, close, volume, adj_close, ema_20, ema_34, ema_50, rsi, atr, source
- Coverage: Last 90-180 days (rolling window)
- Source tags: 'fyers', 'yahoo'

#### `financial_statements`
- Quarterly/annual financial data
- Columns: company_id, period_end, period_type, fiscal_year, quarter, revenue, net_income, eps, ebitda, total_assets, debt_to_equity, roe, roa, source
- Records: 246 companies with data
- Sources: 'screener.in', 'yahoo_finance'

#### `corporate_actions`
- Stock splits, bonuses, dividends
- Columns: company_id, ex_date, action_type, ratio, adjustment_factor
- Records: 391 actions

#### `smart_trader_signals`
- AI-generated trading signals
- Columns: symbol, signal_type, confidence, entry_price, stop_loss, target, reasoning, risk_flags, status
- Status: 'pending', 'executed', 'rejected'

---

## 🔄 Data Update Workflows

### Daily Update (Automated)
```bash
# Scheduled via Windows Task Scheduler (3:45 PM weekdays)
scripts/setup/run_daily_update.bat

# What it does:
1. Download latest NSE bhavcopy
2. Clean and merge with Parquet
3. Apply corporate actions
4. Update PostgreSQL with Fyers data
5. Calculate technical indicators
```

### Yahoo Finance Gap Fill (On-demand)
```bash
# Download 2024-present OHLCV data
python scripts/data_pipeline/yahoo_downloader.py

# Clean and merge
python scripts/data_pipeline/yahoo_data_cleaner.py

# Apply corporate actions
python scripts/data_pipeline/apply_corporate_actions.py
```

### Financial Data Fetch (On-demand)
```bash
# Fetch quarterly/annual financials for 2,180 companies
python scripts/data_pipeline/yahoo_financials_fetcher.py
```

---

## 🎨 Corporate Actions Logic

### Backward Adjustment (Industry Standard)
```python
# For dates BEFORE ex-date:
adjusted_price = raw_price / adjustment_factor
adjusted_volume = raw_volume * adjustment_factor

# Example: 1:2 split (factor = 2.0)
# Before split: ₹100 → Adjusted: ₹50
# Volume: 1000 → Adjusted: 2000
```

### Critical: Multiple Corporate Actions Ordering

**When a stock has multiple corporate actions (e.g., bonus in 2019, split in 2021):**

```python
# MUST apply factors in DESCENDING ex-date order (newest first)
ca_df = ca_df.sort_values("ex_date", ascending=False)

# Example:
# 2021-06-15: Split 1:2 (factor = 2.0)
# 2019-03-10: Bonus 1:1 (factor = 2.0)

# For price on 2018-01-01:
# Step 1: Apply 2021 split: ₹100 / 2.0 = ₹50
# Step 2: Apply 2019 bonus: ₹50 / 2.0 = ₹25
# Final adjusted price: ₹25
```

**Why This Matters:**
- Prevents compounding errors
- Ensures consistent price history
- Matches industry standard (Yahoo, Bloomberg)

### File Usage
- **Backtesting**: Use `adjusted_prices/equity_ohlcv_adj.parquet`
- **Live Trading**: Use `equities_clean/equity_ohlcv.parquet`

---

## 🌐 API Endpoints

### Screener
- `GET /api/screener` - All companies with latest data
- `GET /api/screener/trending` - Live scanner results
- `GET /api/screener/{symbol}` - Company details

### Historical Data
- `GET /api/historical/{symbol}` - Historical OHLCV
- `GET /api/intraday/{symbol}` - Intraday candles

### Portfolio
- `POST /api/risk/comprehensive` - Portfolio risk analysis
- `POST /api/portfolio/save` - Save portfolio
- `GET /api/portfolio/load` - Load portfolio

### Backtesting
- `POST /api/backtest/orb` - Run ORB strategy backtest

### Smart Trader
- `GET /api/smart-trader/signals` - Active signals
- `POST /api/smart-trader/execute` - Execute trade (paper)

---

## 🔧 Configuration

### Environment Variables (.env)
```env
# Database
DB_HOST=localhost
DB_PORT=5432
DB_NAME=algotrading
DB_USER=postgres
DB_PASSWORD=your_password

# Fyers API (optional)
FYERS_APP_ID=your_app_id
FYERS_SECRET_KEY=your_secret
FYERS_ACCESS_TOKEN=your_token

# OpenRouter (for AI features)
OPENROUTER_API_KEY=your_key
```

### Data Paths
- NSE Data: `c:\AlgoTrading\nse_data\`
- Logs: `c:\AlgoTrading\logs\`
- Backups: `c:\AlgoTrading\nse_data\processed\backups\`

---

## 🛠️ Maintenance

### Daily
- ✅ Automated data update (3:45 PM)
- ⚠️ Fyers token renewal (if expired)
- 📊 Monitor disk space

### Weekly
- Review logs in `logs/`
- Run health check: `python scripts/maintenance/health_check.py`
- Verify data quality: `python scripts/maintenance/verify_symbols.py`

### Monthly
- Audit data management: `python scripts/maintenance/audit_data_management.py`
- Clean old backups (keep last 5)
- Update financial data: `python scripts/data_pipeline/yahoo_financials_fetcher.py`

---

## 🐛 Troubleshooting

### Scanner Not Updating
1. Check Fyers authentication
2. Restart backend: `python -m app.main`
3. Verify database connection: `psql -U postgres -d algotrading`

### Missing Historical Data
1. Check Parquet files exist: `ls nse_data/processed/equities_clean/`
2. Verify DuckDB installed: `pip install duckdb`
3. Run data validator: `python nse_data_validator.py`

### Corporate Actions Not Applied
1. Check corporate actions in database: `SELECT COUNT(*) FROM corporate_actions`
2. Re-run adjustment: `python scripts/data_pipeline/apply_corporate_actions.py`
3. Verify adjusted file exists: `ls nse_data/processed/adjusted_prices/`

### Health Check
```bash
python scripts/maintenance/health_check.py
```

**Expected Output:**
- ✅ Database connection
- ✅ Parquet files readable
- ✅ Fyers API accessible
- ✅ All tables populated

---

## 📊 Data Coverage

### NSE Pipeline
- Raw bhavcopy files: ~3,060 (2016-2025)
- Processed symbols: 2,835
- Corporate actions: 391
- Sector classifications: 502 companies

### Yahoo Finance
- Equities downloaded: 2,426 (2024-present)
- Indices downloaded: 10
- Financial statements: 246 companies (more pending)

### PostgreSQL
- Active companies: 2,426
- Historical price records: ~500K (rolling 180 days)
- Financial statements: 246 companies
- Smart Trader signals: Growing

---

## 🔐 Security Notes

- `.env` file is gitignored (contains credentials)
- Fyers tokens expire every 24 hours (auto-renewal needed)
- PostgreSQL uses local authentication
- No sensitive data in logs

---

## 📚 Documentation

- **README.md** (this file): Complete system documentation
- **DATA_ARCHITECTURE.md**: Detailed data architecture guide
- **scripts/README.md**: Scripts documentation
- **docs/SMART_TRADER_README.md**: Smart Trader system guide
- **docs/DAILY_UPDATE_SETUP.md**: Scheduling instructions
- **nse_data/README.md**: NSE pipeline documentation

---

## 🎓 Architecture Principles

1. **No Data Duplication**: Each layer serves distinct purpose
2. **On-Demand Reads**: DuckDB reads Parquet files as needed
3. **Lean Database**: PostgreSQL stays under 200MB (auto-cleanup)
4. **Smart Caching**: Intent-based cache keys
5. **Read-Only Service**: Writes only in specific jobs
6. **Corporate Action Aware**: Backward adjustment for backtesting

---

## 🤝 Contributing

This is a personal project, but suggestions and feedback are welcome!

---

## 📄 License

MIT License - see LICENSE file for details

---

## 📞 Support

For issues or questions:
- Check documentation in `docs/`
- Review `DATA_ARCHITECTURE.md` for data flow
- Run health check: `python scripts/maintenance/health_check.py`

---

**Last Updated**: December 17, 2025  
**Status**: ✅ PRODUCTION READY  
**Data Coverage**: 2016-present (seamless)  
**Companies**: 2,426 active NSE stocks  
**System Health**: ✅ All components operational