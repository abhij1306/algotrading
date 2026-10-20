# SmartTrader Architecture

**Status:** Canonical
**Last updated:** 2026-03-15

## System Topology
- Frontend: Vite+ SPA with React Router (`frontend/router.tsx`, pages in `frontend/app`)
- Backend: FastAPI (`backend/app`)
- Realtime: Fyers WS -> `LiveMarketService` -> `ws_manager` -> backend WS clients
  `ws_manager` is the connection/dispatcher layer that owns client sockets, subscription routing,
  slow-client backpressure handling, and lightweight websocket protocol dispatch so
  `LiveMarketService` does not own low-level client connection concerns.
- Storage:
  - Transactional: PostgreSQL/SQLite via SQLAlchemy models
  - Phase-1 datasets: `data_system/` (2025+ active scope)

## Runtime Layers
1. UI Layer
- Pages: Dashboard, Screener, Terminal, Strategies, Backtest
- Shared realtime store: `frontend/lib/realtime` (one socket; `useQuote`/`useQuotes`/`useConnectionStatus`)
- Terminal trading surfaces:
  - `frontend/app/terminal/page.tsx` (options-first board + execution ticket + panels)

2. API Layer
- FastAPI routers in `backend/app/routers`
- Canonical realtime routes:
  - `POST /api/websocket/connect`
  - `POST /api/websocket/subscribe`
  - `POST /api/websocket/disconnect`
  - `GET /api/websocket/status`
  - `WS /api/websocket/stream`
- Trading and options routes:
  - `/api/trading/*` (mode/order/orders/positions/tradebook/funds/risk-check)
  - `/api/options/*` (general market-data contracts: chain/expiries/atm/greeks, suitable for non-terminal clients)
  - `/api/terminal/options/*` (terminal-optimized board/depth/orderflow/preview-order plus order alias)
  - Route relationship:
    - Shared market data: both route groups read from the same option data services.
    - Terminal-only: board/depth/orderflow/preview-order are UI-focused aggregates not exposed in `/api/options/*`.
    - Order alias: terminal options order endpoint maps terminal payload to `/api/trading/order` semantics.
- Typed frontend contract:
  - `frontend/lib/api/schema.d.ts` is generated from the FastAPI OpenAPI schema and used by the
    `openapi-fetch` client. Never edit it by hand.
  - `scripts/export_openapi.py` imports `app.main:app` (the lifespan does not run, so no DB, broker or
    websocket connection) and writes the schema with sorted keys; `pnpm gen:api` in `frontend/` runs it,
    then openapi-typescript (pinned in `frontend/tools/openapi-gen`, which carries its own TypeScript 5)
    and `vp fmt`.
  - CI job `api-schema` regenerates the file and fails if it differs from the committed one, so any
    backend route or model change must ship with the regenerated types.

3. Domain Services
- `symbol_master.py`: symbol normalization and provider boundary conversion
- `index_universe_loader.py`: loads canonical index constituent CSVs from `data_system/03_universe/constituents`
- `services/universe`: canonical runtime universe lookup; reads DB-backed universe tables first and falls back to `index_universe_loader` when DB is not seeded yet
- `symbol_history` / lifecycle services: date-effective rename, merger, demerger, delisting, successor-predecessor mapping
- `live_market_service.py`: market-hours gating, provider connect/reconnect, tick normalization, broadcast dispatch
- `ws_manager.py`: websocket client multiplexing, subscription fanout, bounded per-client queues,
  and connection cleanup/backpressure handling
- `order_execution_service.py`: unified PAPER/LIVE execution routing
- `option_chain_service.py`: option chain fetch/cache + Greeks
- `risk_manager.py`: pre-trade risk checks for live path

4. Data Layer
- DB entities: company, historical_price, intraday_candle, universe definitions/history, symbol history, orders, positions, dataset_run, snapshot index tables
- Curated snapshot artifacts (Phase-1):
  - `data_system/04_curated/phase1/*.parquet`
  - read by `backend/app/routers/data_snapshot.py`

