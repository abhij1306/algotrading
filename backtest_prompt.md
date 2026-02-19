# Backtest Implementation Prompt (Phase-1 Canonical)

Use this prompt with any coding agent to complete pending Backtest functionality in SmartTrader Phase-1.

## Role
You are the implementation agent for SmartTrader Backtest Phase-1.
Deliver PRD-compliant Backtest B2 and B3 with real data only, no mock paths.

## Non-Negotiable Source of Truth
Follow these docs in this order:
1. `SMARTTRADER_PRD_v2.md` (Backtest section: Phase-1 expectations, required endpoints, acceptance criteria).
2. `docs/execution/phase1-master-plan.md` (Backtest: B2, B3 pending).
3. `docs/execution/api-canonical-map.md` (canonical API contract).
4. `docs/DATABASE_MANAGEMENT_PLAYBOOK.md` (historical data policy: bhavcopy/corporate-actions/weightage only).
5. `docs/ARCHITECTURE.md` (current canonical architecture + data roots).

## Phase-1 Backtest Scope (Implement Now)
1. API endpoints (canonical only):
- `GET /api/backtest/status`
- `POST /api/backtest/run`
- `GET /api/backtest/result/{job_id}`

2. Strategy scope:
- Fixed EMA20/EMA50 crossover only (Phase-1).
- Universe: NIFTY50 only.
- Date range: 2025 onward (bounded by available curated snapshot dates).

3. UI behavior:
- Backtest page must not be blank or "Coming Soon".
- If data missing: explicit blocked state with instruction to run pipeline.
- If data available: allow run, show status, show result with:
  - equity curve
  - drawdown
  - trade log
  - benchmark overlay

## Mandatory File Targets
Start from these files and remove/replace legacy paths in touched scope.

### Backend
- `backend/app/main.py`
  - Resolve dual-router drift (`backtest` + `backtest_v2`) for active Phase-1 behavior.
- `backend/app/routers/backtest.py`
  - Make this canonical contract for `/status`, `/run`, `/result/{job_id}`.
- `backend/app/routers/backtest_v2.py`
  - Keep only if needed for compatibility; do not let it be canonical for Phase-1 UI.
- `backend/app/routers/data_snapshot.py`
  - Reuse for dataset availability/range checks where possible.
- `backend/app/engines/backtest_engine.py`
  - Remove assumptions tied to legacy `historical_prices`/old universe tables for Phase-1 route.
  - Either adapt engine or create dedicated Phase-1 engine/service.
- Optional new service file (recommended):
  - `backend/app/services/backtest_phase1_service.py`

### Frontend
- `frontend/app/backtest/page.tsx`
  - Show real status from `/api/backtest/status`.
- `frontend/app/backtest/new/page.tsx`
  - Remove `mockBacktestAPI` usage and call canonical `/api/backtest/run`.
- `frontend/app/backtest/results/runId/page.tsx`
  - Poll/read `/api/backtest/result/{job_id}`; do not use session mock-only path.
- `frontend/lib/backtest/api.ts`
  - Align to canonical backtest endpoints.
- `frontend/lib/backtest/mock-api.ts`
  - Remove from active runtime paths (delete if unused after migration in touched scope).

## Data Contract for Phase-1 Backtest Engine
Use curated artifacts only under `data_system/04_curated/phase1/`:
- `snapshot_nifty50_daily.parquet` (universe + weights + adjusted fields)
- `snapshot_stock_daily.parquet` (if needed for cross-checks)

Do not source backtest from:
- `historical_prices` fallback logic
- Fyers historical fetch
- legacy processed files outside canonical `data_system/`

## Implementation Requirements
1. Deterministic run behavior:
- same inputs => same outputs.

2. `GET /api/backtest/status` must return:
- data_ready boolean
- min_date, max_date
- universe support (`NIFTY50`)
- message string for blocked state

3. `POST /api/backtest/run` must:
- validate input range against available data
- accept minimal params (`start_date`, `end_date`, `initial_capital`)
- return `job_id` and initial `status`

4. `GET /api/backtest/result/{job_id}` must:
- return `status` (`queued|running|completed|failed`)
- when completed: metrics + equity curve + drawdown series + trade log + benchmark series

5. Benchmark:
- Use NIFTY50 universe-level benchmark derived from same snapshot data window.

6. Error semantics:
- explicit 4xx for invalid range/unavailable data
- explicit 404 for unknown job_id
- explicit failed status payload for runtime errors

## Acceptance Tests (Must Pass)
1. No-data scenario:
- API status says not ready
- UI shows clear "Historical data not loaded" message

2. Valid run scenario:
- `/run` returns `job_id`
- `/result/{job_id}` transitions to completed
- result includes non-empty equity curve and benchmark series

3. Contract compliance:
- Endpoint names and payloads match canonical map
- No mock/dummy values in touched backtest paths

4. Quality gate:
- `cd backend && ruff check . && pytest -o addopts=""`
- `cd frontend && npm run lint && npm run type-check && npm run build`

## Explicit Cleanup Rules in This Slice
1. Remove active runtime references to:
- `frontend/lib/backtest/mock-api.ts`
- `/api/backtest/v2/*` from active Phase-1 pages

2. Keep deletions safe:
- If compatibility path must remain, mark as deprecated and ensure not used by current UI route.

## PR/Commit Output Requirements
Agent must provide:
1. Files changed with one-line purpose.
2. API request/response examples for all 3 canonical endpoints.
3. Test/build command results.
4. Remaining risks/gaps (if any), especially data coverage constraints.

## Do Not Do
1. Do not introduce new universes for Phase-1.
2. Do not re-enable Fyers historical ingestion.
3. Do not keep mock API in active backtest UI path.
4. Do not use legacy data roots outside `data_system/`.
