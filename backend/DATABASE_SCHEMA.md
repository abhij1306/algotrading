# Database Schema Documentation

## Overview
SQLite database with SQLAlchemy ORM for persistent data storage.
Designed to scale from 200 F&O stocks to full NSE universe (1600+ stocks).
Can migrate to PostgreSQL for production without code changes.

## Database Location
`backend/data/screener.db`

## Tables

### 1. companies
Master table for all companies

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| symbol | VARCHAR(20) | Stock symbol (unique, indexed) |
| name | VARCHAR(200) | Company name |
| sector | VARCHAR(100) | Sector classification |
| industry | VARCHAR(100) | Industry classification |
| market_cap | FLOAT | Market capitalization |
| is_fno | BOOLEAN | F&O eligible flag |
| is_active | BOOLEAN | Active trading flag |
| created_at | DATETIME | Record creation time |
| updated_at | DATETIME | Last update time |

**Indexes**: symbol (unique)

---

### 2. historical_prices
Daily OHLCV data

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| company_id | INTEGER | Foreign key to companies |
| date | DATE | Trading date (indexed) |
| open | FLOAT | Opening price |
| high | FLOAT | Highest price |
| low | FLOAT | Lowest price |
| close | FLOAT | Closing price |
| volume | INTEGER | Trading volume |
| adj_close | FLOAT | Adjusted close (for splits/dividends) |
| trades | INTEGER | Number of trades |
| source | VARCHAR(20) | Data source (fyers/yfinance) |
| created_at | DATETIME | Record creation time |

**Indexes**: 
- company_id, date (composite, unique)
- date

**Storage Estimate**: 
- 200 stocks × 252 days/year × 5 years = ~250K rows
- ~50MB for 5 years of data

---

### 3. financial_statements
Annual and quarterly financial data

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| company_id | INTEGER | Foreign key to companies |
| period_end | DATE | Period ending date |
| period_type | VARCHAR(10) | 'annual' or 'quarterly' |
| fiscal_year | INTEGER | Fiscal year |
| quarter | INTEGER | Quarter (1-4, null for annual) |
| **Income Statement** | | |
| revenue | FLOAT | Total revenue |
| operating_income | FLOAT | Operating income |
| net_income | FLOAT | Net income |
| ebitda | FLOAT | EBITDA |
| eps | FLOAT | Earnings per share |
| **Balance Sheet** | | |
| total_assets | FLOAT | Total assets |
| total_liabilities | FLOAT | Total liabilities |
| shareholders_equity | FLOAT | Shareholders' equity |
| total_debt | FLOAT | Total debt |
| cash_and_equivalents | FLOAT | Cash and equivalents |
| **Cash Flow** | | |
| operating_cash_flow | FLOAT | Operating cash flow |
| investing_cash_flow | FLOAT | Investing cash flow |
| financing_cash_flow | FLOAT | Financing cash flow |
| free_cash_flow | FLOAT | Free cash flow |
| **Ratios** | | |
| pe_ratio | FLOAT | Price-to-earnings ratio |
| pb_ratio | FLOAT | Price-to-book ratio |
| debt_to_equity | FLOAT | Debt-to-equity ratio |
| roe | FLOAT | Return on equity |
| roa | FLOAT | Return on assets |
| **Metadata** | | |
| source | VARCHAR(50) | Data source |
| created_at | DATETIME | Record creation time |
| updated_at | DATETIME | Last update time |

**Indexes**: 
- company_id, period_end, period_type (composite, unique)
- period_end

**Storage Estimate**:
- 200 stocks × 20 quarters = ~4K rows
- ~2MB for 5 years of quarterly data

---

