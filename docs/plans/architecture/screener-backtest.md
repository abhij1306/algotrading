# Architecture improvement plan: screener / strategies / backtest / universe / data

Scope: the task changed from a bug hunt to an architecture plan. `git diff` showed no uncommitted edits in `routers/screener.py`. Findings come from import-graph greps plus reading the files named. Paths are relative to `backend/` unless noted. No repo file was edited.

## Ranking (value / effort)

| # | Proposal | Value | Effort |
|---|----------|-------|--------|
| 1 | Delete dead modules and scripts | high | S (1-3 PRs) |
| 2 | Single universe service (collapse 4 implementations) | high | M (3 PRs) |
| 3 | One backtest model; remove stub engine/strategy trees | high | M (4 PRs) |
| 4 | Split `vcp_service.py` into a package | high | M (4 PRs) |
| 5 | Shared indicators module | medium | S-M (2 PRs) |
| 6 | Move business logic out of routers | medium | M (3-4 PRs) |
| 7 | Data access layering | medium-high | L (4 PRs) |
| 8 | Testability seams | medium | folded into the above |

## Severe issues hit on the way (flagged, not hunted)

- `app/services/vcp_service.py:1000-1005` (`run_backtest`): the universe is resolved once at `end_date` and applied to the whole range. Survivorship bias: names dropped from the index never appear, and names added later are traded before they joined. Fix: call `_constituents_for_date` per trading date (or per rebalance month); load the union of symbols once and filter signals by membership at that date.
- `app/engines/backtest_engine.py:411-414`: `_fetch_daily_prices` returns `{}` ("Simplified: return empty for now"), and `_rebalance` runs before any price is fetched. No position is ever opened, so the engine can only emit a flat curve. Nothing calls it, so it is harmless today but should not look usable (proposal 3).
- `app/strategies/black_scholes.py:506-527` (`calculate_time_to_expiry`): divides wall-clock hours (overnight and weekends included) by 6.25*252 trading hours, overstating time to expiry roughly 2-3x for multi-day expiries. `get_next_weekly_expiry` (`:495`) also sends a Thursday to the following week, so same-day expiry is never priced. Only matters if the ORB option path is revived; `option_chain_service` uses only IV and greeks.
- `app/strategies/performance_metrics.py:11` does `from .backtest_engine import Trade`, but `app/strategies/backtest_engine.py` does not exist. The module cannot be imported.

---

## 1. Delete dead code (quickest win)

**Current state** (import graph checked over `app/`, `tests/`, `scripts/`, `../scripts`, `../data_platform`):
- `app/excel_parser.py`, `app/data_cache.py`, `app/cache_manager.py`: no importers. `data_cache.py` and `cache_manager.py` each define a class named `DataCache`. The cache actually in use is `utils/cache.py` (only `position_sync_service`).
- `app/models.py` (pydantic `StockFeatures`, `ScreenerResponse`, `HealthResponse`): shadowed by the `app/models/` package, so it can never be imported; nothing references those names.
- `app/strategies/*` (`orb_strategy`, `base_strategy`, `atr_utils`, `performance_metrics`): the only external consumer is `services/option_chain_service.py:13`, which imports only `strategies.black_scholes`. `strategies/__init__.py` eagerly imports `orb_strategy`, so importing `black_scholes` drags the dead tree along.
- `app/engines/strategies/*` (5 files), `engines/metrics_calculator.py`, `engines/data_provider.py`, `engines/base_strategy.py`, `engines/backtest_engine.py`: no importers other than `engines/__init__.py`. The only thing that imports `app.engines` is `routers/universe.py:16` (for `UniverseManager`), which loads all of the above via `engines/__init__`.
- `app/brokers/plugins/backtest.py` (`BacktestBroker`, 305 lines): no importers; only `plugins.fyers` is used.
- Scripts: `backend/scripts/refresh_fyers_daily_stock_data.py` and `download_fyers_nifty500_5min.py` are 14- and 20-line "deprecated wrapper" files; `backend/scripts/apply_indexes.py` duplicates `../scripts/performance/apply_indexes.py`; `add_categorization_columns.py` is a one-off migration that belongs under `migrations/`.
- Junk at repo and backend roots: `fyersApi.log`, `fyersRequests.log`, `backend/server.log`, `server2.log`, `test.db`, `backend/fyers*.log`. Check whether tracked; if so `git rm` and gitignore.

