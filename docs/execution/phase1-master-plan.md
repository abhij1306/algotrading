# SmartTrader Phase-1 Master Plan (PRD v2)

## Objective
Rebuild SmartTrader to align strictly with `SMARTTRADER_PRD_v2.md`, using vertical slices and hard cleanup of out-of-scope features.

## Execution Order
1. Dashboard
2. Screener
3. Terminal
4. Backtest

## Delivery Model
- Vertical slices only (API + frontend + tests together).
- Every slice must pass strict quality gate before moving forward.
- Out-of-scope Phase-1 features are removed when touched.

## Quality Gate (Blocking)
- API contract validation for touched endpoints
- PRD acceptance scenarios for touched page/module
- Frontend: lint + type-check + build
- Backend: lint + tests
- WebSocket behavior checks for live modules
- No dummy data paths in touched scope

## Tracking System
- Decision log: `docs/execution/decision-log.md`
- Risk register: `docs/execution/risk-register.md`
- Baseline audit: `docs/execution/phase1-baseline-audit.md`
- Data operations playbook: `docs/DATABASE_MANAGEMENT_PLAYBOOK.md`
- Work tracker: `todos/`

## Slice Map
### Dashboard
- D1: Live Core Market State
- D2: Market Widgets (Phase-1 only)
- D3: Post-Market Mode

### Screener
- S1: Universe + Data Integrity
- S2: Real-time Update Model
- S3: Global Sort + Pagination

### Terminal
- T1: Chart + Instrument Context
- T2: Order Panel (Live + Paper)
- T3: Positions + Order Book

### Backtest
- B1: Nifty50 Historical Pipeline
- B2: Minimal Backtest Engine UI/API
- B3: Results + Benchmark Validation

## Current Status (2026-02-19)
- `D1` complete
- `D2` complete
- `D3` complete
- `S1` complete
- `S2` complete
- `S3` complete
- `T1` complete
- `T2` complete
- `T3` complete
- `B1` ready
- `B2-B3` pending

## Phase-1 Data Consolidation (Implemented 2026-02-19)
- Canonical data root created: `data_system/`
- Canonical orchestrator implemented: `data_platform/pipelines/phase1_build.py`
- Canonical metadata artifacts in place:
  - `data_system/05_metadata/phase1/source_manifest.json`
  - `data_system/05_metadata/phase1/checksums.json`
  - `data_system/05_metadata/phase1/validation_report.json`
  - `data_system/05_metadata/phase1/run_log.jsonl`
- Backend snapshot APIs added:
  - `GET /api/data/snapshot/stock`
  - `GET /api/data/snapshot/universe`
  - `GET /api/data/snapshot/status`
