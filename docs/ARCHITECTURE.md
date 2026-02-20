# SmartTrader Architecture

**Status:** Canonical
**Last updated:** 2026-02-19

## System Topology
- Frontend: Next.js App Router (`frontend/app`)
- Backend: FastAPI (`backend/app`)
- Realtime: Fyers WS -> `LiveMarketService` -> backend WS clients
- Storage:
  - Transactional: PostgreSQL/SQLite via SQLAlchemy models
  - Phase-1 datasets: `data_system/` (2025+ active scope)

## Runtime Layers
1. UI Layer
- Pages: Dashboard, Screener, Terminal, Backtest
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
  - `/api/options/*` (chain/expiries/atm/greeks)
  - `/api/terminal/options/*` (board/depth/orderflow/preview-order/order alias)

3. Domain Services
- `symbol_master.py`: symbol normalization and provider boundary conversion
- `index_universe_loader.py`: loads 33 index constituent CSVs from `data_system/03_universe/constituents`
- `live_market_service.py`: market-hours gating, provider connect/reconnect, tick normalization, broadcast dispatch
- `order_execution_service.py`: unified PAPER/LIVE execution routing
- `option_chain_service.py`: option chain fetch/cache + Greeks
- `risk_manager.py`: pre-trade risk checks for live path

4. Data Layer
- DB entities: company, historical_price, orders, positions, dataset_run, snapshot index tables
- Curated snapshot artifacts (Phase-1):
  - `data_system/04_curated/phase1/*.parquet`
  - read by `backend/app/routers/data_snapshot.py`

## Data Architecture (Consolidated)
### Active Inputs (Phase-1)
- `data_system/01_sources/fyers_index_prices/universe_index_price_daily.parquet` (index universe prices for backtesting)
- `data_system/01_sources/nse_bhavcopy/*.csv`
- `data_system/01_sources/nse_corporate_actions/*.csv`
- `data_system/01_sources/nse_index_weights_pdf/**/NIFTY_50_*.pdf`
- `data_system/03_universe/monthly_universe_raw/*/nifty50.csv`
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

## Market-Hours Policy
- Open window: 09:15–15:30 IST, weekdays
- Default `DEV_MODE=false` in startup scripts
- Off-hours: provider WS connection is skipped/cleaned up

## Startup
- `start.bat` -> `start_dev.py` -> backend `backend/start_server.py`
- Backend health probe: `/ping`

## Drift Policy
If code and docs diverge, code is authoritative; docs must be updated in the same PR/commit.