**Problem:** roughly 2k lines of plausible-looking code with no callers. They mislead humans and agents about which backtest and strategy stack is real, and one module cannot even be imported.

**Target:** only live code remains. `black_scholes.py` moves next to its consumer (e.g. `services/options/black_scholes.py`).

**Slices:**
1. Delete never-imported files: `excel_parser`, `data_cache`, `cache_manager`, `models.py`, `engines/strategies/`, `engines/metrics_calculator.py`, deprecated script wrappers, duplicate `apply_indexes`. Run ruff and the existing tests.
2. Move `black_scholes.py` and fix the `option_chain_service` import, then delete the rest of `app/strategies/`. If ORB is wanted later, revive it from git history on proposal 3's design.
3. `git rm` tracked logs and db files, update `.gitignore`; delete `brokers/plugins/backtest.py` unless proposal 3 keeps it as a fill simulator.

**Risk:** low. Re-grep for dynamic imports (`importlib`, string imports) before deleting; I found none. **Size:** S.

## 2. One universe service

**Current state: four implementations of "members of index X as of date D":**
- `services/index_universe_loader.py` (339 lines): CSV loader singleton, used by `main.py:147`, `symbol_master.py:35`, `universe_sync`, and `services/universe`. `get_symbols_by_date` (`:313`) ignores its date argument (`_target_date`).
- `services/universe/__init__.py` (392 lines): `UniverseServiceImpl` plus a `UniverseService` wrapper plus `get_universe_service()`. DB-backed with CSV fallback and four lazy imports of the loader. Used by the `screener`, `universe`, `market` routers and three scripts.
- `engines/universe_manager.py` (476 lines): `HistoricalUniverseManager` and `UniverseManager`, with its own `UniverseConstituent` dataclass duplicating the one in `services/universe`. Sole consumer is `routers/universe.py:16` (custom universe CRUD, `:291-333`); `get_universe_manager()` is unused.
- `services/vcp_service.py:82-134` (`_constituents_for_date`): a third point-in-time query with provenance labels (`bias_corrected`, `indicative_pre_*`, `live_fallback`). `engines/backtest_engine.py:202-259` has a fourth, and it ignores `effective_to`.
- `services/universe_sync.py` (194 lines) is the only writer (CSV to DB tables).

**Problem:** point-in-time semantics differ by call site (VCP honours `effective_to`, the engine and loader do not). The router advertises a 501 "historical not implemented" mode while VCP already does historical. Fixes land in one copy only, and `engines` is imported for one router class.

