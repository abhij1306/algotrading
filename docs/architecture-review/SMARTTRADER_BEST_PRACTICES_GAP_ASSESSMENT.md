# SmartTrader Best-Practices Gap Assessment

**Status:** Review artifact  
**Scope:** Architecture comparison and improvement planning only  
**Last updated:** 2026-03-15

## Purpose
This document compares SmartTrader's current architecture against:

- the current SmartTrader codebase and canonical docs
- `marketcalls/openalgo`
- `marketcalls/historify`
- `marketcalls/vectorbt-backtesting-skills`
- the event-bus best-practice screenshots supplied for this exercise

This is not a feature proposal and does not authorize runtime, schema, or API changes by itself.

## Reference Baseline
External reference patterns used in this review:

- `openalgo`: unified broker/data boundary, operational API service, historical-data integration point
- `historify`: historical data management, analytical storage, scheduled refresh, bulk-data operations
- `vectorbt-backtesting-skills`: strategy research/backtest layer over externally managed historical data
- event-bus screenshots: pub/sub decoupling, replayability, auditability, subscriber independence

Current SmartTrader sources considered authoritative for current state:

- `docs/ARCHITECTURE.md`
- `docs/Websocket.md`
- `docs/DATABASE_MANAGEMENT_PLAYBOOK.md`
- `docs/SYMBOL_MANAGEMENT.md`
- `backend/app/database.py`
- `backend/app/services/live_market_service.py`
- `backend/app/utils/ws_manager.py`
- `backend/app/services/history_service.py`
- `backend/app/services/backtest_phase1_service.py`
- `backend/app/services/vcp_service.py`

## Comparison Matrix
| Domain | Current SmartTrader State | Reference Pattern | Primary Gap | Classification | Recommended Direction |
|---|---|---|---|---|---|
| Database management | Split across SQLAlchemy transactional DB and `data_system/` datasets, with some startup-time schema reconciliation and many direct session call sites | OpenAlgo/Historify separate operational APIs from historical data management and analytical storage | Storage boundary exists but enforcement and operational discipline are incomplete | `refactor later` | Make DB/dataset ownership stricter, move from startup backfills to managed migration policy, and centralize history access |
| WebSocket management | Provider WS -> `LiveMarketService` -> `ws_manager` -> frontend hook, with batching and bounded client queues | OpenAlgo-style service boundary plus general pub/sub reliability patterns | Good local batching, but realtime remains tightly coupled to direct service flow with weak observability/replay | `tighten/document` | Preserve current public API while formalizing ingress, fanout, metrics, and future event-bus handoff |
| Event bus | No internal event bus; direct service coordination and polling dominate | Screenshot reference uses bus as the central operational backbone | Current architecture is still service-call centric | `major future architecture change` | Introduce a future internal event model and migration path before any broker selection |
| Performance | Known hotspots in ORM usage, parquet full reads, pandas-heavy synchronous compute, and request-time orchestration | Historify-style data management and event-driven separation reduce contention and repeated work | Performance risks are known but not yet organized into a platform backlog | `refactor later` | Establish hotspot inventory, benchmarks, and staged optimization plan |
| Backtesting | Canonical daily history now exists, but orchestration is still synchronous and service-owned with partial in-memory fallback | VectorBT/OpenAlgo/Historify separate historical data, research tooling, and orchestration | Backtest data contract is improving, orchestration contract is still thin | `refactor later` | Define durable run lifecycle, reproducibility contract, and research/runtime parity rules |
| Symbol management | `symbol_master` is centralized and documented, with lifecycle direction present but still service-centric | OpenAlgo-style unified broker/data boundary expects canonical symbol resolution and provider mappings | Strong foundation, but lifecycle and provider extensibility are not yet full platform contracts | `keep as is` plus `tighten/document` | Keep central normalization, expand lineage and provider contract rigor before multi-provider growth |

## Domain Review
### 1. Database Management
**Current strengths**

- SmartTrader already distinguishes transactional state from analytical datasets.
- The database playbook now defines canonical lanes for daily, index, and intraday archive data.
- A shared daily history adapter exists, reducing feature-specific file reads.

**Current risks**

- `backend/app/database.py` still performs startup-time schema reconciliation for backtest, universe, and strategy tables. This is operationally convenient but weak as a long-term migration model.
- Many services still create sessions directly with `SessionLocal()` or `get_db_session()` rather than using stable repository boundaries.
- Dataset access is more disciplined than before, but parquet access still depends on service-local caching and full-file reads.
- The transactional DB and analytical parquet boundary is documented but not yet fully enforced by architecture.

**Reference comparison**

- OpenAlgo behaves more like an operational service boundary.
- Historify behaves more like a historical data management layer with explicit bulk-data concerns.
- SmartTrader currently spans both concerns inside one application, which is workable, but it requires stronger internal contracts than it has today.

**Recommended target state**

- Keep the current DB plus dataset split.
- Treat PostgreSQL/SQLite as the runtime and metadata store only.
- Treat `data_system/04_curated/phase1/` as the canonical analytical history layer.
- Treat provider source files as caches or raw inputs only.
- Replace startup schema drift handling with an explicit migration policy in a future refactor.

**Classification**

- `tighten/document` for current ownership rules
- `refactor later` for session management and migration discipline

### 2. WebSocket Management
**Current strengths**

- SmartTrader already normalizes symbols at ingress and uses aggregate subscription deltas.
- `LiveMarketService` batches outbound ticks every 200ms.
- `ws_manager` has per-client queues and disconnects slow clients.
- The frontend hook already handles hidden-tab buffering and reconnect logic.

**Current risks**