## Backbone Architecture
The following backbone is feature-agnostic and must not be owned by any single feature:

1. Database Management
- Defines storage contracts, ingestion stages, validation, publication, and DB sync rules.
- Owns canonical roots under `data_system/`. Backend code imports them (and the repo root, `.env`,
  `logs/`, the Fyers token file) from `backend/app/paths.py`; do not compute paths from `__file__`.
- Request dates are parsed with `app.utils.dates.parse_iso_date`, which answers a malformed date with
  a fixed 400 `INVALID_DATE` envelope naming the field.
- Strategy/backtest/live-trading code may consume these contracts but must not redefine them.

2. Universe Management
- Owns universe definition, effective-date constituent membership, weight history, and snapshots.
- Must support:
  - live lookup using latest active membership
  - historical lookup using effective dates
  - current-constituent fallback only as an explicit policy choice

3. Symbol Management
- Owns provider normalization and symbol identity resolution.
- All provider-facing symbol conversion must pass through `symbol_master`.

4. Symbol Lifecycle Management
- Owns corporate identity changes over time:
  - renames
  - mergers
  - demergers
  - delistings
  - series changes
- Features must ask lifecycle-aware services for symbol resolution rather than hardcoding aliases.

The app should expose these as stable platform services that any future feature can plug into:
- `resolve_universe(universe_id, as_of_date)`
- `resolve_symbols(universe_id, as_of_date)`
- `resolve_symbol(symbol, provider, as_of_date)`
- `load_price_history(symbols, timeframe, start_date, end_date, adjusted, source_policy)`
- `load_index_history(universe_id, start_date, end_date)`
- `get_symbol_lineage(symbol, as_of_date)`

## Data Architecture (Consolidated)
### Active Inputs (Phase-1)
- `data_system/01_sources/fyers_index_prices/universe_index_price_daily.parquet` (index universe prices for backtesting)
- `data_system/01_sources/fyers_stock_prices/stock_price_daily.parquet` (supplemental provider daily cache; not authoritative over curated equity pipeline)
- `data_system/01_sources/nse_bhavcopy/*.csv`
- `data_system/01_sources/nse_corporate_actions/*.csv`
- `data_system/01_sources/nse_index_weights_pdf/**/NIFTY_50_*.pdf`
- `data_system/03_universe/monthly_universe_raw/*/nifty50.csv`
- `data_system/03_universe/constituents/*.csv`
- `data_system/05_metadata/reference/*.csv`

### Active Outputs (Phase-1)
- `data_system/04_curated/phase1/equity_ohlcv.parquet`
- `data_system/04_curated/phase1/equity_ohlcv_adj.parquet`
- `data_system/04_curated/phase1/nifty50_weights_monthly.parquet`
- `data_system/04_curated/phase1/nifty50_membership_daily.parquet`
- `data_system/04_curated/phase1/snapshot_stock_daily.parquet`
- `data_system/04_curated/phase1/snapshot_nifty50_daily.parquet`
- Metadata: `data_system/05_metadata/phase1/*`

### Canonical Data Pipeline
- Entrypoint: `data_platform/pipelines/phase1_build.py`
- Modes: `--mode full|incremental`
- Contract: 2025-01-01 onward only in active runtime
- Scope: stock pipeline only (bhavcopy/corporate actions/universe membership); index universe prices are managed separately via consolidated Fyers dataset.

## Data Lanes
The system intentionally separates these lanes:

1. Universe and index lane
- Universe weights, monthly constituent changes, snapshots, and direct index price series.
- Supports historical universe reconstruction and survivorship-bias-aware backtests.

2. Equity daily lane
- Daily stock OHLCV, adjusted OHLCV, technicals, and snapshots.
- Must be stored in consolidated datasets, not scattered feature-specific downloads.

3. Intraday archive lane
- Optional provider-specific archive for future use.
- Not part of the default canonical backtest/runtime path unless a feature explicitly requires it.
- Must be consolidated by contract, not stored long-term as thousands of per-symbol shard files.

