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
7. Incoming provider ticks are normalized and immediately broadcast as `{"type":"ticker","data":...}`
8. `ws_manager.broadcast()` filters by each client's subscription set

## Message Contract
### Client -> Server
- Subscribe:
```json
{"action":"subscribe","symbols":["SBIN","NSE:RELIANCE-EQ"]}
```
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
- Pong:
```json
{"type":"pong"}
```

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

## Frontend Hook Integration
`frontend/hooks/useWebSocket.ts` behavior:
- URL resolution:
  - `NEXT_PUBLIC_WS_URL`, else derived from `NEXT_PUBLIC_API_URL`, else `ws://127.0.0.1:8000/api/websocket/stream`
- Heartbeat every 30s
- Reconnect backoff with max retries
- `registerCallback()` for low-rerender high-frequency consumption
- Supports `ticker` and `ticker_batch` messages (batch currently optional)

## Notes on Historical Drift
Older docs mentioned a 1s buffer/flush pipeline as the primary path.
Current implementation broadcasts normalized ticks immediately on arrival; use this document as source of truth.
