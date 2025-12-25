# Analyst Module

## Overview
Single-stock analysis and hypothesis testing platform.

## Purpose
- Portfolio risk analysis
- Individual stock backtesting
- Risk metrics calculation
- Position sizing recommendations

## APIs Exposed

### POST `/api/analyst/backtest`
Run single-stock backtest for strategy validation.

### POST `/api/analyst/risk-analysis`
Analyze portfolio risk metrics.

## Dependencies
- Market Data Module
- Historical Data Module (read-only)

## Internal Structure
```
analyst/
├── engines/
│   └── analyst_wrapper.py  # Backtest wrapper
├── services/
│   └── portfolio_risk.py   # Risk calculations
└── routers/
    └── analyst_routes.py
```

## Owner
- TBD
- Last Updated: 2025-12-25
