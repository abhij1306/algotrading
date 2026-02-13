# Symbol Format Rules

## Canonical Formats

| Format | Example | Usage |
|--------|---------|-------|
| DB_FORMAT | `SBIN` | PostgreSQL storage, parquet files |
| FYERS_FORMAT | `NSE:SBIN-EQ` | Fyers API, WebSocket subscriptions |
| DISPLAY_FORMAT | `SBIN` | Frontend display (same as DB) |

## Conversion Rules

1. **Database Storage** (PostgreSQL, Parquet)
   - Always use DB_FORMAT
   - `companies.symbol VARCHAR(20)` stores: `SBIN`
   - Parquet files store: `symbol` column as `SBIN`

2. **Fyers API Communication**
   - Always use FYERS_FORMAT
   - WebSocket subscriptions: `NSE:SBIN-EQ`
   - API quote requests: `NSE:SBIN-EQ`
   - Historical data: `NSE:SBIN-EQ`

3. **Frontend Display**
   - Use DISPLAY_FORMAT (DB_FORMAT)
   - Screener table shows: `SBIN`
   - Search input accepts: `SBIN`
   - WebSocket internally converts to FYERS_FORMAT

4. **Internal Processing**
   - Use DB_FORMAT for all internal logic
   - Convert to FYERS_FORMAT only at API boundary

## Validation Rules

- All symbols must be uppercase
- Tickers are alphanumeric, max 20 characters
- Exchange defaults to NSE
- Series defaults to EQ (equity) or INDEX (indices)

## Migration Plan

1. Update database to use DB_FORMAT (if not already)
2. Update parquet files to use DB_FORMAT
3. Add symbol_master service
4. Update all API layers to convert at boundaries
5. Update WebSocket service to use symbol_master
6. Update frontend to display DB_FORMAT but subscribe with FYERS_FORMAT

## NEVER DO THIS

❌ Ad-hoc conversions like:
```python
fyers_symbol = f"NSE:{symbol}-EQ"  # BAD
ticker = symbol.split(':')[1]       # BAD
```

✅ Always use symbol_master:
```python
from app.services.symbol_master import symbol_master
fyers_symbol = symbol_master.to_fyers(symbol)  # GOOD
```
