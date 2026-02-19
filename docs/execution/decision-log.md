# SmartTrader Decision Log (Phase-1 Rebuild)

## 2026-02-19
### D-001: Hard Reset Baseline
- Decision: treat existing implementations as non-trusted unless verified against PRD v2.
- Rationale: large historical drift and dummy/partial features conflict with Phase-1 scope.

### D-002: Workflow System
- Decision: use `docs/execution/*` + `todos/` as execution source of truth.
- Rationale: enforce long-horizon traceability with dependency-based task control.

### D-003: Slice Model
- Decision: vertical slices only.
- Rationale: avoids backend/frontend drift and allows strict acceptance at each increment.

### D-004: Out-of-Scope Handling
- Decision: delete out-of-scope Phase-1 features when touched.
- Rationale: reduce technical debt and avoid zombie paths.

### D-005: Module Order
- Decision: Dashboard -> Screener -> Terminal -> Backtest.
- Rationale: matches PRD dependency and investor-facing priority.

### D-006: Dashboard Market Status Source of Truth
- Decision: Dashboard market state uses backend `/api/market/status` instead of frontend local time utility.
- Rationale: avoids divergence with backend `DEV_MODE`, holiday logic, and live-connection truthfulness requirements in D1.

### D-007: Market Index Data Honesty
- Decision: `/api/market/indices` returns only valid live rows (or empty list), not synthetic zero-filled placeholders.
- Rationale: Phase-1 widgets must not show dummy values; empty/unavailable should be explicit in UI.

### D-008: Dashboard Unavailable-State Contract
- Decision: Dashboard watchlist and portfolio widgets render explicit unavailable states (`—`, `Unavailable`) when data is missing, instead of numeric fallback values.
- Rationale: prevents misleading trading signals from synthetic zeros and aligns with strict no-dummy-data rule.

### D-009: Post-Market Canonical Feed
- Decision: Post-market dashboard widgets now consume `/api/market/overview` as canonical aggregate feed, with explicit source/timestamp display.
- Rationale: removes fragmented ad-hoc calls, centralizes fallback logic (Fear & Greed/MMI/VIX), and improves stale-data transparency.

### D-010: Screener Canonical Contract Simplification
- Decision: Screener backend is constrained to Phase-1 canonical endpoints (`/api/screener/indices`, `/api/screener/results`) with strict universe validation and explicit error states.
- Rationale: removes legacy endpoint drift and keeps S1 contract aligned to PRD data-integrity requirements.

### D-011: WebSocket Aggregate Subscription Control
- Decision: WebSocket subscribe/unsubscribe now applies net symbol deltas across all active clients before calling live provider subscribe/unsubscribe.
- Rationale: prevents subscription storms and accidental unsubscribes when multiple pages/clients observe overlapping symbols.

### D-012: Screener Global Sort Authority
- Decision: Screener frontend no longer re-sorts paginated rows client-side; backend `/api/screener/results` sort is authoritative.
- Rationale: guarantees global sort correctness with pagination and reduces client compute churn under live updates.

### D-013: Terminal Chart Contract
- Decision: Introduced canonical terminal chart endpoint (`/api/terminal/chart`) and bound Terminal UI to it for OHLCV + EMA overlays with timeframe-aware fetch.
- Rationale: replaces placeholder chart behavior with real-source chart data and keeps T1 slice independently testable.

### D-014: Terminal Paper/Live Path Separation
- Decision: Terminal paper orders use dedicated `/api/terminal/paper/order` path that bypasses broker APIs; live orders continue through trading live path.
- Rationale: enforces unambiguous execution boundaries and guarantees paper mode cannot accidentally hit broker execution.

### D-015: Terminal Panel Data Contract
- Decision: Terminal bottom panels poll positions/orderbook via trading APIs and render explicit LIVE/PAPER labels; websocket ticks update live position LTP/P&L in-place.
- Rationale: ensures T3 trustworthiness by combining stable polling with low-latency tick updates and explicit mode separation.

### D-016: Phase-1 Data Canonical Root
- Decision: use `data_system/` as canonical Phase-1 root with explicit `01_sources/`, `04_curated/phase1/`, and `05_metadata/phase1/` boundaries.
- Rationale: isolates trusted Phase-1 datasets from legacy folders and enables deterministic rebuilds.

### D-017: Single Data Orchestrator
- Decision: standardize Phase-1 build on `python -m data_platform.pipelines.phase1_build` with idempotent subcommands.
- Rationale: removes multi-script drift and makes ingestion, curation, validation, and publish repeatable.

### D-018: NIFTY50 Source Priority Lock
- Decision: monthly NIFTY50 weights use Downloads PDFs as primary source, then repo raw CSV fallback, then clean snapshot fallback, with anomaly logging.
- Rationale: preserves freshest monthly source while preventing pipeline halts on missing/corrupt files.

### D-019: Snapshot Serving Contract
- Decision: add Phase-1 snapshot APIs (`/api/data/snapshot/stock`, `/api/data/snapshot/universe`, `/api/data/snapshot/status`) backed by curated parquet artifacts.
- Rationale: creates direct day-level retrieval path for backend and future UI surfaces without re-running transforms.

### D-020: Archive-First Legacy Data Consolidation
- Decision: move legacy duplicate/non-canonical data trees out of active runtime paths into `archive/data-legacy/2026-02-19/` and keep only canonical/runtime-required datasets under `data_system/`.
- Rationale: enforces single active data layout, reduces accidental reads from stale artifacts, and keeps rollback safety via archive retention.

### D-021: Remove Broker-Historical Price Backfill from Canonical Pipeline
- Decision: deprecate `fetch-fyers-ohlcv` from canonical Phase-1 build; keep only bhavcopy + corporate actions + monthly weightage as trusted inputs.
- Rationale: historical broker-derived OHLCV is now treated as non-trusted for Phase-1 reproducibility and must not influence canonical artifacts.
