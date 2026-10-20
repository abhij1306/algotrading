# Architecture improvement plan: live market / WebSocket / broker area

Scope: live_market_service, fyers_websocket, fyers_client, ws_manager, routers (websocket, market, market_dashboard, options, auth), data_fetcher, data_cache, cache_manager, data_layer, history_service, option_chain_service, brokers/**, utils/auth, market_hours. No repo files edited. Findings come from reading the code and grepping importers. I did not run any code.

Ranked by value/effort (best first).

## 1. Delete dead code (S, near-zero risk)
Current state (no importers found by grep over backend/):
- `app/data_cache.py` (178 lines): no importer.
- `app/cache_manager.py` (182 lines): only itself. It also defines a second class named `DataCache`, which collides in name with data_cache.py.
- `app/utils/auth.py` (JWT helpers): not imported anywhere. It ships a default secret, "your-secret-key-change-in-production". Nothing in routers/ uses auth dependencies, so trading and `/fyers/token` endpoints are unauthenticated. Either delete it, or wire it in deliberately (see #9).
- `app/services/websocket_error_handler.py`: referenced only by tests/test_websocket_error_handling.py.
- `FyersWebSocketService.set_loop` (no-op stub), `callbacks` and the per-symbol `callback` param (nobody passes a callback), `LiveMarketService.get_latest_ticks`, `ConnectionManager.loop`/`set_loop` (set in main.py, never used for anything).
- `DataProvider.get_history` intraday branch raises NotImplementedError. `DataProvider` is used only by routers/system_health.py.
- `utils/cache.py` is used only by position_sync_service. `routers/market_dashboard.py` hand-rolls its own TTL caches with module globals plus locks.
Problem: three "cache" modules and two classes called `DataCache` mislead readers and agents.
Target: one cache primitive (`utils/cache.py`, TTL, injectable clock).
Slices: PR1 delete data_cache.py, cache_manager.py, and the unused stubs and callbacks. PR2 decide on utils/auth.py (delete or wire in), websocket_error_handler.py and its test.

## 2. One Fyers gateway; stop leaking `client.fyers` (M, medium-low risk)
Current state:
- `FyersClient` (services/fyers_client.py) is a singleton via `__new__` plus a module global (`_fyers_client`) plus `reset_fyers_client()`. It does token-file IO, validation, quotes, history and orders.
- Callers reach inside it via `fyers.fyers`: routers/market.py (about 6 sites), option_chain_service.py (`client.fyers.optionchain`, `.quotes`), brokers/plugins/fyers.py (`self.client = fyers_service.fyers`), data_layer/provider.py (`client.fyers.get_profile()`).
- 94 `get_fyers_client()` call sites, many with inline imports.
- routers/auth.py builds its own `fyersModel.SessionModel`, writes access_token.json itself, and computes IST expiry with a manual +5:30 offset. It then calls `reset_fyers_client()`.
- Quote parsing is duplicated in `FyersClient.get_parsed_quotes`, `data_fetcher.fetch_fyers_quotes` (own 5s cache; omits change_pct) and routers/market.py. `fetch_fyers_quotes` checks `config.HAS_FYERS`, which is separate from the client's own credential state.
Problems:
- After re-login, `reset_fyers_client()` creates a new client, but `FyersBroker.client` (cached at `connect()`) and `OptionChainService._fyers_client` (cached forever) keep the OLD model with the expired token. Quotes and orders can then fail until restart. The `FyersDataSocket` also keeps the old token.
- Sync SDK calls are made from `async def` routes (routers/options.py routes call blocking `option_chain_service.get_option_chain`, which does HTTP), which blocks the event loop. In routers/market.py the routes are plain `def`, so they run in a threadpool.
Target design:
- `services/fyers/` package with three small modules.
  - `TokenStore`: load/save the token file, expiry, and an `on_change` hook.
  - `FyersGateway`: the only code that touches fyersModel. It exposes `quotes(db_symbols) -> dict[db_symbol, Quote]`, `history()`, `option_chain()`, `place_order()` and so on. It re-reads the token through TokenStore on change, so there is no cached model held by callers and no reset function.
  - `auth_flow` (generate_authcode, exchange_code). routers/auth.py becomes thin.
- One shared `Quote` dataclass/TypedDict and one parser.
- Inject the gateway via FastAPI `Depends` and constructor params, so tests pass a fake.
Slices:
- PR1 add a gateway with `quotes()` and a single parser. Switch data_fetcher, routers/market.py and routers/market_dashboard.py to it, then delete `get_parsed_quotes` and the duplicate parsers.
- PR2 add `option_chain()` and `profile()`. Move option_chain_service, data_layer and broker reads onto the gateway.
- PR3 move auth flow and token writing out of routers/auth.py. Replace `print()` with logging, and keep tokens out of logs.
- PR4 make the websocket service and broker ask the gateway for the current token.
- PR5 make the async routes `def` or wrap calls in `asyncio.to_thread`.
Risk: moderate, because many call sites. Do it in the slices above with tests/test_runtime_safety_contracts.py as a guard.

## 3. Rebuild the live-market flow around one owner of subscription state (M-L, medium risk)
Current state:
- Subscription truth is spread over 4 places: `ConnectionManager.subscriptions` and `_global_subscriptions` (ws_manager), `LiveMarketService.pending_subscriptions`, `FyersWebSocketService.subscribed_symbols`, and the SDK socket itself.
- routers/websocket.py computes provider deltas by diffing `manager.get_all_subscribed_symbols()` before and after, in three places (subscribe, unsubscribe, finally). That is business logic in the router.
- `ConnectionManager` also keeps a `subscriptions` dict that duplicates `ClientConnection.subscriptions`.
- `LiveMarketService` mixes market-hours policy, a reconnect state machine (`ws_connected` / `_is_connecting` / monitor loop / thread runner), tick normalization, buffering and flush, and the subscription API. It has 6+ mutable fields, and the SDK thread and its own `ws_thread_runner` thread both touch them.
Concrete symptoms I verified while reading (consequences of the design, not a full bug audit):
- Subscriptions are not replayed after a reconnect. On a monitor-triggered reconnect, or the next morning's connect, `connect()` builds a new socket, but only `pending_subscriptions` is replayed (`on_ws_connected`). Clients that subscribed earlier stay in `manager` and silently stop getting ticks. `FyersWebSocketService.disconnect()` also clears `subscribed_symbols`, and the monitor reconnect overwrites `self.ws` without closing the old socket. (I did not verify whether the SDK blocks inside `ws.connect()`, which would also delay `on_ws_connected`.)
- `ConnectionManager.broadcast` drops slow clients by calling `manager.disconnect()` without closing the socket or telling live_market. The symbols then vanish from the global set before the router's `finally` snapshots them, so the router computes an empty delta and the provider subscriptions leak. The still-open client socket just stops receiving data.
- `POST /disconnect` closes the SDK socket but does not update `live_market` state, so the monitor re-connects it within 30s while the market is open.
Target design:
- `SubscriptionRegistry` (pure, sync, unit-testable): `add(client_id, symbols) -> newly_needed`, `remove(client_id, symbols) -> no_longer_needed`, `drop_client(client_id) -> no_longer_needed`, `all() -> set`. Refcount per symbol. It is the single source of truth, replacing `_global_subscriptions` and `pending_subscriptions`. The registry returns deltas, so routers stop diffing.
- `ProviderSession` (replaces most of `FyersWebSocketService` plus the thread runner): start/stop and `desired_symbols`. On every (re)connect it replays `registry.all()`. It owns the SDK thread and posts ticks to the loop with a single `loop.call_soon_threadsafe`.
- `MarketHoursPolicy` (one function, injectable clock) used by live_market, risk_manager and utils/market_hours. Today 9:15/15:30 plus the weekend check is copied in live_market_service.py, utils/market_hours.py, risk_manager.py and the strategies. Also no holiday calendar anywhere.
- `TickPipeline`: normalize (chp, ch, v mapping) -> latest cache -> batch buffer -> fan-out. Already nearly separate. Move normalization out of `handle_tick_incoming` into a pure function.
- `ConnectionManager` becomes only "clients, queues, fan-out". On slow-client drop it closes the socket (code 1013) and calls an `on_client_gone(client_id)` callback, so cleanup always flows through the same registry path.
- A `LiveFeed` facade (`subscribe(client_id, symbols)`, `unsubscribe`, `drop_client`, `status()`) is the only thing routers call. The websocket router becomes parse -> facade -> ack.
Slices:
- PR1 extract `SubscriptionRegistry` with tests, use it in websocket.py and the manager. This also fixes the slow-client leak, since the delta now comes from `drop_client`.
- PR2 make reconnect replay `registry.all()`. Close the old socket before creating a new one. Make `/disconnect` go through live_market.
- PR3 extract the tick normalizer and `MarketHoursPolicy` (delete the copies of 9:15/15:30; holiday calendar goes here later).
- PR4 collapse `LiveMarketService.connect` and the monitor into `ProviderSession`.
Risk: medium (runtime behaviour, hard to test against live Fyers). Keep the Fyers SDK behind a `DataSocket` protocol so a fake socket can drive tests.

## 4. Thin routers: move logic into services (M, low risk, can run in parallel)
- routers/market.py (657 lines): quote fetching, batching by 50, sector and top-mover computation, and symbol search including `_search_option_symbols`, all inside routes. Move to a `MarketQuoteService` and a `SymbolSearchService`. Routes only validate and serialize.
- routers/market_dashboard.py: module-level cache globals and DB fallback chains (live tick -> yfinance -> DB). Move to a `MarketOverviewService` with an injected cache. Note `get_market_overview` also caches the error payload for 15s.
- routers/websocket.py: delta computation (see #3). `connect_websocket` and `main.py._step_connect_live_market` duplicate the status-to-message mapping.
- routers/auth.py: see #2.
- routers/options.py: pydantic mapping is fine, but routes are `async def` calling blocking services.
Slices: one router per PR, with a service test per extracted function. Size M total; each PR S.

## 5. Consolidate history and data access (M, medium risk)
Current state:
- `history_service.DailyHistoryService` (file-based CSV/parquet plus DB), `DataRepository.get_historical_prices` and `get_historical_data`, `DataProvider.get_history` (validates Company then calls the repository), and `yfinance_service` / `MarketDataService` (static methods making network calls) all answer "give me OHLCV".
- backtest_phase1_service and vcp_service use `daily_history_service`; terminal and system_health use repository/provider. `DataProvider.get_quote` goes through `data_fetcher`, which is a second path to the same quotes (#2).
Problem: different callers can get different data for the same symbol, with different symbol normalization and caching in different places.
Target: one `MarketDataRepository` interface (history(symbol, tf, range), latest_close, coverage) with adapters (DB, file snapshot, yfinance for global indices only). `DataProvider` becomes that interface or is deleted; `MarketDataService` static methods become an injected `GlobalMarketsClient` with a cache.
Slices: define the interface and wrap existing implementations (no behaviour change), migrate callers one at a time, then delete `DataProvider` and the unused repository methods.

## 6. Symbol layer: break the circular lazy imports (S-M, low risk)
- `symbol_master` lazily imports `index_universe_loader` and vice versa (function-level imports in both, plus a `_symbol_master` module global in the loader). `symbol_master.get_index_symbols` and `is_index_constituent` duplicate loader methods.
- `to_fyers_list` and `batch_to_fyers` are the same function; `batch_to_db` is a loop over `to_db`.
Target: SymbolMaster only converts formats (no universe knowledge). Index membership lives only in `index_universe_loader`. Callers use the loader directly. Remove alias methods.
Slices: PR1 remove aliases. PR2 move the two index methods out and update callers.

## 7. Global singletons and testability seams (S per item)
Singletons: `live_market`, `manager`, `symbol_master`, `index_universe_loader`, `option_chain_service`, `daily_history_service`, `yfinance_service`, `FyersClient` (class singleton via `__new__` and a module global), `_ws_instance`, `FyersBroker` (class-level executor, lock and circuit breaker shared by all instances).
Problems:
- Tests must monkeypatch module globals.
- `FyersClient` constructor does file IO and creates an SDK model.
- `asyncio.Lock()` is created in `ConnectionManager.__init__` at import time, tying it to the process rather than the app.
Seams to add:
- A `Clock` (market hours, token expiry, cache TTL). Today everything calls `datetime.now(IST)`.
- A `DataSocket` protocol (see #3).
- A `FyersGateway` protocol (see #2).
- An `AppServices` container built in `lifespan` and stored on `app.state`; routers use `Depends`. Keep module-level `get_*` accessors as thin delegates during migration.
No standalone PR; fold into #2 and #3.

## 8. Sync/async layering rules (S, convention plus a few edits)
- `live_market.connect()` is sync and validates the token (a network call: `fyers.validate_token()` -> `get_profile`). It is called on the event loop from the monitor loop (every 30s) and from `async subscribe()`, so it blocks the loop for the HTTP round trip.
- `_monitor_loop` and `_flush_loop` wrap the whole `while True` in one try/except, so one unexpected error ends the loop and reconnects or ticks stop until something calls `connect()` again. Move the try/except inside the `while`.
- `FyersBroker` runs SDK calls in its own `ThreadPoolExecutor` with a circuit breaker; the gateway in #2 can reuse that guard for all reads.
Rule to document in AGENTS.md: services are sync and pure; anything touching the network is called via `asyncio.to_thread` at the router edge or is an `async def` using a thread executor inside the service; loops never swallow exceptions at the outer level.

## 9. Security hygiene at the boundary (S)
- No auth on any router (trading, `/api/websocket/*`, `/fyers/token`). If the app is local-only, bind to 127.0.0.1 and document that; otherwise wire in a dependency (this replaces utils/auth.py's unused code, and it must fail closed when JWT_SECRET_KEY is unset).
- routers/auth.py uses `print` for token-exchange progress and writes the token file with the default umask.
- `/api/websocket/stream` accepts any origin. Check `Origin` against an allowed list if it is ever exposed beyond localhost.

## Suggested order
1. #1 (dead code), then #3 PR1-2 (fixes the subscription replay and leak symptoms), then #2, then #4 and #6 in parallel, then #5. #7 and #8 ride along.

## Tests to add (behaviour-level)
- `SubscriptionRegistry`: two clients on one symbol, only the last removal produces a provider unsubscribe; `drop_client` returns the right delta.
- Reconnect: with a fake `DataSocket`, subscribe, drop the connection, reconnect; assert the symbols are re-subscribed on the new socket and the old socket is closed.
- Slow client: a full queue closes the socket and the provider unsubscribe is issued.
- After re-login (token file changes), a broker order and an option-chain call use the new token (gateway re-reads).
- `MarketHoursPolicy` with a fake clock: open/close edge minutes, weekend, DEV_MODE.
- Tick normalizer: chp/ch/v/vol_traded_today mapping.
- Websocket contract test for subscribe -> ack -> `ticker_batch` against what frontend/hooks/useWebSocket.ts parses (`ticker_batch`, `ticker`, `pong`, `connection_established`).
