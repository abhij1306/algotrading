# SmartTrader Improvement Backlog

**Status:** Review artifact  
**Scope:** Prioritized architecture backlog, no implementation in this phase  
**Last updated:** 2026-03-15

## Purpose
This backlog translates the review conclusions into phased work that another engineer or agent can execute later without re-deciding the architecture.

## Priority Model
- `P0`: required to reduce architectural ambiguity
- `P1`: required to improve maintainability and future feature velocity
- `P2`: required to unlock larger-scale decoupling or reliability
- `P3`: long-term hardening and scale-up work

## Phase A: Contract and Documentation Cleanup
### Objectives
- lock backbone ownership
- remove ambiguity about boundaries
- create explicit future contracts before refactors begin

### Work items
1. `P0` Adopt the review docs as the architecture-review baseline for future refactors.
2. `P0` Add explicit cross-references from canonical docs to the architecture-review set where helpful.
3. `P0` Define a migration policy note replacing startup schema reconciliation as the long-term target.
4. `P0` Define a canonical data-access policy for DB vs curated dataset vs provider cache.
5. `P0` Define a formal event taxonomy draft for market, order, strategy, risk, backtest, and system events.
6. `P1` Define symbol/lifecycle resolution contracts with provider-aware and date-aware method signatures.
7. `P1` Define backtest reproducibility requirements and run metadata requirements.

### Acceptance
- no backbone domain remains ambiguous
- docs clearly classify current behavior vs target behavior
- future refactors can cite a stable contract source

## Phase B: Service Boundary Refactors
### Objectives
- reduce feature-local data logic
- reduce direct session sprawl
- make data and orchestration boundaries explicit

### Work items
1. `P1` Introduce repository/service boundaries for repeated DB access patterns instead of widespread direct `SessionLocal()` and `get_db_session()` usage.
2. `P1` Move remaining high-traffic data paths onto canonical history/universe/symbol services.
3. `P1` Replace startup-time schema backfill reliance with a migration workflow.
4. `P1` Standardize dataset readers so full-file reads are no longer the default access pattern for growing datasets.
5. `P1` Remove remaining feature-owned coupling between strategy logic and storage shape.
6. `P1` Define durable execution-state models for long-running operations before changing execution engines.

### Acceptance
- feature code uses backbone services instead of local ownership patterns
- DB access is more centralized and measurable
- storage shape decisions are no longer embedded in feature services

## Phase C: Event Model Introduction
### Objectives
- introduce decoupling without breaking current public APIs
- convert direct multi-hop service coordination into internal event contracts

### Work items
1. `P2` Finalize internal event categories, schemas, and ownership.
2. `P2` Define publisher/subscriber boundaries for:
   - market data
   - order lifecycle
   - position lifecycle
   - strategy signals
   - backtest lifecycle
   - operational health
3. `P2` Define which events are transient and which must be durable.
4. `P2` Make websocket fanout a downstream consumer of internal events in the target design.
5. `P2` Define replay and audit expectations before choosing event infrastructure.
6. `P2` Define event-driven projections for dashboards/reporting instead of direct cross-module dependencies.

### Acceptance
- event-bus adoption path is explicit and staged
- each subsystem knows whether it is a publisher, subscriber, or both
- websocket is no longer the implied coordination backbone in the target design

## Phase D: Orchestration and Performance Hardening
### Objectives
- improve scale, operability, and latency predictability
- remove request-thread ownership of compute-heavy flows

### Work items
1. `P2` Establish endpoint-level and workload-level performance benchmarks.
2. `P2` Add operational metrics for websocket queues, drops, reconnects, and fanout latency.
3. `P2` Add benchmark coverage for scan duration, backtest runtime, and dataset read latency.
4. `P2` Move compute-heavy scans and backtests onto durable orchestration boundaries.
5. `P3` Introduce replayable event-backed audit for order, position, strategy, and backtest lifecycle events.
6. `P3` Revisit storage and caching strategy after actual benchmark data is available.

### Acceptance
- hotspots are measured rather than guessed
- long-running workflows have durable lifecycle tracking
- realtime and analytical workloads have explicit operational metrics

## Domain-Specific Hotspot Backlog
### Database management
- `P1` Replace schema drift handling at startup with explicit migrations.
- `P1` Reduce service-local session creation patterns.
- `P1` Formalize repository ownership for high-read domains.
- `P2` Review query counts and indexes for screener, terminal, market, and strategy endpoints.

### WebSocket management
- `P1` Preserve current API contract while documenting ingress/fanout stages.
- `P2` Add metrics around queue overflows, dropped clients, and broadcast latency.
- `P2` Define websocket as a consumer of internal events rather than the coordination center.

### Event bus
- `P2` Define event taxonomy and audit requirements first.
- `P3` Introduce durable eventing only after contracts and operational needs are locked.

### Performance improvement
- `P1` Inventory request-path ORM hotspots.
- `P1` Inventory full-file parquet read paths.
- `P2` Add benchmark harnesses for backtests, scans, and realtime fanout.

### Backtesting
- `P1` Define durable run lifecycle and remove in-memory-only assumptions.
- `P2` Separate orchestration from request handling.
- `P2` Align research/runtime parity contracts across symbols, universes, and history sources.

### Symbol management
- `P1` Expand contract coverage for lifecycle-aware resolution by date.
- `P1` Define provider-agnostic mapping requirements beyond FYERS.
- `P2` Add a formal normalization test matrix for aliases, indices, lifecycle transitions, and series changes.

## Benchmark and Measurement Plan
### Required benchmarks
- request latency for screener, terminal, websocket status, strategy status, and backtest endpoints
- DB query count for representative requests
- dataset load latency for daily history and index history
- websocket batch size and fanout latency
- VCP scan runtime and memory usage
- backtest runtime and memory usage for representative symbol and universe scopes

### Required observability outputs
- endpoint latency dashboard or report
- websocket health counters
- backtest and scan execution timing logs
- data-load timing by source layer

## Defaults and Guardrails
1. No public API changes are assumed in Phase A.
2. No event infrastructure choice is assumed in this backlog.
3. No multi-provider implementation is assumed yet, but provider-agnostic contracts must be used.
4. No new feature scope should be bundled with backbone refactors.
5. Every implementation phase must update canonical docs when contracts change.
