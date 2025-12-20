# Fyers API v3 - Complete Reference Documentation

**Last Updated**: December 20, 2025  
**API Version**: v3  
**Python SDK**: fyers-apiv3 (v3.1.7)

---

## Table of Contents

1. [Introduction](#introduction)
2. [Authentication & Login](#authentication--login)
3. [User Information](#user-information)
4. [Transaction Info](#transaction-info)
5. [Order Placement](#order-placement)
6. [Market Data](#market-data)
7. [WebSocket API](#websocket-api)
8. [Rate Limits & Best Practices](#rate-limits--best-practices)

---

## Introduction

Fyers API is a set of REST-like APIs that provide integration with Fyers trading platform. All API requests are made over HTTPS protocol.

### Libraries and SDKs

- **Python**: `fyers-apiv3` (Supports Python 3.8 to 3.12)
- **Node.js**: Fyers Node.js library (Supports Node.js 12 to 21.6.2)
- **Web JS**: Available via CDN
- **C#**: Supports .NET 8.0.4
- **Java**: Supports Java 8

### Base URLs

- **Production**: `https://api-t1.fyers.in`
- **Data API**: `https://api-t1.fyers.in/data`

### Authorization Header

All requests (except authentication) require:
```
Authorization: app_id:access_token
```

Example:
```
Authorization: XC4XXXM-100:eyJ0eXAiOiJKV1QiLCJhbGc...
```

---

## Authentication & Login

### Step 1: Generate Auth Code

**Endpoint**: `GET /api/v3/generate-authcode`

**Parameters**:
- `client_id` (string): Your app_id (e.g., "qwerty-100")
- `redirect_uri` (string): Redirect URL (must match app creation)
- `response_type` (string): Always "code"
- `state` (string): Random value for verification

**Example**:
```bash
https://api-t1.fyers.in/api/v3/generate-authcode?client_id=SPXXXXE7-100&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=sample_state
```

### Step 2: Validate Auth Code

**Endpoint**: `POST /api/v3/validate-authcode`

**Request Body**:
```json
{
  "grant_type": "authorization_code",
  "appIdHash": "c3efb1075ef2332b3a4ec7d44b0f05c1...",
  "code": "eyJ0eXAi***.eyJpc3MiOiJh***.r_65Awa1kGds***"
}
```

**Response**:
```json
{
  "s": "ok",
  "code": 200,
  "message": "",
  "access_token": "eyJ0eXAiOi***.eyJpc3MiOiJh***.HrSubihiFKXOpUOj_7***",
  "refresh_token": "eyJ0eXAiO***.eyJpc3MiOiJh***.67mXADDLrrleuEH_EE***"
}
```

### Refresh Token

**Endpoint**: `POST /api/v3/validate-refresh-token`

**Request Body**:
```json
{
  "grant_type": "refresh_token",
  "appIdHash": "c3efb1075ef2332b3a4ec7d44b0f05c1...",
  "refresh_token": "eyJ0eXAiOiJKV1***.eyJpc3MiOiJhcGkuZn***.5_Qpnd1nQXBw1T_wNJNFF***",
  "pin": "****"
}
```

**Validity**: Refresh token is valid for 15 days.

---

## User Information

### Profile

**Endpoint**: `GET /api/v3/profile`

**Response**:
```json
{
  "s": "ok",
  "code": 200,
  "message": "",
  "data": {
    "name": "XASHXX G H",
    "display_name": "Y2K",
    "email_id": "txxxxxxxxxxx2@gmail.com",
    "PAN": "FYxxxxxx0S",
    "fy_id": "FX0011",
    "pin_change_date": "19-08-2020 14:58:41",
    "mobile_number": "63xxxxxx08",
    "totp": true,
    "pwd_to_expire": 42,
    "ddpi_enabled": false,
    "mtf_enabled": false
  }
}
```

### Funds

**Endpoint**: `GET /api/v3/funds`

**Response**:
```json
{
  "code": 200,
  "message": "",
  "s": "ok",
  "fund_limit": [
    {
      "id": 1,
      "title": "Total Balance",
      "equityAmount": 58.15,
      "commodityAmount": 0
    },
    {
      "id": 10,
      "title": "Available Balance",
      "equityAmount": 58.15,
      "commodityAmount": 0
    }
  ]
}
```

### Holdings

**Endpoint**: `GET /api/v3/holdings`

**Response Fields**:
- `symbol`: Symbol in Fyers format (e.g., "NSE:SBIN-EQ")
- `holdingType`: "HLD" (Holding)
- `quantity`: Total quantity
- `costPrice`: Average buy price
- `marketVal`: Current market value
- `pl`: Profit/Loss
- `ltp`: Last traded price
- `fyToken`: Unique identifier
- `isin`: ISIN code

### Logout

**Endpoint**: `POST /api/v3/logout`

**Response**:
```json
{
  "s": "ok",
  "code": 200,
  "message": "you are successfully logged out"
}
```

---

## Transaction Info

### Orders

**Endpoint**: `GET /api/v3/orders`

**Query Parameters** (Optional):
- `id`: Filter by specific order ID
- `order_tag`: Filter by order tag

**Response Fields**:
- `id`: Order ID
- `exchOrdId`: Exchange order ID
- `symbol`: Symbol (e.g., "NSE:SBIN-EQ")
- `qty`: Original quantity
- `filledQty`: Filled quantity
- `remainingQuantity`: Remaining quantity
- `status`: Order status
  - 1 = Canceled
  - 2 = Traded/Filled
  - 4 = Transit
  - 5 = Rejected
  - 6 = Pending
  - 7 = Expired
- `type`: Order type (1=Limit, 2=Market, 3=SL-M, 4=SL-L)
- `side`: 1=Buy, -1=Sell
- `productType`: CNC, INTRADAY, MARGIN, CO, BO, MTF
- `limitPrice`: Limit price
- `stopPrice`: Stop price
- `tradedPrice`: Average traded price
- `orderDateTime`: Order timestamp
- `orderTag`: Custom order tag

### Positions

**Endpoint**: `GET /api/v3/positions`

**Response Fields**:
- `symbol`: Symbol
- `netQty`: Net quantity
- `buyQty`: Total buy quantity
- `sellQty`: Total sell quantity
- `buyAvg`: Average buy price
- `sellAvg`: Average sell price
- `netAvg`: Net average price
- `pl`: Total P&L
- `realized_profit`: Realized P&L
- `unrealized_profit`: Unrealized P&L
- `ltp`: Last traded price
- `side`: 1=Long, -1=Short
- `productType`: Product type

### Trades

**Endpoint**: `GET /api/v3/tradebook`

**Query Parameters** (Optional):
- `order_tag`: Filter by order tag

**Response Fields**:
- `orderNumber`: Order ID
- `tradeNumber`: Trade ID
- `symbol`: Symbol
- `tradedQty`: Traded quantity
- `tradePrice`: Trade price
- `tradeValue`: Trade value
- `orderDateTime`: Trade timestamp
- `side`: 1=Buy, -1=Sell

---

## Order Placement

### Order Types

1. **Limit Order** (`type: 1`): Buy/sell at specific price or better
2. **Market Order** (`type: 2`): Buy/sell at current market price
3. **Stop Order / SL-M** (`type: 3`): Market order triggered at stop price
4. **Stop Limit Order / SL-L** (`type: 4`): Limit order triggered at stop price

### Product Types

- **CNC**: Cash and Carry (Equity only)
- **INTRADAY**: Intraday trading (All segments)
- **MARGIN**: Margin trading (Derivatives)
- **CO**: Cover Order (requires stopLoss)
- **BO**: Bracket Order (requires stopLoss and takeProfit)
- **MTF**: Margin Trading Facility (Approved symbols only)

### Single Order

**Endpoint**: `POST /api/v3/orders/sync`

**Request Body**:
```json
{
  "symbol": "NSE:SBIN-EQ",
  "qty": 1,
  "type": 2,
  "side": 1,
  "productType": "INTRADAY",
  "limitPrice": 0,
  "stopPrice": 0,
  "validity": "DAY",
  "disclosedQty": 0,
  "offlineOrder": false,
  "stopLoss": 0,
  "takeProfit": 0,
  "orderTag": "tag1",
  "isSliceOrder": false
}
```

**Response**:
```json
{
  "s": "ok",
  "code": 1101,
  "message": "Order submitted successfully. Your Order Ref. No.808058117761",
  "id": "808058117761"
}
```

### Multi Order (Basket Order)

**Endpoint**: `POST /api/v3/multi-order/sync`

**Request Body**: Array of order objects (max 10)

### Modify Order

**Endpoint**: `PATCH /api/v3/orders/sync`

**Request Body**:
```json
{
  "id": "809229222111",
  "qty": 1,
  "type": 2,
  "limitPrice": 61200
}
```

### Cancel Order

**Endpoint**: `DELETE /api/v3/orders/sync`

**Request Body**:
```json
{
  "id": "52009227353"
}
```

### Exit Position

**Endpoint**: `DELETE /api/v3/positions`

**Exit All Positions**:
```json
{
  "exit_all": 1
}
```

**Exit Specific Position**:
```json
{
  "id": "NSE:SBIN-EQ-INTRADAY"
}
```

**Exit by Segment/Side/ProductType**:
```json
{
  "segment": [10],
  "side": [1, -1],
  "productType": ["INTRADAY", "CNC"]
}
```

### Convert Position

**Endpoint**: `POST /api/v3/positions`

**Request Body**:
```json
{
  "symbol": "NSE:SBIN-EQ-INTRADAY",
  "positionSide": 1,
  "convertQty": 1,
  "convertFrom": "INTRADAY",
  "convertTo": "CNC",
  "overnight": 0
}
```

---

## Market Data

### Historical Data (Candles)

**Endpoint**: `GET /data/history`

**Parameters**:
- `symbol`: Symbol (e.g., "NSE:SBIN-EQ")
- `resolution`: Candle resolution
  - Seconds: "5S", "10S", "15S", "30S", "45S"
  - Minutes: "1", "2", "3", "5", "10", "15", "20", "30", "60", "120", "240"
  - Daily: "D" or "1D"
- `date_format`: 0 (epoch) or 1 (yyyy-mm-dd)
- `range_from`: Start date
- `range_to`: End date
- `cont_flag`: 1 for continuous data
- `oi_flag`: 1 to include Open Interest

**Limits**:
- Up to 100 days for minute resolutions
- Up to 366 days for daily resolution
- 30 trading days for second charts

**Response**:
```json
{
  "s": "ok",
  "candles": [
    [1621814400, 417.0, 419.2, 405.3, 412.05, 142964052],
    [1621900800, 415.1, 415.5, 408.5, 412.35, 56048127]
  ]
}
```

Each candle: `[timestamp, open, high, low, close, volume]`

### Quotes

**Endpoint**: `GET /data/quotes`

**Parameters**:
- `symbols`: Comma-separated symbols (max 50)

**Response**:
```json
{
  "s": "ok",
  "code": 200,
  "d": [{
    "n": "NSE:SBIN-EQ",
    "s": "ok",
    "v": {
      "ch": 1.7,
      "chp": 0.4,
      "lp": 426.9,
      "spread": 0.05,
      "ask": 426.9,
      "bid": 426.85,
      "open_price": 430.5,
      "high_price": 433.65,
      "low_price": 423.6,
      "prev_close_price": 425.2,
      "volume": 38977242,
      "fyToken": "10100000003045"
    }
  }]
}
```

### Market Depth

**Endpoint**: `GET /data/depth`

**Parameters**:
- `symbol`: Symbol
- `ohlcv_flag`: 1 to include OHLCV data

**Response**:
```json
{
  "s": "ok",
  "d": {
    "NSE:SBIN-EQ": {
      "totalbuyqty": 2396063,
      "totalsellqty": 4990001,
      "bids": [
        {"price": 427.25, "volume": 4738, "ord": 5}
      ],
      "ask": [
        {"price": 427.4, "volume": 2193, "ord": 4}
      ],
      "ltp": 427.25,
      "volume": 39163870
    }
  }
}
```

### Option Chain

**Endpoint**: `GET /data/options-chain-v3`

**Parameters**:
- `symbol`: Underlying symbol (e.g., "NSE:NIFTY-INDEX")
- `strikecount`: Number of strikes (max 50)
- `timestamp`: Optional timestamp for historical data

**Response**: Returns ATM, ITM, and OTM strikes with Call/Put data

### Market Status

**Endpoint**: `GET /data/marketStatus`

**Response**:
```json
{
  "code": 200,
  "s": "ok",
  "marketStatus": [
    {
      "exchange": 10,
      "segment": 10,
      "market_type": "NORMAL",
      "status": "OPEN"
    }
  ]
}
```

**Status Values**:
- OPEN
- CLOSED
- PREOPEN
- POSTCLOSE_START
- POSTCLOSE_CLOSED

### Symbol Master

Download symbol master files:

- **NSE Capital Market**: https://public.fyers.in/sym_details/NSE_CM.csv
- **NSE F&O**: https://public.fyers.in/sym_details/NSE_FO.csv
- **NSE Currency**: https://public.fyers.in/sym_details/NSE_CD.csv
- **BSE Capital Market**: https://public.fyers.in/sym_details/BSE_CM.csv
- **MCX Commodity**: https://public.fyers.in/sym_details/MCX_COM.csv

---

## WebSocket API

### Market Data WebSocket

**Python Example**:
```python
from fyers_apiv3.FyersWebsocket import data_ws

def onmessage(message):
    print("Response:", message)

def onopen():
    data_type = "SymbolUpdate"
    symbols = ['NSE:SBIN-EQ', 'NSE:NIFTY50-INDEX']
    fyers.subscribe(symbols=symbols, data_type=data_type)
    fyers.keep_running()

access_token = "app_id:access_token"

fyers = data_ws.FyersDataSocket(
    access_token=access_token,
    log_path="",
    litemode=False,
    write_to_file=False,
    reconnect=True,
    on_connect=onopen,
    on_message=onmessage,
    reconnect_retry=10
)

fyers.connect()
```

**Data Types**:
- `SymbolUpdate`: Real-time price updates
- `DepthUpdate`: Market depth updates

**Subscription Limit**: 5000 symbols

### Order Updates WebSocket

**Python Example**:
```python
from fyers_apiv3.FyersWebsocket import order_ws

def onOrder(message):
    print("Order:", message)

def onopen():
    data_type = "OnOrders"  # or "OnTrades", "OnPositions", "OnGeneral"
    fyers.subscribe(data_type=data_type)
    fyers.keep_running()

fyers = order_ws.FyersOrderSocket(
    access_token=access_token,
    on_connect=onopen,
    on_orders=onOrder
)

fyers.connect()
```

**Data Types**:
- `OnOrders`: Order updates
- `OnTrades`: Trade updates
- `OnPositions`: Position updates
- `OnGeneral`: General updates (alerts, EDIS)

---

## Rate Limits & Best Practices

### Rate Limits

| Timeframe | Limit |
|-----------|-------|
| Per Second | 10 |
| Per Minute | 200 |
| Per Day | 100,000 |

**User Blocking**: User will be blocked for the rest of the day if per-minute rate limit is exceeded more than 3 times.

### Best Practices

1. **Never share** your `app_secret` or `access_token`
2. **Use HTTPS** for all API calls
3. **Implement retry logic** with exponential backoff
4. **Handle errors gracefully** - check response codes
5. **Validate inputs** before making API calls
6. **Use WebSocket** for real-time data instead of polling
7. **Cache symbol master** data locally
8. **Implement proper logging** for debugging
9. **Test in sandbox** before production
10. **Monitor rate limits** to avoid blocking

### Error Codes

| Code | Description |
|------|-------------|
| -8 | Token expired |
| -15 | Invalid token |
| -16 | Unable to authenticate |
| -17 | Token invalid or expired |
| -50 | Invalid parameters |
| -51 | Invalid Order ID |
| -99 | Order rejected |
| -300 | Invalid symbol |
| -429 | Rate limit exceeded |
| 200 | Success |
| 400 | Bad request |
| 401 | Authorization error |
| 500 | Internal server error |

### Exchange & Segment Codes

**Exchange**:
- 10 = NSE
- 11 = MCX
- 12 = BSE

**Segment**:
- 10 = Capital Market (Equity)
- 11 = F&O (Derivatives)
- 12 = Currency
- 20 = Commodity

---

## Python SDK Quick Reference

### Installation

```bash
pip install fyers-apiv3==3.1.7
```

### Initialize FyersModel

```python
from fyers_apiv3 import fyersModel

client_id = "YOUR_APP_ID-100"
access_token = "YOUR_ACCESS_TOKEN"

fyers = fyersModel.FyersModel(
    client_id=client_id,
    token=access_token,
    is_async=False,
    log_path=""
)
```

### Common Operations

```python
# Get Profile
profile = fyers.get_profile()

# Get Funds
funds = fyers.funds()

# Get Quotes
quotes = fyers.quotes({"symbols": "NSE:SBIN-EQ,NSE:NIFTY50-INDEX"})

# Get Historical Data
data = {
    "symbol": "NSE:SBIN-EQ",
    "resolution": "D",
    "date_format": "1",
    "range_from": "2024-01-01",
    "range_to": "2024-12-31",
    "cont_flag": "1"
}
history = fyers.history(data)

# Place Order
order_data = {
    "symbol": "NSE:SBIN-EQ",
    "qty": 1,
    "type": 2,
    "side": 1,
    "productType": "INTRADAY",
    "limitPrice": 0,
    "stopPrice": 0,
    "validity": "DAY",
    "disclosedQty": 0,
    "offlineOrder": False
}
response = fyers.place_order(order_data)
```

---

## Additional Resources

- **API Documentation**: https://myapi.fyers.in/docsv3
- **Community Forum**: https://community.fyers.in
- **Support Email**: api-support@fyers.in
- **GitHub Samples**: https://github.com/fyers-api

---

**Note**: This documentation is based on Fyers API v3. Always refer to the official documentation for the most up-to-date information.
