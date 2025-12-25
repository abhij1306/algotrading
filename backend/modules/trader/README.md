# Trader Module

## Overview
Order execution and broker integration layer.

## Purpose
- Multi-broker support (Fyers, Paper, Backtest)
- Paper trading simulation
- Order placement abstraction
- Position tracking

## APIs Exposed

### POST `/api/trader/orders`
Place new order.

### GET `/api/trader/positions`
Get current positions.

### GET `/api/trader/orders/history`
Order history.

## Dependencies
- Market Data Module
- Portfolio Module

## Internal Structure
```
trader/
├── brokers/
│   ├── base.py           # Base broker interface
│   └── plugins/
│       ├── fyers.py      # Fyers integration
│       ├── paper.py      # Paper trading
│       └── backtest.py   # Backtest broker
├── services/
│   └── paper_trading.py
└── routers/
    └── trading_routes.py
```

## Owner
- TBD
- Last Updated: 2025-12-25