### 4. quarterly_results
Quarterly earnings announcements

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| company_id | INTEGER | Foreign key to companies |
| quarter_end | DATE | Quarter ending date |
| fiscal_year | INTEGER | Fiscal year |
| quarter | INTEGER | Quarter (1-4) |
| revenue | FLOAT | Quarterly revenue |
| net_profit | FLOAT | Net profit |
| eps | FLOAT | Earnings per share |
| revenue_growth_yoy | FLOAT | YoY revenue growth % |
| profit_growth_yoy | FLOAT | YoY profit growth % |
| announcement_date | DATE | Result announcement date |
| result_type | VARCHAR(20) | audited/unaudited/preliminary |
| additional_data | TEXT | JSON string for extra data |
| source | VARCHAR(50) | Data source |
| created_at | DATETIME | Record creation time |
| updated_at | DATETIME | Last update time |

**Indexes**: 
- company_id, quarter_end (composite, unique)

---

### 5. data_update_logs
Track data updates for monitoring

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER | Primary key |
| company_id | INTEGER | Foreign key to companies |
| data_type | VARCHAR(50) | historical/financial/quarterly |
| last_update | DATETIME | Update timestamp |
| records_updated | INTEGER | Number of records updated |
| status | VARCHAR(20) | success/failed/partial |
| error_message | TEXT | Error details if failed |
| start_date | DATE | Start date for historical data |
| end_date | DATE | End date for historical data |
| created_at | DATETIME | Log creation time |

**Indexes**: company_id, data_type

---

## Usage Examples

### Save Historical Data
```python
from database import SessionLocal
from data_repository import DataRepository

db = SessionLocal()
repo = DataRepository(db)

# Save DataFrame to database
repo.save_historical_prices('RELIANCE', df, source='fyers')

db.close()
```

### Retrieve Historical Data
```python
# Get last 365 days
df = repo.get_historical_prices('RELIANCE', days=365)

# Get specific date range
df = repo.get_historical_prices(
    'RELIANCE', 
    start_date=date(2024, 1, 1),
    end_date=date(2024, 12, 31)
)
```

### Save Financial Statement
```python
data = {
    'period_end': date(2024, 3, 31),
    'fiscal_year': 2024,
    'quarter': 4,
    'revenue': 50000000000,
    'net_income': 5000000000,
    'eps': 25.50,
    # ... other fields
}
repo.save_financial_statement('RELIANCE', data, period_type='quarterly')
```

### Check Last Update
```python
last_update = repo.get_last_update('RELIANCE', 'historical')
if last_update:
    print(f"Last updated: {last_update.last_update}")
    print(f"Records: {last_update.records_updated}")
```

---

## Migration Path

### Current: SQLite
- Perfect for development and small-scale production
- Single file database
- No server required
- Handles 1M+ rows easily

### Future: PostgreSQL
To migrate to PostgreSQL:
1. Change DATABASE_URL in `database.py`:
   ```python
   DATABASE_URL = "postgresql://user:pass@localhost/screener"
   ```
2. Install psycopg2: `pip install psycopg2-binary`
3. Run migrations (SQLAlchemy handles schema)
4. No code changes needed!

---

## Maintenance

### Backup
```bash
# Backup database
cp backend/data/screener.db backend/data/screener_backup_$(date +%Y%m%d).db
```

### Vacuum (Optimize)
```python
from database import engine
engine.execute("VACUUM")
```

### Check Size
```bash
ls -lh backend/data/screener.db
```

---

## Performance Considerations

### Indexes
All critical columns are indexed for fast queries:
- Symbol lookups: O(log n)
- Date range queries: O(log n)
- Company-specific queries: O(log n)

### Query Optimization
- Use `days` parameter instead of full date range
- Limit results with `limit` parameter
- Use specific queries instead of loading all data

### Scaling
- Current design handles 1600 stocks × 5 years = 2M rows
- Expected database size: ~500MB for full NSE universe
- Query performance: <100ms for most queries

---

## Future Enhancements

1. **Partitioning**: Partition historical_prices by year
2. **Compression**: Enable SQLite compression
3. **Replication**: Master-slave setup for reads
4. **Caching**: Redis layer for hot data
5. **Analytics**: Materialized views for common queries
