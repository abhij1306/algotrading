# Smart Trader - Quick Start Guide

## Overview

Smart Trader is a multi-agent NSE paper trading system integrated as the fourth tab in the AlgoTrading application.

## Prerequisites

- Python 3.11+
- Node.js 18+
- Running PostgreSQL database (for main application)
- Fyers API credentials (already configured in your project)

## Installation

### 1. Backend Dependencies

The required packages (`pyyaml` and `pytz`) have been added to `requirements.txt`:

```bash
cd backend
pip install -r requirements.txt
pip install apscheduler  # Required for scheduled tasks
```

### 2. Optional: Groq API Key (for AI features)

To enable AI-powered signal analysis:

1. Get a free API key from [Groq](https://console.groq.com/)
2. Add to your `.env` file:
   ```
   GROQ_API_KEY=your_groq_api_key_here
   ```

> **Note**: The system works perfectly without Groq API - AI features will simply be disabled.

## Running the Application

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

Navigate to `http://localhost:3000` and click on the **"Smart Trader"** tab.

## How It Works

### 1. Start the Scanner
- Click **"Start Scanner"** button
- System checks if market is open (09:15-15:30 IST, Monday-Friday)
- Scanners run every 5 minutes automatically

### 2. View Trading Signals
- Stock signals from NSE F&O universe
- Index options signals (NIFTY, BANKNIFTY)
- Each signal shows:
  - Entry price, Stop Loss, Target
  - Momentum score and confidence level
  - Detailed reasons for the signal
  - Risk:Reward ratio

### 3. Execute Trades (Paper Money)
- Click **"Take Trade"** on any signal
- Risk validation happens automatically:
  - Checks daily trade limit (5 trades/day)
  - Validates capital availability
  - Ensures R:R ratio > 1.5:1
  - Applies symbol cooldown (30 minutes)
- Trade executes with realistic slippage (0.05%)

### 4. Monitor Positions
- View all open positions in real-time
- P&L updates automatically
- Positions auto-close when Stop Loss or Target is hit

### 5. Stop Scanner
- Click **"Stop Scanner"**
- All open positions can be manually closed
- View final P&L summary

## Configuration

Edit `backend/app/smart_trader/config.yaml` to customize:

```yaml
scan_interval_sec: 300  # Scan every 5 minutes
risk:
  max_trades_per_day: 5
  max_risk_per_trade_pct: 2  # 2% of capital per trade
  max_daily_loss_pct: 5       # Stop trading at -5% daily loss
paper_trading:
  initial_capital: 100000      # ₹1,00,000 starting capital
  slippage_pct: 0.05           # 0.05% realistic slippage
```

## Features

✅ **Paper Trading Only** - No real money at risk  
✅ **Multi-Agent System** - 7 specialized agents working in coordination  
✅ **Risk Management** - Multiple layers of validation  
✅ **Real-time Monitoring** - Positions tracked every 30 seconds  
✅ **Explainable Signals** - Every signal has clear, documented reasons  
✅ **Performance Tracking** - Win rate, profit factor, max drawdown  
✅ **Market Hours Aware** - Automatically starts/stops with market  

## Safety Features

- **No Broker Integration**: Pure paper trading simulation
- **User Approval Required**: Every trade needs manual confirmation
- **Hard Limits**: Cannot exceed 5 trades/day or 5% daily loss
- **Symbol Cooldown**: prevents overtrading same symbol
- **Position Sizing**: Based on risk amount, not account size

## Architecture

```
Orchestrator Agent
├── Stock Scanner (NSE F&O)
├── Options Scanner (NIFTY/BANKNIFTY)
├── Decision Agent (Merge & Rank)
├── Risk Agent (Validation)
├── Execution Agent (Paper Trading)
└── Journal Agent (P&L Tracking)
```

## API Endpoints

All endpoints under `/api/smart-trader/`:

- `POST /start` - Start the system
- `POST /stop` - Stop the system
- `GET /status` - Get system status
- `GET /signals` - Get current trading signals
- `POST /execute-trade` - Execute a paper trade
- `GET /positions` - Get open positions
- `GET /pnl` - Get P&L summary
- `GET /tradebook` - Get trade history

## Troubleshooting

### Market Closed Error
- Smart Trader only runs during market hours (09:15-15:30 IST, Mon-Fri)
- Outside these hours, you'll see: *"Market is currently closed..."*

### No Signals Appearing
- Wait for first scan cycle to complete (5 minutes)
- Check console logs for any errors
- Ensure Fyers API token is valid

### Groq AI Features Not Working
- Check `GROQ_API_KEY` in `.env` file
- Verify API key is valid at https://console.groq.com/
- System works fine without AI - it just won't show AI summaries

## Next Steps

1. **Test During Market Hours**: Try running the scanner during live market hours
2. **Execute Sample Trades**: Take a few paper trades to test the flow
3. **Monitor Performance**: Track your paper trading statistics
4. **Customize Settings**: Adjust risk parameters in `config.yaml`

## Support

For issues or questions:
1. Check the [walkthrough.md](file:///C:/Users/abhij/.gemini/antigravity/brain/c628982b-dc5e-4b19-949a-1b6eb60ca4ca/walkthrough.md) for detailed documentation
2. Review the [implementation_plan.md](file:///C:/Users/abhij/.gemini/antigravity/brain/c628982b-dc5e-4b19-949a-1b6eb60ca4ca/implementation_plan.md) for architecture details

---

**Status**: ✅ Ready to use  
**Version**: 1.0.0  
**Last Updated**: December 2024
