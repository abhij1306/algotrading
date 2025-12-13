# PostgreSQL Database Library for AlgoTrading

A comprehensive PostgreSQL library for managing trading data including stock prices, orders, positions, and portfolio holdings.

## 📋 Features

- ✅ Connection pooling for efficient database access
- ✅ Complete schema for trading data
  - Stock prices (OHLCV)
  - Orders tracking
  - Positions management
  - Portfolio holdings
  - Trading strategies
  - Trade logs
- ✅ CRUD operations for all tables
- ✅ Bulk insert with conflict handling
- ✅ Index optimization for performance

## 🗄️ Database Schema

### Tables

| Table | Description |
|-------|-------------|
| `stock_prices` | Historical OHLCV data for stocks |
| `orders` | Order book tracking |
| `positions` | Daily positions snapshot |
| `holdings` | Portfolio holdings |
| `strategies` | Trading strategy definitions |
| `trade_logs` | Execution logs |

## 🚀 Installation

1. **Install PostgreSQL** (if not already installed)
   - Download from https://www.postgresql.org/download/

2. **Install Python dependencies**
   ```bash
   pip install psycopg2-binary python-dotenv
   ```

3. **Set up database**
   ```sql
   CREATE DATABASE algotrading;
   ```

4. **Configure connection**
   - Copy `db_config.example` to `.env`
   - Update with your PostgreSQL credentials

## 📖 Usage

```python
from trading_db import TradingDatabase
from datetime import date

# Initialize database
db = TradingDatabase()

# Create schema (first time only)
db.initialize_schema()

# Insert stock prices
prices_data = [{
    'symbol': 'RELIANCE',
    'date': date(2024, 12, 10),
    'open': 1260.00,
    'high': 1275.50,
    'low': 1255.00,
    'close': 1270.75,
    'volume': 5500000
}]
db.insert_stock_prices(prices_data)

# Retrieve stock prices
prices = db.get_stock_prices('RELIANCE', start_date='2024-12-01', limit=30)

# Insert order
order_data = {
    'order_id': 'ORD12345',
    'symbol': 'RELIANCE',
    'exchange': 'NSE',
    'order_type': 'LIMIT',
    'transaction_type': 'BUY',
    'quantity': 10,
    'price': 1260.00,
    'status': 'COMPLETE',
    'order_timestamp': datetime.now()
}
db.insert_order(order_data)

# Get orders
orders = db.get_orders(symbol='RELIANCE', status='COMPLETE')

# Close connections
db.close()
```

## 🔧 Main Methods

### Stock Prices
- `insert_stock_prices(prices_data)` - Bulk insert OHLCV data
- `get_stock_prices(symbol, start_date, end_date, limit)` - Query prices

### Orders
- `insert_order(order_data)` - Insert/update order
- `get_orders(symbol, status)` - Query orders

### Positions
- `upsert_position(position_data)` - Insert/update position
- `get_positions(date)` - Get positions for date

### Utilities
- `initialize_schema()` - Create all tables
- `execute_raw_query(query, params)` - Execute custom SQL
- `close()` - Close connection pool

## 📁 Files

| File | Description |
|------|-------------|
| `trading_db.py` | Main database library |
| `example_usage.py` | Usage examples |
| `db_config.example` | Configuration template |

## 🔐 Environment Variables

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=algotrading
DB_USER=postgres
DB_PASSWORD=your_password
```

## ⚡ Performance Tips

- Connection pooling is enabled (1-20 connections)
- Indexes on frequently queried columns
- Bulk inserts for stock prices
- ON CONFLICT handling for upserts

## 🎯 Integration

This library integrates with:
- **Kotak Neo API** - Store order/position data from live trading
- **jugaad-data** - Persist historical stock prices
- **AlgoTrading Backend** - Central data repository

## Run Example

```bash
cd c:\AlgoTrading\database
python example_usage.py
```

## 📝 Notes

- Ensure PostgreSQL is running before use
- Run `initialize_schema()` once to create tables
- Use environment variables for credentials (never commit passwords)
