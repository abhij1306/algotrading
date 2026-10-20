# WebSocket Reference

**Status:** Canonical
**Last updated:** 2026-02-19

## Endpoints
- `POST /api/websocket/connect`
- `POST /api/websocket/subscribe`
- `POST /api/websocket/disconnect`
- `GET /api/websocket/status`
- `WS /api/websocket/stream`

## Current Runtime Flow
1. Client connects to `WS /api/websocket/stream`
2. `ws_manager.connect()` accepts + handshake (`connection_established`)
3. Client sends `subscribe`/`unsubscribe`
4. Router normalizes symbols to DB format via `symbol_master.to_db`
5. Router computes aggregate provider deltas using union-of-subscriptions
6. `LiveMarketService` subscribes/unsubscribes provider with Fyers symbols
7. Incoming provider ticks are normalized, buffered for up to 200ms, and flushed as `ticker_batch`
8. `ws_manager.broadcast()` filters by each client's subscription set and disconnects slow clients on queue overflow

## Message Contract
### Client -> Server
- Subscribe:
```json
{"action":"subscribe","symbols":["SBIN","NSE:RELIANCE-EQ"]}
```
Note: subscribe symbols can be short (`"SBIN"`) or exchange-prefixed (`"NSE:RELIANCE-EQ"`); both are normalized to DB format in backend via `symbol_master.to_db`.
- Unsubscribe:
```json
{"action":"unsubscribe","symbols":["SBIN"]}
```
- Heartbeat: `ping` or `{"action":"ping"}`

### Server -> Client
- Handshake:
```json
{"type":"connection_established","message":"Connected to SmartTrader WebSocket Stream"}
```
- Ack:
```json
{"type":"ack","action":"subscribe","count":2,"provider_delta":1}
```
- Tick:
```json
{"type":"ticker","data":{"symbol":"SBIN","ltp":...,"change":...,"change_pct":...,"volume":...}}
```
- Tick batch:
```json
{"type":"ticker_batch","data":[{"symbol":"SBIN","ltp":...},{"symbol":"RELIANCE","ltp":...}]}
```
`ticker_batch` contains multiple tick payloads in one frame; `ticker` contains exactly one payload.
- Pong:
```json
{"type":"pong"}
```
- Error:
```json
{"type":"error","message":"Invalid symbol: XYZ","code":"INVALID_SYMBOL","retry_after":null}
```

### Error codes
- `INVALID_SYMBOL`: symbol rejected during subscribe/unsubscribe normalization/validation.
- `MALFORMED_REQUEST`: missing or invalid `action`/`symbols` shape.
- `RATE_LIMIT_EXCEEDED`: request burst throttled; `retry_after` may be provided (seconds).

When `subscribe`/`unsubscribe` requests fail, server returns an `error` message instead of `ack`.
Malformed `ping`/`pong` and invalid control frames also use the same `error` envelope.

## Market-Hours Behavior
Implemented in `backend/app/services/live_market_service.py`:
- IST trading window: 09:15 to 15:30, weekdays only
- Off-hours: skip provider connect and cleanup dropped sessions
- `DEV_MODE=true`: forces market-open behavior for development
- Current startup defaults set `DEV_MODE=false`

## Connection and Stability Rules
- Router uses `_safe_send()` to avoid disconnect exceptions leaking
- `ws_manager.connect()` is handshake-safe and returns `False` if client disappears early
- `ws_manager.broadcast()` removes dead sockets on `WebSocketDisconnect`/`RuntimeError`
- Live service queues pending subscriptions when provider WS is not yet connected
- Provider reconnect uses exponential backoff in thread runner (`max_retries=10`)
- `GET /api/websocket/status` is the operational health view for local runtime checks:
  - provider connection state
  - pending subscriptions
  - websocket client/subscription counts
  - queue pressure and overflow counters
  - broadcast and provider timing metrics

## Frontend Hook Integration
`frontend/hooks/useWebSocket.ts` behavior:
- URL resolution:
  - `VITE_WS_URL`, else derived from `VITE_API_URL`, else same origin `/api/websocket/stream` (proxied to the backend by Vite)
- Heartbeat every 30s
- Reconnect backoff with `max_retries=10`
- `registerCallback()` for low-rerender high-frequency consumption
- Supports `ticker` and `ticker_batch` messages (batch currently optional)

## Terminal Usage Pattern
- Terminal subscribes to the selected symbol, watchlist symbols, and open-position symbols.
- Tick updates are used for:
  - live watchlist LTP/change refresh
  - position PnL refresh in bottom panels
  - lightweight chart last-candle updates
  - options board contract LTP refresh between polling intervals
- Depth/chain/orderflow remain polling-driven; websocket is incremental refresh, not authoritative snapshot source.

## Runtime Metrics
The current local implementation keeps lightweight in-memory runtime metrics only.

Current metrics focus:
- websocket connections accepted/disconnected
- active client and subscription counts
- queue depth and overflow counts
- batch sizes and enqueued message counts
- provider connect/reconnect attempts
- flush/broadcast timings

These metrics are intentionally lightweight for a single-user local app. They are meant
to improve debugging and future development, not to introduce a full telemetry stack.