- Realtime still couples provider ingestion, normalization, buffering, and UI broadcast in one linear service flow.
- There is no durable tick/event log, replay path, or structured realtime audit trail.
- The current WebSocket system is reliable enough for a single-source setup, but it is not yet a platform-ready pub/sub backbone.
- Queue dropping protects the server but is not yet observable through metrics or operational reporting.

**Reference comparison**

- OpenAlgo provides a stronger service boundary around broker/data connectivity.
- The event-bus pattern from the screenshots shows how websocket distribution should eventually become just one subscriber, not the center of coordination.

**Recommended target state**

- Preserve the current public websocket endpoints and message contract.
- Formalize realtime as three internal stages:
  - provider ingress and normalization
  - internal event distribution
  - websocket/UI fanout
- Add future observability requirements:
  - dropped-client counts
  - batch sizes
  - provider reconnect counters
  - inbound/outbound tick rates

**Classification**

- `keep as is` for the current public API
- `tighten/document` for current operational flow
- `refactor later` for internal decoupling

### 3. Event Bus
**Current strengths**

- The need is visible in current architecture boundaries: live ticks, strategy state, backtest run state, and position/order workflows already represent event-like flows.
- The codebase already has natural publisher/subscriber candidates even though they are not yet modeled as such.

**Current risks**

- Strategy, DB state, websocket fanout, and reconciliation paths still rely on direct service calls or polling.
- No canonical event taxonomy exists.
- No durable replay or audit-oriented event stream exists for operational debugging.

**Reference comparison**

- The screenshot pattern is directly relevant: strategy, risk, OMS, reporting, console, and ML consumers should not depend on direct function-call webs.
- SmartTrader today resembles the “before” side more than the “after” side.

**Recommended target state**

- Introduce a future internal event bus as a backbone service, not a feature service.
- Use it first as an internal contract and event taxonomy before selecting any technology.
- Make websocket fanout, reporting, strategy audit, and backtest lifecycle consumers of the same event model over time.

**Classification**

- `major future architecture change`

### 4. Performance Improvement
**Current strengths**

- Realtime batching already exists.
- `history_service` caches source datasets in memory per process.
- The frontend websocket hook avoids excessive rerenders through callback registration.

**Current risks**

- Direct ORM usage is widespread in services and routers.
- Some request paths are still query-heavy and service-local.
- Daily history loading still relies on full parquet loads rather than partition-aware or predicate-pushdown reads.
- Backtests and VCP scans remain synchronous pandas-heavy compute in the request/service layer.

**Reference comparison**

- Historify-style bulk historical management suggests a stronger separation between refresh/storage concerns and consumer workloads.
- VectorBT-style research stacks assume cleaner data-serving and compute boundaries than SmartTrader currently enforces.

**Recommended target state**

- Organize optimization work into:
  - quick wins: query cleanup, endpoint hot-path measurement, cache boundaries
  - medium-term refactors: repository boundaries, dataset readers, orchestration separation
  - deep changes: event-driven pipelines, background execution, analytical-serving patterns

**Classification**

- `refactor later`

### 5. Backtesting
**Current strengths**

- SmartTrader now has a canonical daily history service instead of direct feature-owned source file reads.
- Universe and symbol backbone docs already support survivorship-bias-aware evolution later.
- Backtest runs persist results in DB.

**Current risks**

- Backtest execution is still synchronous and service-owned.
- Run lifecycle still falls back to in-memory job storage in some cases.
- There is no durable queue abstraction or event-driven run lifecycle.
- Research/runtime parity is improving but still not fully explicit as a platform contract.

**Reference comparison**

- VectorBT/backtesting-skills shows a cleaner separation between research logic and data source.
- OpenAlgo/Historify show that historical data management should not be owned by the strategy layer.
- SmartTrader is directionally aligned but not yet fully separated.

**Recommended target state**

- Keep the canonical history contract.
- Define backtesting as a backbone with:
  - stable input contracts
  - stable run metadata
  - reproducibility and benchmark rules
  - durable orchestration boundary
  - event-friendly lifecycle
- Eliminate in-memory job storage fallback:
  - adopt a durable job store or queue for run lifecycle management
  - persist run state transitions and backtest results for restart and recovery
  - add tests and operational checks for concurrent orchestration and failure recovery

**Classification**

- `refactor later`

### 6. Symbol Management
**Current strengths**

- `symbol_master` is already centralized and enforced in docs.
- Provider and UI conversions are explicitly routed through a single service.
- Index alias handling and display policy are clearer than before.

**Current risks**

- Lifecycle resolution is not yet a universally applied platform contract across all future provider scenarios.
- The current implementation is strongly FYERS-oriented even though the docs are provider-agnostic.
- Future multi-provider support would require stronger provider mapping metadata and lineage interfaces.

**Reference comparison**

- OpenAlgo-style unified broker/data boundaries reinforce the need for strict symbol normalization at every provider edge.
- SmartTrader already has the right center of gravity here; it mainly needs maturity and test coverage, not a rewrite.

**Recommended target state**

- Keep `symbol_master` as the normalization boundary.
- Expand it into a broader symbol platform contract for:
  - lifecycle-aware resolution by date
  - provider mapping by provider and series
  - canonical validation and unresolved-symbol handling

**Classification**

- `keep as is` for the central service pattern
- `tighten/document` for future provider and lifecycle contracts

## Overall Conclusions
1. SmartTrader already has the right backbone direction for database, symbol, and historical data management.
2. The biggest architecture gap is not storage; it is internal operational decoupling, especially around event flow, orchestration, and observability.
3. Websocket and backtest systems are serviceable for current scale but should be treated as transition-state architectures.
4. The external repos are best used as pattern references:
   - OpenAlgo for operational boundary thinking
   - Historify for historical data management discipline
   - VectorBT examples for research/runtime separation
5. The event-bus screenshots describe the most important future step for reducing feature coupling and making future development leaner.
