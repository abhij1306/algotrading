# Quant Platform Module

## Overview
Multi-strategy portfolio backtesting and optimization platform.

## Purpose
- Portfolio construction with multi-strategy allocation
- Risk governance and policy enforcement
- Strategy library management
- Real-time monitoring of live portfolios

## APIs Exposed

### POST `/api/quant/backtest`
Run portfolio backtest with multiple strategies.

### GET `/api/quant/strategies`
List available strategies in library.

### POST `/api/quant/portfolios`
Create new portfolio configuration.

### GET `/api/quant/governance/policies`
Get risk governance policies.

### GET `/api/quant/monitoring/live`
Real-time portfolio monitoring.

## Dependencies
- Market Data Module
- Screener Module (for stock selection)
- Risk Management Module

## Internal Structure
```
quant/
├── engines/
│   ├── portfolio_backtest_core.py
│   ├── portfolio_constructor.py
│   ├── allocator_explainer.py
│   ├── metrics_calculator.py
│   ├── strategy_executor.py
│   └── strategies/
│       ├── index_mean_reversion.py
│       ├── intraday_momentum.py
│       └── ... (6 strategies)
├── routers/
│   ├── portfolio_backtest.py
│   ├── governance.py
│   └── monitoring.py
└── models/
    ├── portfolio.py
    ├── strategy.py
    └── policy.py
```

## Owner
- TBD
- Last Updated: 2025-12-25
