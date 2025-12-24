# WebSocket Integration Technical Documentation

## Overview
The AlgoTrading platform implements a real-time data streaming architecture using WebSockets to deliver live market data to the frontend Screener component. This document describes the complete architecture, protocols, and implementation details.

## Architecture Components

### 1. LiveMarketService (Central Orchestrator)
**Location**: `backend/app/services/live_market_service.py`

**Responsibilities**:
- Market hours enforcement (09:15-15:30 IST)
- Fyers WebSocket lifecycle management
- Tick data throttling (1-second intervals)
- Thread-safe message routing

**Key Features**:
- `DEV_MODE` environment variable override for testing
- Atomic buffer swapping to prevent data loss
- Async executor pattern for blocking SDK calls

### 2. FyersWebSocketService (Data Provider)
**Location**: `backend/app/services/fyers_websocket.py`

**Responsibilities**:
- Fyers API authentication
- Raw tick data reception
- Message routing via handler pattern

**Key Features**:
- Pathlib-based token resolution
- Decoupled from broadcasting logic
- Thread-safe callback execution

### 3. ConnectionManager (Client Pool)
**Location**: `backend/app/utils/ws_manager.py`

**Responsibilities**:
- Frontend WebSocket connections management
- Thread-safe broadcasting
- Connection lifecycle handling

**Key Features**:
- `broadcast_threadsafe()` for cross-thread safety
- Event loop injection via `set_loop()`

### 4. WebSocket Endpoint
**Location**: `backend/app/routers/websocket.py`

**Protocol**: JSON-based bidirectional messaging

**Client → Server Actions**:
```json
{"action": "subscribe", "symbols": ["NSE:SBIN-EQ"]}
{"action": "unsubscribe", "symbols": ["NSE:RELIANCE-EQ"]}
"ping"
```

**Server → Client Responses**:
```json
{"type": "ack", "action": "subscribe", "count": 2}
{"type": "ticker", "data": {"symbol": "NSE:SBIN-EQ", "ltp": 500.5, "ch": 1.2}}
{"type": "pong"}
```

## Data Flow

### Startup Sequence
1. **Lifespan Handler** (`main.py`)
   - Captures asyncio event loop
   - Injects loop into `ConnectionManager`
   - Initializes `LiveMarketService`

2. **Market Hours Check**
   - If market open: Connect to Fyers (threaded)
   - If market closed: Skip connection, serve static data

3. **Connection Establishment**
   - Fyers connection runs in daemon thread
   - Message handler registered with `LiveMarketService`
   - Flush loop started for tick throttling

### Runtime Data Flow
```
Fyers API → FyersWebSocketService → LiveMarketService.handle_tick_incoming() 
    → asyncio.run_coroutine_threadsafe() → _update_buffer() 
    → tick_buffer (dict) → _flush_loop() (1s interval) 
    → ConnectionManager.broadcast() → Frontend Clients
```

### Subscription Flow
1. Frontend sends `{"action": "subscribe", "symbols": [...]}`
2. WebSocket endpoint receives message
3. Converts to Fyers format (e.g., `NSE:SBIN-EQ`)
4. Calls `await live_market.subscribe(symbols)` (executor pattern)
5. Executor runs `ws_service.subscribe()` in thread pool
6. ACK sent to client: `{"type": "ack", ...}`

## Critical Design Patterns

### 1. Async Executor for Blocking SDK
**Problem**: Fyers SDK is synchronous, blocks event loop  
**Solution**: 
```python
await asyncio.get_running_loop().run_in_executor(
    None, 
    self.ws_service.subscribe, 
    symbols
)
```

### 2. Atomic Buffer Swap
**Problem**: Race condition between buffer read/clear  
**Solution**:
```python
batch = self.tick_buffer  # Atomic reference swap
self.tick_buffer = {}
```

### 3. Thread-Safe Broadcasting
**Problem**: Fyers runs in separate thread, asyncio in main loop  
**Solution**:
```python
def handle_tick_incoming(self, tick):
    asyncio.run_coroutine_threadsafe(
        self._update_buffer(tick), 
        self.loop
    )
```

## Configuration

### Environment Variables
- `DEV_MODE=True`: Override market hours check for testing
- `FYERS_APP_ID`: Fyers application ID
- `FYERS_SECRET_ID`: Fyers secret key
- `FYERS_TOKEN_FILE`: Path to access token JSON

### Market Hours
- **Open**: 09:15 IST
- **Close**: 15:30 IST
- **Weekends**: Always closed

## Error Handling

### Connection Failures
- Fyers connection errors logged, system remains operational
- REST API continues serving static DB data
- Frontend receives "DEGRADED" status

### Subscription Errors
- 429 Rate Limit: Logged, subscription skipped
- Token Expired: Requires re-authentication
- Connection Lost: Auto-reconnect during market hours

## Frontend Integration

### Hook Usage
```typescript
const { isConnected, lastMessage } = useWebSocket();

// Subscribe to symbols
websocket.send(JSON.stringify({
  action: 'subscribe',
  symbols: ['NSE:SBIN-EQ', 'NSE:RELIANCE-EQ']
}));

// Handle updates
useEffect(() => {
  if (lastMessage?.type === 'ticker') {
    // Update UI with lastMessage.data
  }
}, [lastMessage]);
```

## Testing

### Protocol Verification
**File**: `test_ws_v2.py` (deleted after verification)

**Test Flow**:
1. Connect to `ws://localhost:8000/api/websocket/stream`
2. Send ping, verify pong
3. Subscribe to symbols
4. Verify ACK reception
5. Listen for tick data

### Health Checks
- `/api/system/health`: Overall system status
- `/api/websocket/status`: WebSocket connection status

## Performance Optimizations

### Throttling
- Ticks buffered for 1 second
- Only latest tick per symbol broadcast
- Prevents frontend saturation during high volatility

### Pagination Sync
- Frontend unsubscribes from previous page symbols
- Subscribes only to visible rows
- Minimizes bandwidth and processing

## Troubleshooting

### No Tick Data
1. Check market hours (09:15-15:30 IST)
2. Verify Fyers token valid: `curl http://localhost:8000/api/system/health`
3. Check logs for connection errors
4. Set `DEV_MODE=True` for testing

### Subscription Hangs
1. Verify executor pattern in `live_market_service.py`
2. Check asyncio loop initialized in lifespan
3. Confirm `await` used for subscribe calls

### Path Errors
1. Verify token file exists: `fyers/config/access_token.json`
2. Check pathlib resolution from current directory
3. Ensure fallback path checked

## Version History
- **v1.0**: Initial REST-only implementation
- **v2.0**: WebSocket push model with market hours enforcement
- **v2.1**: Executor pattern, atomic buffer, lifespan handler (current)