**Target design:** one package `services/universe/`:
- `repository.py`: DB queries only (`members_as_of(index_code, d)`, `latest`, `list_indices`).
- `service.py`: `UniverseService(repo)` owns the single `Member` dataclass and the single rule `effective_from <= d AND (effective_to IS NULL OR effective_to >= d)`. It returns a `Provenance` enum (`POINT_IN_TIME | INDICATIVE_PRE_HISTORY | LIVE_FALLBACK`, the strings VCP already emits).
- `sources/csv_loader.py` and `sync.py`: ingestion only (today's loader and `universe_sync`).
- `custom.py`: custom universe CRUD moved from `UniverseManager`.
- `get_universe_service()` stays the single public factory; the repository or session is injected, so tests use `UniverseService(repo=FakeRepo())`.

**Slices:**
1. Add `Member` and `Provenance`; move VCP's `_constituents_for_date` logic into `UniverseService.members_as_of`; VCP calls it. Pin with unit tests for the three provenance cases using the in-memory SQLite pattern from `tests/test_universe_sync.py`.
2. Move custom-universe code into `services/universe/custom.py`, repoint `routers/universe.py`, delete `engines/universe_manager.py` (and the `engines` package, with proposal 3).
3. Make `get_symbols_by_date` honour dates via the repository or delete it; move `universe_sync` and the loader under the package. Update the three scripts and the test import paths (all in-repo).

**Risk:** medium. Router responses feed `frontend/app/screener` and `frontend/app/backtest`; keep response models unchanged and add a contract test per endpoint. **Size:** M, about 3 PRs of 300-500 lines each, net negative LOC.

## 3. One backtest model

**Current state:**
- `services/backtest_phase1_service.py` (608 lines): the live one behind `routers/backtest.py`. Vectorised, three hard-coded strategies (`SUPPORTED_STRATEGIES`, `_build_signal` at `:122`), trade log, metrics (`_compute_metrics` `:194`), in-memory job store (`:532,569`), `BacktestRun` persistence (`_create_db_run`/`_finalize_db_run`). One class does allocation, data loading, signal logic, metrics, persistence and job handling.
- `services/vcp_service.py` `run_backtest` (`:992-1187`) and `_build_backtest_trade` (`:877`): a second, event-style backtest with its own metrics, curve and benchmark, writing the same `BacktestRun` table, exposed through `routers/strategies.py`.
- Dead: `engines/backtest_engine.py` (stub), `engines/base_strategy.py` and `engines/strategies/*` (`run_day` contract), `strategies/base_strategy.py` and `orb_strategy.py` (`on_data` contract), `brokers/plugins/backtest.py` (`IBroker` simulator).
- Metrics are computed in four places (phase1, VCP, `engines/metrics_calculator.py`, `strategies/performance_metrics.py`) with different conventions (rf 0 vs 6%, CAGR by `252/len` vs calendar days, Sortino by negative-only std).

**Problem:** the next strategy has no obvious home; the two live backtests can show inconsistent Sharpe or drawdown for equal curves; dead engines suggest an architecture that does not exist.

**Target:** `services/backtest/` package:
- `metrics.py`: one pure `compute_metrics(equity, trades, *, rf, periods_per_year)`; single home for Sharpe, Sortino, drawdown, CAGR conventions.
- `strategies/`: one `Strategy` Protocol (`signals(history, params) -> target weights by date`) and a registry. The three phase1 strategies become small classes; `SUPPORTED_STRATEGIES` derives from the registry. VCP keeps its event-driven runner but returns the same result type.
- `result.py` (`BacktestResult` with `to_api()`), `persistence.py` (one `BacktestRun` writer), `jobs.py` (job store behind a small interface so it can later move to DB or queue).
- `BacktestBroker` is kept only if simulated live-path fills are a stated goal; otherwise delete it.

**Slices:**
1. Extract `compute_metrics` from `phase1._compute_metrics` with golden-value tests; switch phase1 and VCP to it (document any number changes in the PR).
2. Extract `BacktestResult` and persistence; use from both services.
3. Strategy registry for phase1 (move the `_build_signal` branches into classes), with a no-look-ahead test per strategy.
4. Delete `engines/` and `strategies/` leftovers and rewire `__init__` files (the file deletions are proposal 1).

**Risk:** medium (UI numbers can change; check `frontend/app/backtest` types; `tests/test_backtest_benchmark.py` and `test_backtest_persistence.py` are the safety net). **Size:** M.

## 4. Split `vcp_service.py` (1228 lines, one class, six concerns)

**Current state (method map):**
- Config and regime: `_get_config`/`_set_config` (`:68,73`), `_asof_date` (`:146`), `get_regime` (`:154`), `RegimeSnapshot`.
- Universe: `_constituents_for_date` (`:82`), `_company_meta` (`:136`).
- Pure pattern analytics: `_percentile_rating`, `_compute_rs_map` (`:178`), `_stage2_metrics` (`:196`), `_pivot_points` (`:218`), `_has_no_overhead_resistance` (`:237`), `_analyze_pattern` (`:259`, about 175 lines). All static or pure.
- Scan and persistence: `run_scan` (`:485`), `get_latest_scan`, `get_signal_detail`, `_serialize_*`.
- Positions and risk: `queue_signal`, `cancel_signal`, `list_positions`, `close_position`, `update_stop`, `halt`, `resume`, `get_status` (`:682-858`); calls `fetch_fyers_quotes` directly.
- Backtest: `_daily_symbol_map`, `_next_trading_date`, `_build_backtest_trade`, `run_backtest` (about 195 lines), `get_backtest`, `list_backtests` (`:859-1228`).
- Module singleton `vcp_service`; methods open sessions internally via `get_db_session`/`db_session_scope`, so even pure logic is tested through a DB-bound class.

**Problem:** the most valuable logic (pattern detection) cannot be unit-tested without the service; the EMA-21 trailing logic is duplicated (`:657` and `:931`) so detail view and backtest can drift; review and agent edits on 1.2k lines are slow and risky.

**Target:** `services/vcp/` package:
- `config.py` (constants and a `VCPParams` dataclass passed in rather than read from module globals).
- `pattern.py`: pure DataFrame functions, `analyze_pattern(frame, rs_rating, params) -> PatternResult` (dataclass instead of dicts). No DB or I/O.
- `regime.py`, `scan.py` (`ScanService(universe, history, repo, clock)`), `positions.py` (`PositionService(repo, quotes, clock)`), `backtest.py` (uses `pattern.analyze_pattern` and a shared `trailing_stop`, returns proposal 3's `BacktestResult`, uses per-date membership).
- `__init__.py` keeps `vcp_service`, `DEFAULT_CAPITAL`, `DEFAULT_UNIVERSE` as a facade so `routers/strategies.py` is unchanged initially.
- Ports: `HistoryPort` (`load_equity_history`), `QuotePort`, `Clock`; real adapters wired in `__init__`, fakes in tests.

**Slices:**
1. Pure extraction: move the six pattern/RS functions into `vcp/pattern.py` unchanged, keep static shims on `VCPService`, add characterization tests on synthetic frames (no DB). About 400 lines moved, no behaviour change.
2. Extract positions and `get_status` into `positions.py` with an injected quote port.
3. Extract scan, then regime and config.
4. Extract backtest; ship the per-date universe fix as a separate, clearly labelled behaviour-change PR.

**Risk:** low for 1-3 (pure moves, facade kept), medium for 4. **Size:** M, 4 PRs.

## 5. Shared indicators

**Current state:** `indicators.py` (231 lines) has `ema`, `atr`, `rsi`, `zscore`, `compute_features` (about 125 lines; only importer is `data_repository.py:94`), and `calculate_macd/bbands/stoch/adx/obv` with no importers. Duplicates elsewhere:
- EMA via `ewm(span=...)`: `routers/terminal.py:282-283`, `backtest_phase1_service.py:131-132`, `vcp_service.py:657,931`, `engines/strategies/nifty_strategies.py:131`.
- Wilder ATR/ADX via `ewm(alpha=1/period)`: `services/market_data_service.py:36-42`.
- `strategies/atr_utils.py`: a rolling-mean ATR whose docstring says "Exponential", disagreeing with `indicators.atr`.

**Problem:** at least two ATR definitions (SMA and Wilder) across screener, VCP and intraday logic; EMA warm-up (`adjust`, `min_periods`) is not guaranteed identical across callers.

**Target:** `app/indicators/` (or keep the file) with pure vectorised functions and explicit documented conventions (Wilder vs SMA, `adjust=False`, `min_periods`). Callers: terminal router, phase1, VCP, `market_data_service`. Delete unused `calculate_*` or cover them with tests if the terminal will use them.

**Slices:** (1) golden-value tests for `ema/atr/rsi`; switch phase1 and terminal to `indicators.ema`. (2) Switch `market_data_service` ADX and VCP EMA/ATR to the shared functions; delete unused ones. **Risk:** low-medium (compare numerics on a fixture before and after). **Size:** S-M.

## 6. Business logic in routers

**Current state:**
- `routers/screener.py` (389 lines) holds a TTL LRU cache with lock (`:19-57`), the multi-subquery latest and previous-close SQL (`_build_screener_query`, `:84-150`), sorting (`:152,211`), quote hydration (`:177`), row serialisation (`:187-250`) and payload assembly (`_load_screener_payload`, `:251`). It imports `SessionLocal`, models, and `fetch_fyers_quotes` directly. For live sort fields (`price/change/volume`) it loads all rows and hydrates quotes for every symbol (`:262-276`), so sorting NIFTY500 by volume fans out to about 500 quotes per call. This is a scan-performance trap.
- `routers/universe.py`: response mapping (`constituent_to_response`), custom universe creation with DB writes through `UniverseManager`.
- `routers/data_snapshot.py` (215 lines): pyarrow parquet reads, run-log JSONL parsing and file metadata in the router, with its own `PROJECT_ROOT/PHASE1_ROOT/CURATED_ROOT` (`:26-29`), duplicated in `services/history_service.py:21-29` and `data_platform/pipelines/phase1_build.py`.
- Date parsing is copied in `data_snapshot.py:96`, `routers/strategies.py:34`, `backtest_phase1_service.py:58`, `routers/universe.py:98`.
- `routers/terminal.py:282` computes indicators inline.

**Problem:** SQL and cache logic cannot be tested without the app and a DB; data-root paths live in three places so moving `data_system/` breaks silently.

**Target:**
- `services/screener/` with `query.py`, `cache.py` (reusable TTL LRU), `serialize.py`, `service.py`. The router becomes parse, call, return (about 60 lines). For live sorts, hydrate only the top-K candidates or cap and document it.
- `services/snapshots/` with `SnapshotStore(root)` for parquet and metadata reads; one `paths.py` (`DATA_ROOT`, `CURATED_ROOT`, `METADATA_ROOT`) imported by `history_service`, `data_snapshot`, `data_platform`.
- `utils/dates.py: parse_iso_date` replacing the four copies.

**Slices:** (1) `paths.py` and `parse_iso_date`. (2) `SnapshotStore` extracted with tmp-path parquet tests. (3) `ScreenerService` extraction as a pure move. (4, optional) live-sort cap. Response models stay unchanged to protect `frontend/app/screener`.

**Risk:** low for 1-3, medium for 4. **Size:** M.

## 7. Data access layering

**Current state: several overlapping entry points and two storage roots:**
- `data_repository.py` (568 lines): `DataRepository(Session)` covering companies, `HistoricalPrice` save/read, intraday candles, financial statements, quarterly results, update log. `save_historical_prices` lazily imports `indicators.compute_features` (`:94`), coupling persistence to feature computation. Used by `data_layer/provider.py` and `routers/terminal.py`.
- `data_layer/provider.py` (218 lines): `DataProvider(db)` used only by `routers/system_health.py`. A second, different `DataProvider` lives in `engines/data_provider.py` (dead).
- `services/history_service.py` (379 lines): `DailyHistoryService` reads `data_system` parquet first, DB as fallback; the canonical history for VCP and phase1.
- `data_fetcher.py` (195 lines): top-level module with `fetch_fyers_quotes`, `fetch_fyers_preopen`, `get_enhanced_quote`; imported by the screener router, VCP, and `data_layer` (three layers reaching into one top-level helper).
- `services/market_data_service.py` and `services/yfinance_service.py`: dashboard-only path.
- Writers: `data_platform/pipelines/phase1_build.py` (1319 lines) and `fyers_stock_history_build.py` (258 lines), which import `backend.app.*` via `sys.path` insertion (`phase1_build.py:44-46`), plus `backend/scripts/refresh_fyers_*`, `load_bhavcopy.py`, and `DataRepository.save_*`.
- 57 direct `get_db_session`/`SessionLocal` call sites in `app/` despite the AGENTS.md rule to prefer `db_session_scope()` for service-owned work.

**Problem:** no single answer to "where do daily prices come from?". The screener reads `HistoricalPrice` from the DB, backtests read parquet-first, the terminal uses the repository, and system health reports a different provider. Parquet and DB can disagree with no provenance in responses. Writers sit in three places, and the pipelines have no tests.

**Target (ports and adapters, incremental):**
- `app/data/ports.py` with two `Protocol`s: `PriceHistory` (`load_equity_history`, `load_index_history`, `coverage`, `latest_date`) and `QuoteSource` (`get_quotes`). Adapters: today's `DailyHistoryService` and `data_fetcher`.
- Retire `data_layer/provider.py`: `system_health` calls `history.coverage()` and a quotes health check. Move `data_fetcher.py` into `services/quotes.py` with a thin re-export.
- Split `DataRepository` into `repositories/{prices,fundamentals,companies}.py`; move `compute_features` out of the save path into the caller or ingest job.
- `data_platform/` is the only writer package; `backend/scripts/refresh_*` become thin CLIs calling it; replace the `sys.path` hack with one bootstrap.
- Add a grep-based check (like `scripts/quality-checks/check_symbol_format.py`) forbidding new `SessionLocal()` use outside allowed modules.

**Slices:** (1) ports module plus `history_service` conformance, no behaviour change. (2) Move `data_fetcher`, retire `data_layer`. (3) Split `DataRepository`, decouple features. (4) Writers consolidation and session-pattern check; each PR is independent after slice 1.

**Risk:** medium-high for slice 4 (hand-run pipelines, untested; keep diffs to moving functions and run each script's `--help` and a dry run). **Size:** L.

## 8. Testability seams (do alongside the above)

Current tests cover backtest benchmark and persistence, universe sync, symbol flow, runtime safety, websocket, portfolio, startup. There are none for screener, VCP pattern/scan/backtest, indicators, metrics, universe point-in-time, or snapshots.
- `Clock` (`now`/`today`): VCP `_asof_date`, screener cache TTL (`time.monotonic`, `screener.py:19-57`), market hours.
- Session injection: services take a `Session` or factory instead of calling `get_db_session()` internally (`phase1_service`, `vcp_service`).
- Keep module singletons (`vcp_service`, `backtest_phase1_service`, `index_universe_loader`, `daily_history_service`) as production wiring only; construct classes with dependencies in tests.
- `DailyHistoryService` takes a root path argument so tests can point at tmp parquet.

## Tests to add (behavioural, literal expected values)

1. `compute_metrics`: hand-computed 5-point equity curve with known total return, max drawdown, Sharpe (rf=0) and win rate.
2. Universe point-in-time: date before the earliest record (indicative), date inside a window with an `effective_to`-expired symbol excluded (bias-corrected), and no match (live fallback).
3. VCP `analyze_pattern` on synthetic frames: a contracting base yields a signal, a flat frame yields none; plus a characterization test that the extraction changes nothing.
4. VCP backtest survivorship: a symbol removed mid-range is not entered after its `effective_to`. Fails today (severe issue 1).
5. Phase1 strategies: the signal on day t is unchanged when rows after t are altered (no look-ahead).
6. Screener service: sort by `change` with fake quotes gives a deterministic order and requests at most K symbols.
7. `SnapshotStore`: missing artifact gives a domain error; tmp parquet returns filtered rows for a date.
8. Black-Scholes (if kept): put-call parity at r=0.065, sigma=0.2, and a documented Thursday expiry-day case.
9. Indicators: golden values for `ema/atr/rsi` on a 20-bar fixture.

## Suggested PR order

1. Proposal 1 slice 1 (pure deletion). 2. Proposal 6 slice 1 (`paths.py`, `parse_iso_date`). 3. Proposal 4 slice 1 (VCP pure extraction plus tests). 4. Proposal 3 slices 1-2. 5. Proposal 2 slices 1-3. 6. Proposal 1 slice 2 and proposal 3 slice 4. 7. Remaining slices of proposals 5, 6, 4, 7.