4. Feature lane
- Strategy, screener, terminal, allocator, and analytics features.
- Must read from the backbone services and canonical datasets instead of building their own symbol/universe logic.

## Provider Script Policy
Provider scripts must be source adapters, not feature adapters.

Allowed long-term script shapes:
- `refresh_fyers_index_daily`
- `refresh_fyers_equity_daily`
- `refresh_fyers_intraday_archive`

Avoid long-term patterns like:
- any script that bakes a single feature or a single universe into its identity

Provider scripts should accept:
- `universe`
- `symbols`
- `timeframe`
- `range_from`
- `range_to`
- `refresh_policy`
- `output_contract`

## Symbol Boundary Rules
- Storage + internal logic: DB format (`SBIN`, `NIFTY50`)
- Provider calls: Fyers format (`NSE:SBIN-EQ`, `NSE:NIFTY50-INDEX`)
- UI display: DB format
- All conversions must use `symbol_master` only.

## Realtime Flow (Current)
1. Frontend opens `WS /api/websocket/stream`
2. Client subscribes symbols (DB/Fyers accepted; normalized to DB)
3. Router computes subscription deltas across all clients
4. `LiveMarketService.subscribe()` converts to Fyers and subscribes provider
5. Incoming provider tick -> normalized (`symbol`, `change_pct`, `change`, `volume`) -> buffered for timer-based batch flush
   Flush policy:
   `flushIntervalMs = 200` via the `_flush_loop()` timer in `LiveMarketService`.
   There is no size-based flush trigger today and no explicit `maxBufferSize`; the buffer is a symbol-keyed map,
   so newer ticks replace older ticks for the same symbol until the next flush.
   Backpressure strategy: websocket fanout uses bounded per-client queues in `ws_manager` (`CLIENT_QUEUE_SIZE = 8`);
   if a client queue overflows, that client is disconnected rather than slowing provider ingestion or spilling to disk.
   Expected latency is sub-second under normal local use, with a nominal additional flush delay of up to 200ms.
6. `ws_manager` filters per-client by subscription set before send

Operational endpoint:
- `GET /api/websocket/status` exposes provider state plus websocket health metrics for local development and on-host operational health checks.

## Terminal Execution Flow
1. UI sets mode through `POST /api/trading/mode`.
2. Orders use unified contract (`/api/trading/order`) for both PAPER and LIVE.
3. `order_execution_service` routes by mode:
- PAPER: simulated fill path only.
- LIVE: confirmation + risk + broker dispatch.
4. Orders and account panels are served from `/api/trading/orders|positions|tradebook|funds`.

### Order idempotency (`client_order_id`)
- Every order request may carry `client_order_id`, a UUID v4 the client generates once per submit
  intent and reuses on every retry of that intent (a new click is a new id). It is **required for
  LIVE** (`POST /api/trading/order` and `POST /api/terminal/options/order` answer 422 without it) and
  optional for PAPER. It is stored in `live_orders.client_order_id` under the unique index
  `uq_live_orders_client_order_id`; `ensure_live_order_schema` adds the column and index to existing
  databases at startup.
- LIVE orders are persisted first: the row is committed as `PENDING_SUBMIT` before the broker is
  called, then updated with the broker id and status. If that first commit fails the broker is never
  called (a down database answers 503). If the database drops after the broker accepted, the row
  stays `PENDING_SUBMIT` and the server logs the broker order id with its `client_order_id`.
- A request whose `client_order_id` already exists returns HTTP 200 with the existing order's
  current state and `duplicate: true`, and never reaches the broker or the paper simulator again.
  Two concurrent identical requests race on the unique index; the loser returns the winner's row.
  Fresh orders answer with `client_order_id` and `duplicate: false`.
- Duplicate response: `{order_id, status, message, average_price, filled_qty, client_order_id, duplicate}`.
- The Fyers order carries `orderTag` = `ST` + the first 18 hex digits of the `client_order_id`
  (uppercase, 20 alphanumeric characters) so a broker order can be matched to its row.
