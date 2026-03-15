# SmartTrader Target Backbone

**Status:** Review artifact  
**Scope:** Target architecture and contracts, no implementation authorized  
**Last updated:** 2026-03-15

## Purpose
This document defines the target backbone architecture SmartTrader should converge toward to make future development leaner without changing current runtime behavior in this review phase.

## Target Principles
1. Backbone concerns are feature-agnostic.
2. Provider adapters must not become feature adapters.
3. Realtime distribution, state changes, and long-running orchestration should converge on explicit internal contracts.
4. Historical data access must be reproducible and separated from runtime state.
5. Symbol and universe resolution must remain central and lifecycle-aware.

## 1. Database and Storage Backbone
### Target ownership split
- Transactional/runtime DB:
  - orders
  - positions
  - configs
  - universe definitions/history
  - symbol lineage/mappings
  - backtest/strategy run metadata
  - audit metadata and operational state

- Canonical analytical datasets:
  - daily equity history
  - adjusted history
  - benchmark/index history
  - snapshots
  - reproducible backtest inputs

- Provider caches:
  - raw or lightly normalized provider data used for refresh support or short-term supplementation

- Archive-only data:
  - non-canonical intraday research archives
  - legacy or exploratory outputs

### Target operating rule
- Features read runtime state from DB.
- Features read analytical history through canonical history services.
- Features do not read provider caches or raw source files directly.
- Archive data is not part of default feature/runtime paths.

### Required internal contracts
- `load_price_history(symbols, timeframe, start_date, end_date, adjusted, source_policy)`
- `load_index_history(universe_id, start_date, end_date)`
- `resolve_universe(universe_id, as_of_date)`
- `resolve_symbols(universe_id, as_of_date)`

### Future governance requirements
- explicit migration policy instead of startup schema drift repair
- repository/service boundaries around DB access
- retention and promotion rules for source, cache, curated, and archive lanes

## 2. WebSocket Backbone
### Target internal topology
1. Provider ingress
2. normalization and validation
3. internal event publication
4. websocket fanout adapter
5. frontend state adapter

### What stays stable
- `POST /api/websocket/connect`
- `POST /api/websocket/subscribe`
- `POST /api/websocket/disconnect`
- `GET /api/websocket/status`
- `WS /api/websocket/stream`

### Target internal rules
- websocket is a distribution surface, not the system coordination backbone
- symbol normalization happens before publication
- client fanout is downstream of internal events
- batching and slow-client handling remain explicit
- dropped messages and disconnects become observable metrics

### Required observability
- inbound tick rate
- batch size distribution
- dropped-client count
- queue overflow count
- reconnect attempts and reasons
- provider connection state timeline

## 3. Event Bus Backbone
### Target role
The event bus is the future internal coordination backbone. It sits between publishers and subscribers and removes direct module-to-module coupling where operational flows cross subsystem boundaries.

### Target event categories
- market data events
- strategy signal events
- order lifecycle events
- position lifecycle events
- risk decision events
- backtest lifecycle events
- data refresh and publication events
- system health and connectivity events

### Initial publisher model
- provider ingress services
- strategy engines
- order execution and broker sync services
- position sync services
- backtest orchestrators
- data refresh pipelines

### Initial subscriber model
- websocket fanout
- audit/logging
- dashboards and reporting
- risk services
- strategy state projections
- reconciliation services
- future ML/analytics consumers

### Durability model
- in-memory transient events for low-latency fanout may exist
- durable event storage is required for:
  - backtest lifecycle
  - order and position lifecycle
  - strategy signal audit
  - operational replay/debugging

### Ordering and replay expectations
- ordering must be explicit per event category
- replay is required for operational/debug categories before event bus adoption is considered complete
- websocket replay is optional; internal audit replay is not

## 4. Performance Backbone
### Quick-win target
- reduce repeated direct ORM query patterns in request paths
- document hot paths and expected SLAs
- standardize cached vs uncached history access
- measure websocket queue pressure and broadcast latency

### Medium-term target
- central repository/service boundaries for DB access
- partition-aware or predicate-aware dataset readers
- background orchestration for compute-heavy scans and backtests
- remove feature-local data loading logic

### Deep-change target
- event-driven orchestration
- durable job lifecycle and projections
- richer analytical serving model for large history workloads

### Required measurement points
- request latency by endpoint family
- DB query count for high-traffic endpoints
- websocket broadcast latency and drops
- backtest runtime and memory profile
- strategy scan duration and memory profile

## 5. Backtesting Backbone
### Target operating model
- historical data comes from canonical history services only
- run metadata persists independently of compute execution
- orchestration is durable and not request-thread-owned
- benchmark handling is explicit and reproducible
- universe resolution is date-aware and survivorship-bias-ready

### Target contracts
- `BacktestRunRequest`
- `BacktestRunMetadata`
- `BacktestExecutionState`
- `BacktestResultPayload`
- `BenchmarkPolicy`
- `UniverseResolutionPolicy`

### Required guarantees
- reproducible data source and date range
- explicit benchmark source
- explicit universe resolution mode
- strategy version and parameter capture
- durable status progression without in-memory-only fallback

### Research/runtime parity rule
- research backtests and runtime strategies must consume the same symbol, universe, and history contracts even if orchestration differs

## 6. Symbol Backbone
### Target role
`symbol_master` remains the single normalization boundary between:
- inbound UI/API identifiers
- internal canonical identity
- provider-specific identifiers

### Target contract
- `resolve_symbol(symbol, provider, as_of_date)`
- `map_symbol_to_provider(symbol, provider, as_of_date)`
- `map_provider_symbol_to_db(provider_symbol, provider, as_of_date)`
- `get_symbol_lineage(symbol, as_of_date)`

### Required capabilities
- canonical validation
- provider mapping by provider and series
- lifecycle-aware resolution by date
- index alias handling
- display formatting policy
- unresolved-symbol handling with explicit failure state

### Current default preserved
- DB format remains the canonical internal symbol identity
- DISPLAY format remains aligned with DB format unless a future documented override is introduced centrally

## Target Migration Shape
### Phase 0
- keep current runtime behavior
- define contracts and ownership only

### Phase 1
- remove feature-specific data access and local coupling
- stabilize DB/history/symbol/universe contracts

### Phase 2
- introduce event taxonomy and internal publication/subscription boundaries

### Phase 3
- move orchestration-heavy flows onto durable lifecycle management

### Phase 4
- add replay, richer observability, and operational hardening

## Non-Goals of This Target Note
- no broker selection for event-bus technology
- no Redis/Kafka/stream implementation choice
- no schema migration design beyond policy direction
- no API changes in this review phase
