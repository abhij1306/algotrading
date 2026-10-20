# WebSocket Reference

**Status:** Canonical
**Last updated:** 2026-10-20

## Endpoints
- `POST /api/websocket/connect`
- `POST /api/websocket/subscribe`
- `POST /api/websocket/disconnect`
- `GET /api/websocket/status`
- `WS /api/websocket/stream`

## Startup Sequence
`backend/app/main.py` lifespan, in order:
1. Get the event loop
2. Set the loop on the WebSocket manager
3. Validate `symbol_master`
4. Validate the database (create/upgrade the schema, `SELECT 1`)
5. Validate the Fyers token (warn-only)
6. Check the index universe data path (warn-only)
7. `live_market.connect(loop, prevalidated_token)`; market-hours rules apply, so off-hours it returns `market_closed` and the monitor loop connects once the market opens

Steps 5-7 do not use the database. If step 4 finds Postgres down it is deferred, not fatal: steps 5-7
still run, so live market streaming works without the DB. A background task retries the deferred schema
work with backoff (5s doubling to 60s), runs it once when the DB answers, logs
`[OK] Database reachable again; deferred startup completed` and stops. Shutdown cancels it.
`GET /api/websocket/status` and `GET /api/system/health` report it as `startup`: `DEFERRED` while
retrying, `FAILED` if the work hit a non-connection error after the DB came back, else `COMPLETE`.

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
- Off-hours: skip provider connect and cleanup dropped sessions; the monitor's off-hours cleanup also stops a connect runner that is still retrying, without blocking the next session's automatic connect
- `DEV_MODE=true`: forces market-open behavior for development
- Current startup defaults set `DEV_MODE=false`

## Connection and Stability Rules
- Router uses `_safe_send()` to avoid disconnect exceptions leaking
- `ws_manager.connect()` is handshake-safe and returns `False` if client disappears early
- `ws_manager.broadcast()` removes dead sockets on `WebSocketDisconnect`/`RuntimeError`
- Live service queues pending subscriptions when provider WS is not yet connected
- Provider connect runs in a thread (`_run_provider_connect`): after `connect()` it waits up to 15s for the socket to report connected, counts a timeout as a failed attempt, and retries with exponential backoff (5s doubling to 60s, `max_retries=10`). An explicit disconnect wakes and stops the runner immediately. `connect()` returns `already_connected` when a connect is in progress or the provider socket is already up
- The SDK's own `reconnect=True` reconnects are covered too: `FyersWebSocketService.on_open_handler` (wired to the SDK `on_connect` callback) calls `LiveMarketService.on_provider_open()`, which waits for the socket and then calls `on_ws_connected()`. The runner skips its own replay when that hook already ran for the attempt
- On every provider (re)connect, `LiveMarketService.on_ws_connected()` replays the aggregate symbol set of all connected clients, HTTP-API subscriptions and any queued symbols
- `_monitor_loop` and `_flush_loop` catch and log unexpected errors per iteration (then pause 1s) instead of dying; `CancelledError` still propagates
- Async callers use `LiveMarketService.connect_async()`, which runs the blocking Fyers token validation in a worker thread; `connect()` stays available for sync callers
- Dropping a client (disconnect, slow-client overflow, dead socket) goes through `ws_manager.disconnect()`, which releases provider symbols no remaining client needs via `on_symbols_released`; slow clients are closed with code 1013
- `POST /api/websocket/disconnect` calls `LiveMarketService.disconnect()`; the monitor loop and `subscribe()` do not reconnect until an explicit `POST /api/websocket/connect` (non-explicit connects return `disconnected`)
- The Fyers client is resolved per use (broker and option chain do not cache it), and `reset_fyers_client()` also drops the class singleton, so re-login takes effect without restart
- `GET /api/websocket/status` is the operational health view for local runtime checks:
  - provider connection state
  - `startup`: `COMPLETE`, `DEFERRED` (DB was down at startup, schema work still retrying) or `FAILED`
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
- `logging.suppressed_tracebacks.<site>`: hot-path errors (tick handling, Fyers message handler, connect retries) log a full traceback the first time and then at most once per 60s per site; the skipped tracebacks are counted here

Live prices for paper fills and the market dashboard only use fresh ticks
(`LiveMarketService.get_fresh_tick`): market open (or `DEV_MODE`) and exchange time within
`PAPER_TICK_MAX_AGE_SEC` (default 60s). The tick cache keeps old ticks after a disconnect
or market close, so a cached tick alone is not a live price.

These metrics are intentionally lightweight for a single-user local app. They are meant
to improve debugging and future development, not to introduce a full telemetry stack.
