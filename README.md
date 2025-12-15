# AlgoTrading - NSE Portfolio Analyzer

Advanced portfolio risk analysis platform combining technical and fundamental analysis for NSE F&O stocks.

## 🎯 Features

### 📊 Stock Screener
- **2,386 NSE Companies**: Complete database with technicals and financials
- **Dual View**: Technicals (LTP, Volume, RSI, MACD) + Financials (Revenue, EPS, ROE, P/E)
- **Advanced Filtering**: 
  - Symbol search with autocomplete
  - **Sector Filter**: Filter by industry sector (39 companies classified across 15 sectors)
- **Smart Pagination**: Navigate all companies with working page controls
- **Sticky Headers**: Table headers stay visible while scrolling
- **Sorting**: Sort by any column (ascending/descending)
- **Export**: Download filtered results as CSV

### 📈 Portfolio Risk Analyzer
- **Comprehensive Risk Assessment**:
  - **Market Risk**: VaR (95%), CVaR, Max Drawdown, Volatility, Beta, Sharpe Ratio
  - **Portfolio Concentration**: HHI Index, position allocation breakdown
  - **Fundamental Risk**: Leverage ratios, Financial Fragility Score, ROE
- **Interactive Portfolio Building**:
  - Symbol search with autocomplete
  - **Auto-calculation**: Invested value = Quantity × Avg Buy Price
  - Real-time portfolio composition tracking
- **Visual Analytics**:
  - Portfolio vs Nifty 50 performance comparison
  - Sector allocation pie chart
  - Risk-return scatter plot
- **Optimized UX**:
  - Holdings table displayed at top
  - All risk metrics visible without scrolling
  - Compact layout for better space utilization
  - One-click "Create New Portfolio" button

### 📉 Strategy Backtester
- **Engine**: Event-driven backtesting for Equity and Options
- **Strategies**: 
  - **ORB (Opening Range Breakout)**: Intraday strategy with Black-Scholes pricing
- **Performance Metrics Dashboard**:
  - **4-Card Layout**: Net Profit, Win Rate, Profit Factor, Max Drawdown
  - **Real-time Calculations**: CAGR, Sharpe Ratio, drawdown periods
  - **Visual Design**: Color-coded metrics (red for losses, green for profits)
  - **Zero Mock Data**: All values from backend calculations
- **Features**:
  - **Option Pricing**: Synthetic premiums using Black-Scholes model
  - **ATR-Based Risk Management**: Dynamic stop loss and take profit based on volatility
  - **Position Sizing**: Kelly Criterion support (future), 1-lot minimum for options
  - **Analytics**: Equity Curve, Sharpe Ratio, Win Rate, Trade Logs
  - **Currency**: Proper rupee (₹) formatting throughout
- **Data**: Fyers API Integration for 5-minute Intraday Data
  - 299 NSE F&O stocks (3 months historical data)
  - NIFTY50-INDEX, Stock Futures, Index Options
- **Location**: Available on home page (`localhost:3000`) Backtest tab

### 🤖 Smart Trader (New)
- **Multi-Agent System**: Automated analysis and paper trading
- **Features**:
  - **Live Scanner**: Real-time momentum scanning of NSE F&O stocks
  - **Grid View**: Compact signal display with expandable details
  - **Paper Trading**: Simulate trades with realistic slippage and P&L tracking
  - **Risk Guard**: Automatic validation (Max 5 trades/day, Max Daily Loss)
- **Controls**: Manual Start/Stop scanner, Manual Trade execution
- **Mode**: Toggle between PAPER and LIVE (Safety prompts enabled)

## 📁 Project Structure

