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
2. Validate `symbol_master`
3. Validate the database (create/upgrade the schema, `SELECT 1`)
4. Validate the Fyers token (warn-only)
5. Check the index universe data path (warn-only)
6. `live_market.connect(loop, prevalidated_token)`; market-hours rules apply, so off-hours it returns `market_closed` and the monitor loop connects once the market opens

Steps 4-6 do not use the database. If step 3 finds Postgres down it is deferred, not fatal: steps 4-6
still run, so live market streaming works without the DB. A background task retries the deferred schema
work with backoff (5s doubling to 60s), runs it once when the DB answers, logs
`[OK] Database reachable again; deferred startup completed` and stops. Shutdown cancels it.
`GET /api/websocket/status` and `GET /api/system/health` report it as `startup`: `DEFERRED` while
retrying, `FAILED` if the work hit a non-connection error after the DB came back, else `COMPLETE`.

## Shutdown Sequence
The lifespan `finally` runs each step even if an earlier one failed; a failure is one
`[WARN] Shutdown step <name> failed: ...` line with no traceback:
1. `deferred_startup`: cancel the deferred schema retry task
2. `live_market`: `live_market.shutdown()` cancels the monitor and flush loops and closes the provider socket
3. `websocket_clients`: `manager.close_all()` closes every client socket with code 1001 (going away)
4. `database_engine`: `engine.dispose()` closes the pooled DB connections

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
- The stream checks `Origin` before `accept()`: `config.ALLOWED_ORIGINS` (`CORS_ORIGINS`, shared with CORS) and any `http://localhost`, `http://127.0.0.1` or `http://[::1]` origin on any port (exact hostname, parsed with `urllib.parse`) get in; any other origin, including `null` and look-alikes such as `localhost.evil.com`, is refused with close code 1008 and a throttled warning log. A missing `Origin` (scripts, tests) is allowed
- Router uses `_safe_send()` to avoid disconnect exceptions leaking
- `ws_manager.connect()` is handshake-safe and returns `False` if client disappears early
- `ws_manager.broadcast()` removes dead sockets on `WebSocketDisconnect`/`RuntimeError`
- Subscription bookkeeping lives in one place, `SubscriptionRegistry` (`backend/app/services/subscription_registry.py`, held as `ws_manager.manager.registry`): a thread-safe refcount of DB-format symbols per owner, where an owner is a stream client id (`ws-N`) or the HTTP API owner (`api`). `add`/`remove`/`drop_owner` return the provider delta (symbols newly needed / no longer held by any owner), so the router never diffs before/after sets; `all_symbols()` is the provider replay set
- A stream `subscribe`/`unsubscribe` ack's `provider_delta` is the size of that registry delta; a symbol another owner (another client or the HTTP API) already holds is not re-subscribed, and is not released until its last owner lets go
- When the provider WS is not connected yet, subscribed symbols stay in the registry and are subscribed by the next replay
- The provider socket belongs to a `ProviderSession` (`backend/app/services/live/provider_session.py`), held as `LiveMarketService.session`. LiveMarketService keeps the policy (market hours, `DEV_MODE`, explicit disconnect) and talks only to the session; the session talks to a `DataSocket` (`services/live/data_socket.py`: `connect`, `close_connection`, `is_connected`, `subscribe`, `unsubscribe`, plus the `on_message`/`on_connect`/`on_close`/`on_error` callbacks). The Fyers SDK's `FyersDataSocket` satisfies that protocol; `fyers_websocket.py` only builds it (`fyers_socket_factory`) and reads the current token (`fyers_token`). Tests drive the session with `backend/tests/fakes/data_socket.py`
- Session states (`provider_state` in `GET /api/websocket/status`): `idle` (never started, or stopped off-hours or at shutdown), `connecting`, `connected`, `dropped` (the socket closed, or reported an error and is down), `failed` (the runner gave up) and `closed` (explicit disconnect). An SDK error on a socket that is still up (a bad symbol) leaves the state alone
- Provider connect runs on a background runner thread: every attempt fetches the current token (so a re-login takes effect on the next connect), closes the previous socket, builds a new one, waits up to 15s for it to report connected, counts a timeout as a failed attempt, closes the failed socket, and retries with exponential backoff (5s doubling to 60s, 10 attempts). An explicit disconnect or the off-hours cleanup wakes and stops the runner immediately. `connect()` returns `already_connected` while a connect is in progress or the provider socket is up
- On every provider (re)connect the session replays `registry.all_symbols()` (every symbol held by a connected client or the HTTP API, including those subscribed before the provider connected). That covers the SDK's own `reconnect=True` reconnects, which re-fire `on_connect` on the same socket; the runner replays itself only if `on_connect` did not fire. Symbols released while the replay ran are unsubscribed straight after it
- Callbacks from a socket the session has let go of (closed, replaced) are ignored, so a late SDK reconnect after an explicit disconnect does not bring the feed back. An exception in the tick handler or the replay is logged (rate-limited) and the session carries on
- `_monitor_loop` and `_flush_loop` catch and log unexpected errors per iteration (then pause 1s) instead of dying; `CancelledError` still propagates
- Async callers use `LiveMarketService.connect_async()`, which runs the blocking Fyers token validation in a worker thread; `connect()` stays available for sync callers
- Dropping a client (disconnect, slow-client overflow, dead socket) goes through `ws_manager.disconnect()`, which calls `registry.drop_owner()` and releases the symbols no remaining owner holds via `on_symbols_released`; slow clients are closed with code 1013. `LiveMarketService.unsubscribe()` re-checks the registry, so a release that races with a re-subscribe keeps the symbol, and after the provider unsubscribe it re-subscribes any symbol an owner took back while that call was in flight; symmetrically, `subscribe()` unsubscribes any symbol whose last owner let go while the provider subscribe was in flight. A sender loop that drops its own client does not cancel itself, so the release always completes
- `POST /api/websocket/disconnect` calls `LiveMarketService.disconnect()`; the monitor loop and `subscribe()` do not reconnect until an explicit `POST /api/websocket/connect` (non-explicit connects return `disconnected`)
- The Fyers client is resolved per use (broker and option chain do not cache it), and `reset_fyers_client()` also drops the class singleton, so re-login takes effect without restart
- `GET /api/websocket/status` is the operational health view for local runtime checks:
  - provider connection state
  - `startup`: `COMPLETE`, `DEFERRED` (DB was down at startup, schema work still retrying) or `FAILED`
  - `pending_subscriptions`: symbols waiting for the provider (the registry size while the provider socket is down, else 0)
  - websocket client/subscription counts
  - queue pressure and overflow counters
  - broadcast and provider timing metrics

