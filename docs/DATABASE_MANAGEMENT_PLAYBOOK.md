# SmartTrader Database Management Playbook

**Status:** Canonical
**Last updated:** 2026-03-15

## Purpose
This document defines the feature-agnostic data backbone of SmartTrader:

- database management
- universe management
- symbol management
- symbol lifecycle management
- provider ingestion policy

No feature may redefine these rules locally.

## Design Objective
The platform must remain lean, accurate, and pluggable.

Future features should be able to consume:
- universes
- symbols
- lifecycle-aware symbol identity
- daily and index history
- optional intraday archives

without introducing new one-off data models or provider-specific shortcuts.

## Canonical Root
- `data_system/`

## Canonical Layers
1. Raw source layer
- `data_system/01_sources/`
- Provider and exchange source artifacts only.
- No feature-owned files.

2. Staging and normalization layer
- `data_system/02_*` is intentionally unused today and reserved for future transient landing or extraction workspaces if a source requires a pre-normalization step.
- The active canonical tree currently skips `02_` rather than assigning it ad hoc, so new datasets should not invent `02_*` paths without a playbook update.
- `data_system/03_staging/`
- `data_system/03_universe/`
- Parsed universe inputs, normalized reference files, staging transforms.

3. Curated canonical layer
- `data_system/04_curated/phase1/`
- Stable, feature-consumable datasets.

4. Metadata and audit layer
- `data_system/05_metadata/phase1/`
- Manifests, validation, checksums, anomalies, lineage.

5. Archive layer
- `archive/`
- `archive/` lives outside the numbered canonical hierarchy on purpose; it is not a numbered production layer and should not be treated as a canonical feature dependency.
- Non-canonical, exploratory, legacy, or optional archives such as intraday bulk dumps.

## Phase Definitions
- `phase1` in paths such as `data_system/04_curated/phase1/` and `data_system/05_metadata/phase1/` is a dataset contract and operating-lane label, not a feature maturity score or UI roadmap tag.
- `phase1` currently means: production-backed daily equity, universe, and metadata datasets that have a defined storage contract, validation flow, and consumer-facing service path.
- No additional active dataset phases (`phase2`, `phase3`, etc.) exist today in the canonical tree.
- If future phases are introduced, they should mean a distinct published contract or operating lane, not a vague "next version" bucket.

Promotion criteria for moving a dataset into a new phase or from staging/archive into `phase1`:
- schema and contract documented in the playbook and owning docs
- validation checks and anomaly handling implemented
- reproducible build/refresh path with manifests and lineage
- minimum data quality/SLA expectations defined for the intended consumers
- review sign-off from the platform/data-owner change that promotes the dataset

## Backbone Domains
### 1. Database Management
Owns:
- storage contracts
- build/publish flow
- validation
- DB sync rules
- retention and archive rules

Features consume database artifacts; they do not define them.

### 2. Universe Management
Owns:
- `index_universe_definitions`
- `index_constituents_history`
- `index_weightage_changes`
- `universe_snapshots`
- monthly universe raw inputs
- weightage-derived membership logic

Required capabilities:
- live lookup by latest active membership
- historical lookup by effective date
- explicit fallback mode when historical coverage is unavailable

### 3. Symbol Management
Owns:
- provider symbol normalization
- display vs DB vs provider formats
- provider mapping metadata

All provider conversions must go through `symbol_master`.

`symbol_master` is the canonical normalization service/module, implemented in `backend/app/services/symbol_master.py`; it is not a standalone database table in the current architecture.
Its interface contract is service-based:
- `to_db(symbol: str) -> str`
- `to_fyers(symbol: str) -> str`
- `to_display(symbol: str) -> str`
- `get_info(symbol: str) -> SymbolInfo`

Current `SymbolInfo` record shape:
- `ticker: str`
- `exchange: str`
- `series: str`
- `company_name: str`
- `sector: str`
- `isin: str`
- `lot_size: int`
- `tick_size: float`
- `indices: list[str]`

Example:
```python
symbol_master.get_info("NSE:SBIN-EQ")
# -> SymbolInfo(ticker="SBIN", exchange="NSE", series="EQ", ...)
```

`symbol_master` sits between canonical DB identity and provider adapters. All provider conversions and UI display conversions must call the `symbol_master` API rather than bypass it with string parsing.

### 4. Symbol Lifecycle Management
Owns:
- renames
- mergers
- demergers
- delistings
- series changes
- successor/predecessor mapping

Lifecycle rules must be queryable by date and must remain outside feature code.

## Canonical Data Lanes
### A. Universe / Index Lane
Purpose:
- historical universe composition
- weight history
- benchmark/index backtests

Canonical inputs:
- `data_system/01_sources/fyers_index_prices/universe_index_price_daily.parquet`
- `data_system/01_sources/nse_index_weights_pdf/**`
- `data_system/03_universe/monthly_universe_raw/<month>/<universe>.csv`
- `data_system/03_universe/constituents/*.csv`