```
AlgoTrading/
├── frontend/                   # Next.js React frontend
│   ├── app/
│   │   ├── page.tsx           # Main app with Screener, Portfolio Risk, and Backtest tabs
│   │   └── globals.css        # Tailwind styles
│   └── components/
│       ├── strategies/        # Backtesting components
│       │   ├── PerformanceMetrics.tsx  # 4-card metrics dashboard
│       │   ├── EquityCurve.tsx         # Equity curve chart
│       │   ├── TradesTable.tsx         # Trade log table
│       │   └── StrategyConfiguration.tsx  # Backtest config form
│       ├── UnifiedPortfolioAnalyzer.tsx  # Portfolio risk analyzer
│       └── Navbar.tsx         # Tab navigation
├── backend/                    # FastAPI backend
│   ├── app/
│   │   ├── strategies/        # Strategy Engine
│   │   │   ├── orb_strategy.py        # ORB strategy logic
│   │   │   ├── performance_metrics.py # Metrics calculation
│   │   │   ├── black_scholes.py       # Options pricing
│   │   │   ├── backtest_engine.py     # Backtesting framework
│   │   │   └── atr_utils.py           # ATR calculation
│   │   ├── main.py            # API endpoints
│   │   ├── database.py        # PostgreSQL ORM models
│   │   ├── data_fetcher.py    # Fyers API integration
│   │   ├── data_repository.py # Data persistence layer
│   │   ├── screener_scraper.py # Screener.in data scraper
│   │   ├── risk_metrics.py    # Risk calculation engine
│   │   ├── portfolio_risk.py  # Portfolio risk analyzer
│   │   ├── excel_parser.py    # Financial statement parser
│   │   ├── indicators.py      # Technical indicators
│   │   └── populate_sectors.py # Sector classification (OpenRouter AI)
│   ├── data/
│   │   └── nse_fno_universe.json  # 299 F&O stocks list
│   ├── scripts/               # Utility scripts
│   │   ├── fetch_nse_symbols.py   # NSE symbol fetcher
│   │   └── populate_historicals.py # Historical data loader
│   ├── fetch_fno_data.py      # F&O data downloader (3 months, 5-min)
│   ├── fetch_intraday_data.py # Intraday data fetcher utility
│   └── check_nifty_data.py    # Data verification tool
├── Fyers/                      # Fyers broker integration (active)
├── NSEdata/                    # jugaad-data library for NSE data
├── KotakNeo/                   # Kotak broker integration (future)
├── Zerodha/                    # Zerodha broker integration (future)
└── docs/                       # Documentation
    └── risk_metrics_roadmap.md
```

## 🚀 Quick Start

### Backend Setup

```bash
cd backend
pip install -r requirements.txt
python -m app.main
# Backend runs on http://localhost:8000
```

### Frontend Setup

```bash
cd frontend
npm install
npm run dev
# Frontend runs on http://localhost:3000
```

### Database Setup

**PostgreSQL Required**

1. Install PostgreSQL
2. Create database: `createdb algotrading`
3. Configure `database/.env`:
```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=algotrading
DB_USER=postgres
DB_PASSWORD=your_password
```

4. Initialize database:
```bash
python init_database.py
```

## 📊 API Endpoints

### `/api/risk/comprehensive` (POST)
Comprehensive risk analysis for portfolio

**Request:**
```json
{
  "symbols": ["RELIANCE", "TCS"],
  "allocations": [60, 40],
  "lookback_days": 365
}
```

**Response:**
```json
{
  "portfolio_metrics": {
    "risk_grade": "B+",
    "sharpe_ratio": 1.45,
    "volatility": 0.18,
    "beta": 1.1
  },
  "position_metrics": [...],
  "warnings": [...]
}
```

### `/api/screener` (GET)
Screen stocks by strategy

**Parameters:**
- `strategy`: `momentum` | `reversal` | `volatility`

### `/api/upload/financials` (POST)
Upload Screener.in Excel files for fundamental data

## 🔧 Configuration

### Environment Variables
- `PORT`: Backend port (default: 8000)
- `HAS_FYERS`: Enable Fyers API integration

### Broker Integration

#### Fyers Setup
```bash
python fyers/fyers_login.py
```

#### Zerodha Setup
```bash
python zerodha/zerodha_login.py
```

## 🛠️ Development

### Code Style
- Backend: FastAPI, SQLAlchemy, pandas
- Frontend: Next.js, TypeScript, Tailwind CSS
- Database: SQLite with automatic migrations

### Adding New Risk Metrics
Edit `backend/app/risk_metrics.py` and add to `RiskMetricsEngine` class.

### Adding Strategies
Add to `backend/app/strategies.py` and define scoring logic.

## � Risk Analysis Methodology

### Technical Analysis
- **Sharpe Ratio**: Risk-adjusted returns
- **Sortino Ratio**: Downside deviation focus
- **VaR (95%, 99%)**: Value at Risk
- **CVaR**: Conditional Value at Risk
- **Beta**: Market correlation (vs NIFTY50)
- **Max Drawdown**: Peak-to-trough decline

### Fundamental Analysis
- **ROE**: Return on Equity
- **Debt/Equity Ratio**: Leverage analysis
- **Profit Margin**: Profitability
- **Current Ratio**: Liquidity
- **Interest Coverage**: Debt servicing ability

### Risk Grading Scale
- **A (1-2)**: Low risk, stable returns
- **B (3-4)**: Moderate risk
- **C (5-6)**: Medium-high risk
- **D (7-8)**: High risk
- **F (9-10)**: Very high risk

## 📄 License

This project is for educational purposes. Please comply with broker API terms of service and applicable regulations.

## 🙏 Acknowledgments

- NSE for F&O universe data
- Fyers & Zerodha for API access
- Screener.in for financial data format