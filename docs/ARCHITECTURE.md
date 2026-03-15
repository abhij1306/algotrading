# SmartTrader Architecture

**Status:** Canonical
**Last updated:** 2026-03-15

## System Topology
- Frontend: Next.js App Router (`frontend/app`)
- Backend: FastAPI (`backend/app`)
- Realtime: Fyers WS -> `LiveMarketService` -> backend WS clients
- Storage:
  - Transactional: PostgreSQL/SQLite via SQLAlchemy models
  - Phase-1 datasets: `data_system/` (2025+ active scope)

## Runtime Layers
1. UI Layer
- Pages: Dashboard, Screener, Terminal, Strategies, Backtest
- Shared realtime hook: `frontend/hooks/useWebSocket.ts`
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

3. Domain Services
- `symbol_master.py`: symbol normalization and provider boundary conversion
- `index_universe_loader.py`: loads canonical index constituent CSVs from `data_system/03_universe/constituents`
- `services/universe`: canonical runtime universe lookup; reads DB-backed universe tables first and falls back to `index_universe_loader` when DB is not seeded yet
- `symbol_history` / lifecycle services: date-effective rename, merger, demerger, delisting, successor-predecessor mapping
- `live_market_service.py`: market-hours gating, provider connect/reconnect, tick normalization, broadcast dispatch
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
- Owns canonical roots under `data_system/`.
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
- `consolidate_fyers_archives`

Avoid long-term patterns like:
- `download_fyers_nifty500_5min.py`
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
5. Incoming provider tick -> normalized (`symbol`, `change_pct`, `change`, `volume`) -> immediate broadcast as `ticker`
6. `ws_manager` filters per-client by subscription set before send

## Terminal Execution Flow
1. UI sets mode through `POST /api/trading/mode`.
2. Orders use unified contract (`/api/trading/order`) for both PAPER and LIVE.
3. `order_execution_service` routes by mode:
- PAPER: simulated fill path only.
- LIVE: confirmation + risk + broker dispatch.
4. Orders and account panels are served from `/api/trading/orders|positions|tradebook|funds`.

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

## Drift Policy
If code and docs diverge, code is authoritative; docs must be updated in the same PR/commit.