Canonical outputs:
- `data_system/04_curated/phase1/<universe>_weights_monthly.parquet`
- `data_system/04_curated/phase1/<universe>_membership_daily.parquet`
- `data_system/04_curated/phase1/snapshot_<universe>_daily.parquet`

### B. Equity Daily Lane
Purpose:
- strategy scans
- daily backtests
- screener metrics
- technical calculations

Canonical inputs:
- `data_system/01_sources/nse_bhavcopy/*.csv`
- `data_system/01_sources/nse_corporate_actions/*.csv`
- optional supplemental provider cache:
  - `data_system/01_sources/fyers_stock_prices/stock_price_daily.parquet`

Canonical outputs:
- `data_system/04_curated/phase1/equity_ohlcv.parquet`
- `data_system/04_curated/phase1/equity_ohlcv_adj.parquet`
- `data_system/04_curated/phase1/snapshot_stock_daily.parquet`

Rule:
- curated equity outputs are the canonical daily stock interface for features

### C. Intraday Archive Lane
Purpose:
- future intraday research
- optional intraday strategies
- optional execution simulation support

Rule:
- intraday data is non-canonical-but-supported
- it is not part of the default canonical Phase-1 daily feature path
- intraday archives must live outside the core curated lane unless explicitly promoted by a documented phase change

Storage rule:
- do not keep long-term intraday archives as thousands of per-symbol shard files
- consolidate by stable contract, then archive under `archive/`

## Source Policy
1. Monthly universe and weight logic must remain authoritative for historical universe composition.
2. Daily stock feature logic should consume canonical daily datasets, not raw provider shards.
3. Provider caches are allowed as source supplements, but not as feature-owned truth.
4. Any survivorship-bias choice must be an explicit runtime policy, not a hidden data shortcut.

## Provider Policy
Providers are adapters, not feature implementations.

### Approved long-term FYERS script responsibilities
- refresh index daily cache
- refresh equity daily cache
- refresh optional non-canonical intraday archive
- consolidate optional provider archives before retention

### Approved FYERS entrypoints
- `backend/scripts/refresh_fyers_equity_daily.py`
- `backend/scripts/refresh_fyers_index_daily.py`
- `backend/scripts/refresh_fyers_intraday_archive.py`
- `backend/scripts/consolidate_fyers_intraday_archive.py`

### Disallowed long-term patterns
- scripts named after a single feature
- scripts named after a single hardcoded universe unless they are temporary migration utilities
- scripts that choose storage layout based on one strategy's needs

### FYERS script contract requirements
Every maintained FYERS script should support some combination of:
- `--universe`
- `--symbols`
- `--timeframe`
- `--range-from`
- `--range-to`
- `--refresh-policy`
- `--output-contract`

## Database / Dataset Boundary
### Database should store
- normalized metadata
- universe definitions/history
- symbol history / lifecycle
- transactional runtime state
- feature results
- daily `historical_prices` when required for query performance and runtime APIs

### Curated parquet datasets should store
- bulk analytical history
- snapshots
- publication-ready historical datasets
- reproducible backtest inputs

### Rule
Features must not have to guess whether to read DB or parquet.

The intended access pattern is:
- metadata and runtime state: DB
- bulk analytical history and snapshots: curated datasets
- provider raw dumps: source layer only

## Coverage Semantics
1. Price coverage gap is not the same as membership gap.
2. Missing price data must remain explicit.
3. Membership must be date-effective where historical coverage exists.
4. If pre-history fallback is used, it must be labeled clearly as indicative.

## Future Survivorship Bias Readiness
Even when a feature temporarily ignores survivorship bias, the backbone must preserve:
- date-effective universe membership
- date-effective symbol lineage
- explicit current-membership fallback mode

That allows future survivorship-bias-aware features without redesigning the storage model.

## Canonical Service Interfaces
The platform should converge on stable service contracts like:
- `resolve_universe(universe_id, as_of_date)`
- `resolve_symbols(universe_id, as_of_date)`
- `resolve_symbol(symbol, provider, as_of_date)`
- `get_symbol_lineage(symbol, as_of_date)`
- `load_price_history(symbols, timeframe, start_date, end_date, adjusted, source_policy)`
- `load_index_history(universe_id, start_date, end_date)`

New features should plug into these services rather than create local data rules.

## Build / Maintenance Rules
1. Keep active runtime/build paths on canonical `data_system/` assets only.
2. Move experimental or legacy outputs to `archive/`.
3. Consolidate provider bulk outputs before keeping them long-term.
4. Keep source manifests and validation current whenever source contracts change.
5. Update docs in the same change when database/universe/symbol contracts change.

## Current Alignment Notes
1. `data_system/01_sources/fyers_index_prices/*` is canonical for direct index daily series.
2. `data_system/01_sources/fyers_stock_prices/*` should be treated as a provider daily cache, not a feature-owned truth layer.
3. `data_system/01_sources/fyers_intraday_5min/*` should be treated as optional intraday archive material and consolidated into `archive/` if retained.
4. Strategy modules like VCP must consume the backbone through universe, symbol, and daily history services rather than carrying their own data model.
