# Market Data Module - API Reference

## Base Path
`/api/market`

---

## Endpoints

### `GET /api/market/status`
Get current market status (open/closed with timing info).

**Response:**
```json
{
  "is_open": false,
  "message": "Market closed. Opens at 09:15 AM",
  "next_open": "2025-12-26 09:15:00"
}
```

---

### `GET /api/market/overview`
Comprehensive market overview for dashboard.

**Response:**
```json
{
  "indices": [
    {
      "name": "Nifty 50",
      "symbol": "NIFTY",
      "price": 21500,
      "change": 150,
      "change_pct": 0.70,
      "status": "POSITIVE"
    }
  ],
  "sentiment": {
    "us_fear_greed": {"score": 65, "status": "Greed"},
    "india_sentiment": {"score": 57, "status": "Greed"}
  },
  "condition": {
    "status": "Trending",
    "adx": 28
  },
  "timestamp": "2025-12-25T20:00:00"
}
```

---

### `GET /api/market/search`
Search for stocks and indices.

**Query Parameters:**
- `query` (required): Search term (min 2 chars)
- `exclude_indices` (optional): Set `true` to only return equities

**Example:** `/api/market/search?query=RELI&exclude_indices=false`

**Response:**
```json
[
  {
    "symbol": "RELIANCE",
    "name": "Reliance Industries Ltd",
    "sector": "Oil & Gas",
    "type": "EQUITY"
  }
]
```

---

### `GET /api/market/quote/{symbol}`
Get latest quote for a symbol.

**Example:** `/api/market/quote/RELIANCE`

**Response:**
```json
{
  "symbol": "RELIANCE",
  "name": "Reliance Industries Ltd",
  "ltp": 2450.50,
  "change": 25.30,
  "change_pct": 1.04,
  "volume": 12500000,
  "timestamp": "2025-12-25T15:30:00"
}
```

---

### `GET /api/market/indices`
Get list of available indices.

**Response:**
```json
{
  "indices": [
    {"id": "NIFTY50", "name": "NIFTY 50", "symbol": "NIFTY"},
    {"id": "BANKNIFTY", "name": "BANK NIFTY", "symbol": "BANKNIFTY"}
  ],
  "default": "NIFTY50"
}
```

---

### `GET /api/market/sectors`
Get list of all available sectors.

**Response:**
```json
{
  "sectors": [
    "Automobile",
    "Banking",
    "IT",
    "Oil & Gas",
    "Pharmaceuticals"
  ]
}
```

---

### `GET /api/market/quotes/live`
Get live quotes for multiple symbols (market hours only).

**Query Parameters:**
- `symbols` (required): Comma-separated list (e.g., `RELIANCE,TCS,INFY`)

**Example:** `/api/market/quotes/live?symbols=RELIANCE,TCS`

**Response:**
```json
{
  "quotes": {
    "RELIANCE": {"ltp": 2450.50, "change": 1.04},
    "TCS": {"ltp": 3500.75, "change": -0.25}
  }
}
```

**Error (Market Closed):**
```json
{
  "detail": "Live quotes unavailable: Market closed"
}
```

---

## Module Dependencies

### Imports From
- `app.database` - Database session, Company model
- `app.utils.market_hours` - Market timing utilities
- `app.constants.indices` - Index definitions

### Consumed By
- Screener Module
- Analyst Module
- Quant Module
- Trader Module
- Dashboard Frontend

---

## Development Notes

### Testing
```bash
# Start server
cd backend
uvicorn app.main:app --reload

# Test endpoint
curl http://localhost:8000/api/market/status
curl "http://localhost:8000/api/market/search?query=RELI"
```

### Error Handling
- All endpoints return proper HTTP status codes
- 404: Symbol/resource not found
- 500: Internal server error
- 503: Service unavailable (e.g., market closed for live quotes)

### Performance
- Search results limited to 15 items
- Caching implemented for frequently accessed data
- Live quotes only available during market hours
