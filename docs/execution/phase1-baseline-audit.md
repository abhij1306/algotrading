# Phase-1 Baseline Audit (PRD v2)

Date: 2026-02-19

## Scope
Baseline inventory and PRD gap snapshot before module-by-module rebuild.

## Keep/Delete Inventory
## Keep
- `nse_data/` (required for backtest pipeline)
- `backend/app/` core services and routers (to be validated/refactored per slice)
- `frontend/app/{dashboard,screener,terminal,backtest}` scaffolding
- shared infrastructure: startup scripts, env handling, websocket core, symbol conversion, index loader

## Delete or Decommission
- Planning/tooling directories no longer used: `.kiro/`, `.trae/`
- PRD Phase-1 out-of-scope implementation paths when touched:
  - advanced options-chain UI/strategy-builder style features
  - non-Phase-1 custom screener builders/alerts
  - backtest mock-only surfaces
- dummy/mock data usage in runtime paths for Phase-1 pages

## API Namespace Snapshot (Current)
Observed router overlap and legacy paths include:
- `market.py` has both `/market/status` and `/status`
- `market.py` has both `/market/indices` and `/indices`
- `market_dashboard.py` adds `/market/indices` and other market-overview endpoints
- Backtest endpoints split across `backtest.py` and `backtest_v2.py`

Action:
- standardize canonical Phase-1 endpoint surface as defined by PRD and map/remediate duplicates in slice work.

## PRD Requirement Status Snapshot
Legend: Implemented | Partial | Wrong | Missing

### Dashboard
- Live core state (status + live ticks): Partial
- Required widgets (gainers/losers/sector/watchlist/portfolio): Partial
- Post-market mode data policy: Partial
- No-dummy-data compliance: Wrong (mock/placeholder paths present in project)

### Screener
- 33 universe loading via index loader: Implemented
- Live price/change/volume updates: Partial
- Global sort across full universe: Partial
- strict Phase-1 scope cleanup: Partial

### Terminal
- Chart + EMA overlay + live updates: Partial
- Live/paper order flow separation: Partial
- positions/orderbook integrity and clarity: Partial
- no placeholder-only UI states in core flow: Partial

### Backtest
- Nifty50 data pipeline (constituent-aware + corp actions): Missing/Partial
- Minimal Phase-1 run/status/result UI/API: Partial
- benchmark and validation workflow: Partial
- mock-data elimination in runtime Phase-1 path: Wrong

## Notable Findings from Code Scan
- Mock/backtest utility usage in frontend runtime paths:
  - `frontend/app/backtest/new/page.tsx` imports mock API
  - `frontend/lib/backtest/mock-api.ts` actively present
- TODO/FIXME density in core services indicates incomplete production logic in several areas.
- Endpoint volume is large and includes overlapping responsibilities across market/backtest routers.

## Immediate Phase-0 Deliverables
- Execution docs created under `docs/execution/`
- Todo tracker initialized under `todos/`
- Legacy planning directories removed (`.kiro`, `.trae`)
- canonical endpoint normalization and per-page cleanup moved into slice tasks
