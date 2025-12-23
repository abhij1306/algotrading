# SmartTrader 3.0 - API Reference

**Version:** 2.0.0  
**Base URL:** `http://localhost:8000`

---

## Table of Contents
1. [System Health](#system-health)
2. [Market Data](#market-data)
3. [Analyst APIs](#analyst-apis)
4. [Quant APIs](#quant-apis)
5. [Screener APIs](#screener-apis)
6. [Lifecycle Management](#lifecycle-management)

---

## System Health

### GET /api/system/health
Get system-wide health status.

**Response:**
```json
{
  "status": "READY",
  "postgresql": "OK",
  "fyers": "OK",
  "last_data_update": "2025-12-22T10:00:00",
  "total_symbols": 3421,
  "recommendations": []
}
```

### GET /api/system/pre-flight/screener
Pre-flight check for Screener module.

**Response:**
```json
{
  "status": "READY",
  "data_available": true,
  "fyers_connected": true,
  "message": "Screener ready to use"
}
```

### GET /api/system/health-report
Comprehensive system health report.

**Response:**
```json
{
  "timestamp": "2025-12-22T11:50:00",
  "version": "2.0.0",
  "system": {
    "cpu_percent": 45.2,
    "memory_percent": 62.8,
    "disk_percent": 34.5
  },
  "database": {
    "status": "OK",
    "companies": 3421,
    "historical_prices": 1250000,
    "strategies": 12
  },
  "overall_status": "HEALTHY"
}
```

---

## Market Data

### GET /api/market/search?q={query}
Search for stocks by symbol or name.

**Parameters:**
- `q` (string, required) - Search query

**Response:**
```json
[
  {
    "symbol": "NIFTY50",
    "name": "NIFTY 50",
    "exchange": "NSE"
  }
]
```

---

## Analyst APIs

### GET /api/analyst/portfolios
List all portfolios.

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "Tech Portfolio",
    "created_at": "2025-12-22T10:00:00",
    "total_value": 1000000
  }
]
```

### POST /api/analyst/backtest/run
Run backtest with dynamic parameters (Analyst mode).

**Request Body:**
```json
{
  "strategy_name": "ORB",
  "symbol": "NIFTY50",
  "start_date": "2024-01-01",
  "end_date": "2024-12-01",
  "timeframe": "5MIN",
  "initial_capital": 100000,
  "params": {
    "stopLoss": 2.0,
    "takeProfit": 4.0,
    "riskPerTrade": 1.5
  }
}
```

**Response:**
```json
{
  "total_return_pct": 25.5,
  "sharpe_ratio": 1.8,
  "max_drawdown_pct": -8.2,
  "win_rate_pct": 65.0,
  "total_trades": 45,
  "equity_curve": [...],
  "trade_log": [...]
}
```

---

## Quant APIs

### GET /api/quant/backtest/strategies
List all strategy contracts.

**Response:**
```json
{
  "strategies": [
    {
      "strategy_id": "ORB_NIFTY_5MIN",
      "description": "Opening Range Breakout for NIFTY",
      "lifecycle_state": "LIVE",
      "regime": "TREND",
      "timeframe": "5MIN",
      "allowed_universes": ["NIFTY50_CORE"]
    }
  ]
}
```

### POST /api/quant/backtest/run
Run backtest with locked strategy contract.

**Request Body:**
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "universe_id": "NIFTY50_CORE",
  "start_date": "2024-01-01",
  "end_date": "2024-12-01"
}
```

**Response:**
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "universe_id": "NIFTY50_CORE",
  "num_symbols": 50,
  "avg_return_pct": 18.5,
  "avg_sharpe": 1.6,
  "max_drawdown_pct": -12.3,
  "total_trades": 250
}
```

### POST /api/quant/research/compare
Compare multiple strategies.

**Request Body:**
```json
{
  "strategy_ids": ["ORB_NIFTY", "TREND_BANK"],
  "universe_id": "NIFTY50_CORE",
  "start_date": "2024-01-01",
  "end_date": "2024-12-01"
}
```

**Response:**
```json
{
  "strategies": [...],
  "correlation_matrix": {...},
  "best_performer": "ORB_NIFTY"
}
```

---

## Lifecycle Management

### GET /api/quant/lifecycle/states/summary
Get strategy lifecycle state summary.

**Response:**
```json
{
  "summary": {
    "RESEARCH": 5,
    "PAPER": 3,
    "LIVE": 2,
    "RETIRED": 1
  },
  "total": 11
}
```

### POST /api/quant/lifecycle/transition
Transition strategy to new lifecycle state.

**Request Body:**
```json
{
  "strategy_id": "ORB_NIFTY_5MIN",
  "new_state": "PAPER",
  "reason": "Backtest showed 65% win rate",
  "approved_by": "quant_team"
}
```

**Valid Transitions:**
- RESEARCH → PAPER
- PAPER → LIVE
- PAPER → RESEARCH
- LIVE → RETIRED
- * → RETIRED (emergency)

**Response:**
```json
{
  "success": true,
  "strategy_id": "ORB_NIFTY_5MIN",
  "previous_state": "RESEARCH",
  "new_state": "PAPER",
  "message": "Strategy transitioned successfully"
}
```

---

## Screener APIs

### GET /api/screener/scan?view={view}&threshold={threshold}
Scan market for trading opportunities.

**Parameters:**
- `view` (string) - "intraday", "positional", "financial"
- `threshold` (float) - Minimum score threshold (0.0-1.0)

**Response:**
```json
[
  {
    "symbol": "TCS",
    "name": "Tata Consultancy Services",
    "ltp": 3650.25,
    "change_pct": 2.15,
    "volume": 2500000,
    "score": 0.85,
    "signals": ["MOMENTUM_BUY", "VOLUME_SPIKE"]
  }
]
```

---

## Error Responses

All endpoints return standard error format:

**404 Not Found:**
```json
{
  "detail": "Resource not found"
}
```

**400 Bad Request:**
```json
{
  "detail": "Invalid parameter: symbol"
}
```

**503 Service Unavailable:**
```json
{
  "detail": "Fyers API unavailable"
}
```

**500 Internal Server Error:**
```json
{
  "detail": "Internal server error",
  "trace": "..."
}
```

---

## Rate Limits

- **System Health:** No limit
- **Market Data:** 100 requests/minute
- **Backtest Runs:** 10 requests/minute
- **Lifecycle Transitions:** 5 requests/minute

---

## Authentication

Currently, no authentication required for local development.

For production deployment, add Bearer token:
```
Authorization: Bearer <your_token_here>
```

---

## Changelog

### v2.0.0 (2025-12-22)
- ✅ Added System Hardening v3.0
- ✅ Dual backtest modes (Analyst + Quant)
- ✅ Strategy lifecycle management
- ✅ Paper trading infrastructure
- ✅ Quant research APIs

### v1.0.0 (2024-XX-XX)
- Initial release