- The terminal tags orders by instrument: `terminal-equity` for equities, `terminal-options` for
  options, whatever terminal view is open.

## Options Board Data Flow
1. UI selects underlying + expiry.
2. `GET /api/terminal/options/board` returns chain context + ATM summary.
3. `GET /api/terminal/options/depth` returns depth for selected contract/underlying.
4. `GET /api/terminal/options/orderflow` returns derived OI/volume/depth metrics.
5. WebSocket ticks update contract LTP and position PnL between polling cycles.

## Options API Usage Guidance
- Use `/api/options/*` when you need reusable market-data primitives (chain/expiries/atm/greeks) for external tools or non-terminal workflows.
- Use `/api/terminal/options/*` from terminal UI flows where compact board/depth/orderflow payloads and preview-order behavior are required.

## Market-Hours Policy
- Open window: 09:15–15:30 IST, weekdays
- Default `DEV_MODE=false` in startup scripts
- Off-hours: provider WS connection is skipped/cleaned up

## Startup
- `start.bat` -> `start_dev.py` -> backend `backend/start_server.py`
- Backend health probe: `/ping`

## Database Unavailable
- The Postgres engine uses `connect_timeout=3`, so a stopped server fails a request in seconds.
- Startup logs one line, `[ERROR] Database unreachable at host:port/db (start Postgres: net start postgresql-x64-18)`,
  and the server keeps running. The database step is deferred, not fatal: the Fyers token check, index universe
  and live market connect still run (see `docs/Websocket.md`, Startup Sequence). A background task
  (`app/services/deferred_startup.py`) retries the schema work (5s doubling to 60s) and, once the DB answers, runs it
  once and logs `[OK] Database reachable again; deferred startup completed`. Shutdown cancels it.
- `GET /api/system/health` and `GET /api/websocket/status` carry `startup`: `DEFERRED` while that retry is pending
  (health is then at best `DEGRADED`), `FAILED` if the work hit a non-connection error, else `COMPLETE`.
- Any request that hits a down database answers HTTP 503
  `{"error": {"code": "DATABASE_UNAVAILABLE", "message": "Database unavailable"}}`. This holds when a
  router wraps the error (`raise HTTPException(...) from exc`). Driver text never reaches the client.
- `GET /api/system/health` answers 503 with its normal report (`status: DOWN`, `postgresql: DOWN`) plus that envelope.
  If the database answers but the probe query fails (schema drift, SQL bug), it reports `postgresql: ERROR` and
  `status: DEGRADED` with HTTP 200, not an outage.
- "Database unavailable" means connection loss only (`app/utils/db_errors.py`): OperationalError, InterfaceError,
  or a connection-invalidated DBAPIError. SQL bugs (ProgrammingError, IntegrityError) stay 500.
- Services must not turn a down database into empty lists or zeros. Order placement fails closed: the PENDING
  row is flushed before the broker call, so a LIVE order never reaches the broker while the DB is down.
- Logging: the handler writes one throttled warning per minute. A log filter strips the traceback from any
  `logger.exception` record whose exception is a down DB, and lets through one such line per call site per minute.

## Error Text and Unavailable Dependencies
- Clients never receive exception text (driver, broker, file system). Routers log the exception and answer a
  fixed message; `handle_api_error`/`sanitize_error_message` return only the fixed message in every environment.
  Validation messages we write ourselves (a `ValueError` raised by our own input checks) may still be returned.
- Broker-backed reads fail closed: `GET /api/smart-trader/positions` and `/pnl` answer HTTP 503
  `{"error": {"code": "BROKER_UNAVAILABLE", "message": "Broker unavailable"}}` instead of an empty list or a
  zero P&L. The message is `"Broker not connected"` when there is no client or the login token is invalid.
- Missing local datasets answer HTTP 503 `DATA_UNAVAILABLE`; for example `GET /api/strategies/regime` (and every
  route that needs the regime) when the NIFTY50 index history is missing.

## Drift Policy
If code and docs diverge, code is authoritative; docs must be updated in the same PR/commit.