## Frontend Hook Integration
`frontend/lib/realtime/quote-store.ts` owns the single socket; components read it through the hooks in `frontend/lib/realtime/hooks.ts`:
- URL resolution:
  - `VITE_WS_URL`, else derived from `VITE_API_URL`, else same origin `/api/websocket/stream` (proxied to the backend by Vite)
- Heartbeat every 30s
- Reconnect backoff with `max_retries=10`
- `useQuotes(symbols)` / `useQuote(symbol)` subscribe while mounted; subscriptions are ref-counted and only the aggregate delta is sent
- `useConnectionStatus()` for the connection badge
- Supports `ticker` and `ticker_batch` messages (batch currently optional)

## Screener Usage Pattern
- The screener subscribes the symbols of the rows on screen through `useQuotes`, so paging, sorting or filtering moves the subscription and unmounting drops it.
- Rows stay in the server's sort order; live values only overlay price, change and volume.
- Symbols with no streamed quote (or all of them while the socket is down) are polled from `GET /api/market/quotes/live`: every 3s while the market is open, every minute otherwise, at most 100 symbols.
- While the market is open a streamed quote older than 5s (by the store's `receivedAt`) counts as missing, so a connected but silent socket falls back to polling; the quote that arrived last wins. Outside market hours the stream wins while connected.

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
- provider connect attempts, connects, drops, failures and monitor-triggered reconnects (`live_market.provider.*`)
- flush/broadcast timings
- `logging.suppressed_tracebacks.<site>`: hot-path errors (tick handling, Fyers message handler, connect retries) log a full traceback the first time and then at most once per 60s per site; the skipped tracebacks are counted here

Live prices for paper fills and the market dashboard only use fresh ticks
(`LiveMarketService.get_fresh_tick`): market open (or `DEV_MODE`) and exchange time within
`PAPER_TICK_MAX_AGE_SEC` (default 60s). The tick cache keeps old ticks after a disconnect
or market close, so a cached tick alone is not a live price.

These metrics are intentionally lightweight for a single-user local app. They are meant
to improve debugging and future development, not to introduce a full telemetry stack.
