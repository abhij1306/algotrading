# SmartTrader 3.0

> AI-powered quantitative trading platform for the Indian NSE market

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Next.js 14](https://img.shields.io/badge/next.js-14-black)](https://nextjs.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## Features

### 📊 Stock Screener
- Filter 2400+ NSE stocks by technical indicators
- 33+ index universes (NIFTY50, NIFTY500, sector indices)
- Real-time price updates via WebSocket
- Technical indicators: RSI, MACD, EMA, ADX, Bollinger Bands

### 📈 Backtesting Engine
- Test strategies on historical data
- Multiple asset types: Stocks, Options, Index Universes
- Performance metrics: Sharpe ratio, max drawdown, win rate
- Monte Carlo simulation and walk-forward analysis

### 💹 Live Trading
- Paper trading mode for risk-free testing
- Live trading via Fyers broker integration
- Real-time position tracking and P&L
- Risk management with circuit breakers

### 🎯 Options Trading
- Option chain analysis with Greeks
- Black-Scholes pricing model
- Strike selection and expiry management
- Multi-leg strategies (spreads, straddles)

---

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL (or SQLite for development)
- Fyers trading account (for live data)

### Installation

**1. Clone the repository**
```bash
git clone https://github.com/yourusername/smarttrader.git
cd smarttrader
```

**2. Backend Setup**
```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env with your Fyers credentials

# Run backend
python -m app.main
```

Backend will start on `http://localhost:8000`

**3. Frontend Setup**
```bash
cd frontend
npm install
npm run dev
```

Frontend will start on `http://localhost:3000`

**4. Load Initial Data**
```bash
# Load bhavcopy (daily stock data)
cd backend
python scripts/load_bhavcopy.py --file ../nse_data/bhavcopy/sec_bhavdata_full_DDMMYYYY.csv

# Index data is loaded automatically from nse_data/index_universe/constituents/
```

---

## Usage

### Stock Screener

1. Navigate to **Screener** page
2. Select universe (e.g., NIFTY50)
3. Search for specific stocks
4. Click on any stock to view details in Terminal

### Backtesting

1. Navigate to **Backtest** page
2. Click **New Backtest**
3. Configure:
   - Asset type (Stocks/Options/Index)
   - Date range
   - Strategy parameters
   - Risk management rules
4. Click **Run Backtest**
5. View results with charts and metrics

### Live Trading

1. Connect Fyers account:
   - Go to Settings
   - Click **Connect Fyers**
   - Authorize on Fyers website
2. Toggle **PAPER** / **LIVE** mode
3. Place orders from Terminal
4. Monitor positions in real-time

---

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         Frontend                             │
│  Next.js 14 + React + TypeScript + Tailwind CSS            │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP/WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                         Backend                              │
│  FastAPI + Python 3.11 + SQLAlchemy                         │
├─────────────────────────────────────────────────────────────┤
│  Routers: screener, backtest, trading, options, market      │
│  Services: symbol_master, index_universe_loader, fyers      │
│  Models: Company, HistoricalPrice, LiveOrder, LivePosition  │
└─────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                    ▼                   ▼
            ┌──────────────┐    ┌──────────────┐
            │  PostgreSQL  │    │  Fyers API   │
            │   Database   │    │   Broker     │
            └──────────────┘    └──────────────┘
```

### Key Components

**Backend Services:**
- `SymbolMaster` - Symbol format conversion (DB ↔ Fyers ↔ Display)
- `IndexUniverseLoader` - Manages 33+ NSE indices from CSV files
- `FyersClient` - Broker API integration
- `LiveMarketService` - WebSocket streaming
- `OrderExecutionService` - Order routing with risk checks
- `RiskManager` - Pre-trade and post-trade risk validation

**Frontend Components:**
- `Screener` - Stock filtering and selection
- `Terminal` - Trading interface with order entry
- `Backtest` - Strategy testing and analysis
- `OptionChain` - Option strike selection
- `Dashboard` - Portfolio overview

---

## Configuration

### Environment Variables

Create `.env` file in `backend/` directory:

```env
# Fyers API Credentials
CLIENT_ID=your_fyers_client_id
SECRET_KEY=your_fyers_secret_key
REDIRECT_URI=http://localhost:3000/auth/callback

# Database
DATABASE_URL=postgresql://user:password@localhost/smarttrader
# Or use SQLite for development:
# DATABASE_URL=sqlite:///./smarttrader.db

# Trading Mode
TRADING_MODE=PAPER  # PAPER or LIVE

# Risk Management
MAX_DAILY_LOSS=5000
MAX_POSITION_SIZE=100000
```

### Data Sources

**Stock Data:**
- Daily: NSE Bhavcopy (uploaded via `/api/upload/bhavcopy`)
- Live: Fyers WebSocket

**Index Data:**
- NSE CSV files in `nse_data/index_universe/constituents/`
- Auto-loaded on startup

---

## API Documentation

### REST API

Full API documentation available at `http://localhost:8000/docs` (Swagger UI)

**Key Endpoints:**

```
GET  /api/screener/results?universe=nifty50&page=1&limit=25
GET  /api/screener/indices
GET  /api/market/quotes/live?symbols=SBIN,RELIANCE
POST /api/backtest/v2/run
GET  /api/backtest/v2/results/{run_id}
POST /api/trading/order
GET  /api/trading/positions
GET  /api/options/chain?underlying=NIFTY&strike_count=10
```

### WebSocket API

Connect to `ws://localhost:8000/api/websocket/stream`

**Subscribe to symbols:**
```json
{
  "action": "subscribe",
  "symbols": ["SBIN", "RELIANCE", "TCS"]
}
```

**Receive ticks:**
```json
{
  "type": "ticker",
  "data": {
    "symbol": "SBIN",
    "ltp": 625.50,
    "change_pct": 1.25,
    "volume": 1234567
  }
}
```

---

## Performance

### Optimizations

- Database indexes on frequently queried columns
- Connection pooling (10 connections, 20 max overflow)
- API response caching with TTL
- WebSocket for real-time updates (no polling)
- Code splitting and lazy loading in frontend
- Memoization of expensive calculations

### Benchmarks

| Operation | Response Time |
|-----------|---------------|
| Screener load (25 stocks) | <500ms |
| Live quote fetch | <200ms |
| Backtest execution | 2-5s (depends on date range) |
| WebSocket latency | <50ms |

---

## Testing

### Backend Tests
```bash
cd backend
pytest
pytest --cov=app tests/  # With coverage
```

### Frontend Tests
```bash
cd frontend
npm test
npm run test:e2e  # End-to-end tests
```

---

## Deployment

### Production Checklist

- [ ] Set `TRADING_MODE=LIVE` only after thorough testing
- [ ] Use PostgreSQL (not SQLite)
- [ ] Enable HTTPS
- [ ] Set up proper authentication
- [ ] Configure rate limiting
- [ ] Set up monitoring and alerts
- [ ] Backup database regularly
- [ ] Review risk management settings

### Docker Deployment

```bash
# Build images
docker-compose build

# Start services
docker-compose up -d

# View logs
docker-compose logs -f
```

---

## Troubleshooting

### Common Issues

**"Fyers token expired"**
- Solution: Reconnect Fyers from Settings page
- Tokens expire at midnight IST daily

**"No stocks in screener"**
- Solution: Load bhavcopy data using `scripts/load_bhavcopy.py`
- Ensure index CSV files exist in `nse_data/index_universe/constituents/`

**"WebSocket not connecting"**
- Solution: Check if backend is running on port 8000
- Verify Fyers token is valid

**"Slow performance"**
- Solution: Check database indexes are created
- Review `COMPREHENSIVE_AUDIT_2026.md` for optimization tips

---

## Contributing

We welcome contributions! Please see [AGENTS.md](AGENTS.md) for development guidelines.

### Development Workflow

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`pytest` and `npm test`)
5. Commit your changes (`git commit -m 'Add amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

---

## Roadmap

### Q1 2026
- [x] Stock screener with 33+ indices
- [x] Real-time WebSocket streaming
- [x] Fyers broker integration
- [x] Paper trading mode
- [ ] Live trading with risk management

### Q2 2026
- [ ] Options trading terminal
- [ ] Multi-leg option strategies
- [ ] Advanced backtesting (Monte Carlo, walk-forward)
- [ ] Mobile app (React Native)

### Q3 2026
- [ ] AI-powered strategy generation
- [ ] Social trading features
- [ ] Portfolio optimization
- [ ] Tax reporting

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## Disclaimer

**Important:** This software is for educational and research purposes only. Trading in financial markets involves substantial risk of loss. Past performance is not indicative of future results. Always consult with a qualified financial advisor before making investment decisions.

The authors and contributors are not responsible for any financial losses incurred through the use of this software.

---

## Support

- **Documentation:** [AGENTS.md](AGENTS.md)
- **Issues:** [GitHub Issues](https://github.com/yourusername/smarttrader/issues)
- **Discussions:** [GitHub Discussions](https://github.com/yourusername/smarttrader/discussions)

---

## Acknowledgments

- [Fyers](https://fyers.in/) for broker API
- [NSE India](https://www.nseindia.com/) for market data
- [FastAPI](https://fastapi.tiangolo.com/) framework
- [Next.js](https://nextjs.org/) framework
- [Raycast](https://raycast.com/) for design inspiration

---

**Built with ❤️ for the Indian trading community**

**Last Updated:** February 18, 2026
