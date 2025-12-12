# AlgoTrading - NSE Portfolio Analyzer

Advanced portfolio risk analysis platform combining technical and fundamental analysis for NSE F&O stocks.

## 🎯 Features

### Portfolio Risk Analyzer
- **Comprehensive Risk Assessment**: Technical + fundamental analysis
- **Real-time Data Fetching**: Auto-fetches from Fyers API or yfinance
- **Multi-symbol Analysis**: Analyze portfolios with custom allocations
- **Risk Metrics**: Sharpe ratio, VaR, Beta, volatility, drawdown analysis
- **Fundamental Scoring**: ROE, debt/equity, profit margins
- **Risk Grading**: A to F grade system with detailed warnings

### Stock Screener
- **Strategy-based Screening**: Momentum, reversal, volatility strategies
- **F&O Universe**: Focus on NSE futures & options stocks
- **Technical Indicators**: EMA, RSI, ATR, volume analysis
- **Database Caching**: Fast repeat queries with SQLite storage

## 📁 Project Structure

```
AlgoTrading/
├── frontend/                   # Next.js React frontend
│   └── components/
│       └── RiskDashboard.tsx  # Main portfolio analyzer UI
├── backend/                    # FastAPI backend
│   ├── app/
│   │   ├── main.py            # API endpoints
│   │   ├── risk_metrics.py    # Risk calculation engine
│   │   ├── data_fetcher.py    # Price data fetcher
│   │   ├── excel_parser.py    # Financial statement parser
│   │   └── database.py        # SQLite ORM models
│   └── data/
│       ├── screener.db        # Price & financial data cache
│       └── nse_fno_universe.json
├── fyers/                      # Fyers broker integration
└── zerodha/                    # Zerodha broker integration
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

### Database Initialization

The database auto-initializes on first run. To populate with historical data:

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