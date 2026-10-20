# Architecture plan: status and remaining work

**Last audited:** 2026-10-20, against main at `19eb32f` (after PRs #18 to #41).

The area files hold the design and evidence. This file tracks what is left. Lanes can run in parallel because each one owns a set of files. Tasks inside a lane run in order. Sizes: S = small, M = medium, L = large.

**How to update it:** tick a task in the PR that lands it, with the PR number. "In flight" marks work on an open branch.

## Done

- Every severe defect in [README.md](README.md) except the Black-Scholes expiry math, which is in flight.
- The deletion PRs:
  - Frontend 1 PR1 to PR3, and the frontend dead code.
  - Screener/backtest 1.1 and 1.3.
  - Live-market 1-PR2.
- **Live market:**
  - Subscription replay on reconnect.
  - The slow-client leak.
  - The explicit `/disconnect`.
  - Current-token consumers.
- **Trading:**
  - The quote shape.
  - The stop-loss trigger.
  - Position sync during an outage.
  - Positions raise on a broker outage.
- **Backtest:**
  - One metrics module (#25).
  - The VCP point-in-time universe (#20).
- **Frontend:**
  - The typed client.
  - The Query provider for the terminal, screener and backtest.
  - The quote store.
  - LIVE is never persisted.
- **Platform:**
  - The CI gates (#31).
  - A down database returns 503 `DATABASE_UNAVAILABLE` and the server keeps serving (#38). This replaces fail-fast from trading 5a.

## In flight (open branches, 2026-10-20)

- Order idempotency: `client_order_id`, commit before the broker call, and the paper order tag (trading S6, 4b, 4c).
- Error-text leaks in routers, plus fake `/pnl` and `/positions` on broker failure, plus `/strategies/regime` returning 500 (trading 9b, 3a, part of 9c).
- Live market recovering after a DB-down startup.
- Retiring `lib/api-client.ts`: dashboard, Insights, strategies and LoginButton (frontend 2 PR3 and PR4).
- Frontend UX: theme persistence, real command-palette search, Ctrl or ⌘ hints.
- Black-Scholes moved to `services/options/`, with the expiry math fixed (screener-backtest 1.2 and the severe defect).
- Symbol aliases removed and the index methods moved out of `symbol_master` (live-market 6-PR1 and 6-PR2).
- Options routes stop blocking the event loop (live-market 2-PR5).
- A `gen:api` script and a CI check that the schema is current (frontend 3 PR1).

## Remaining lanes

### Lane 1: live feed
Files: `ws_manager.py`, `routers/websocket.py`, `live_market_service.py`, `fyers_websocket.py`, `utils/market_hours.py`.

- [x] 1. Live cleanup (S): remove `set_loop`/`loop`, `get_latest_ticks` and `ConnectionManager.subscriptions`; share one status-to-message map.
- [x] 2. SubscriptionRegistry, a pure refcount (M). The router stops diffing subscriptions. Add registry and slow-client tests.
- [x] 3. MarketHoursPolicy, a Clock and a tick-normalizer module (S-M). Delete the copies of 9:15/15:30, including the dead `RiskConfig` fields. (#56, #67)
  - PR #56: `MarketHoursPolicy` (IST, 09:15:00-15:30:00 inclusive, weekends, DEV_MODE, an empty-by-default `holidays` set) and the `Clock` seam (`utils/clock.py`) own market hours in `utils/market_hours.py`; `live_market_service.py` takes an injected clock, and the tick normalizer lives in `services/live/ticks.py`. PR #67: the dead `RiskConfig.market_open_time`/`market_close_time` fields are deleted and `RiskManager` takes an injected `MarketHoursPolicy`. Follow-up: a holiday calendar (`holidays` is still empty).
- [x] 4. ProviderSession behind a DataSocket protocol, with fake-socket tests (L). (#73)
  - `services/live/provider_session.py` owns the socket: the connect runner, `SessionState`, replay on every (re)connect including SDK auto-reconnects, and a fresh token per connect. `services/live/data_socket.py` is the `DataSocket` protocol the Fyers SDK socket satisfies; `fyers_websocket.py` is down to the socket factory and the token source, and `FyersWebSocketService` is gone. Tests run on `tests/fakes/data_socket.py`. The `ws_service` attribute is gone: `services/fyers/auth_flow.py` asks `LiveMarketService.has_provider_socket()` (a socket held or a connect in flight) before closing the old-token socket after a login.
- [ ] 5. Write the sync/async and local-only rules into AGENTS.md, and add an `Origin` check on `/stream` (S).

### Lane 2: Fyers gateway and market routers
Files: `fyers_client.py`, `data_fetcher.py`, `routers/market.py`, `market_dashboard.py`, `options.py`, `auth.py`, `option_chain_service.py`, `data_layer/`, `system_health.py`.

- [ ] 2. A Quote type and one parser in the gateway (M). Delete `get_parsed_quotes`.
- [ ] 3. Gateway `option_chain()`, `profile()` and `depth()` (M).
- [x] 4. A TokenStore, with the auth flow moved out of `routers/auth.py` (S-M). Use zoneinfo for IST and write the token file as 0600. Done: `services/fyers/token_store.py` and `services/fyers/auth_flow.py`.
- [ ] 5. MarketQuoteService and SymbolSearchService, extracted from `market.py` (M).
- [ ] 6. MarketOverviewService on `utils/cache` (S). It must not cache error payloads.
- [ ] 7. Retire DataProvider and `data_layer`, and move `data_fetcher` to `services/quotes.py` (M).

### Lane 3: trading execution
Files: `routers/trading.py`, `terminal.py`, `order_execution_service.py`, `risk_manager.py`, `position_sync_service.py`, `brokers/`.

- [x] Risk checks on live modify, exit and square-off (part of S4).
- [x] Paper money math moved to Decimal (`services/execution/paper_book.py`; paise, ROUND_HALF_UP).
- [x] Paper modify re-runs the fill simulation.
- [x] LIVE modify and cancel stop returning `str(e)`.
- [x] 1. A shared FakeBroker and frozen-hours fixtures in conftest. Make coverage opt-in in `pytest.ini` (S). `fake_broker` (`tests/fakes/broker.py`) and `freeze_market(True | False | instant)`; CI passes `--cov` explicitly.
- [x] 2. Risk fixes (S): propagate WARNING, count LIVE orders only, cast quantity to int, track daily loss per user. (#67) A LIVE place that only warns needs `risk_override_reason` and returns `warnings`; `coerce_quantity` rejects fractions; the daily loss is keyed to `LIVE_ACCOUNT_USER_ID` until task 5. Also: `/api/trading/positions` answers 503 `BROKER_UNAVAILABLE` on a failed sync, and `/positions/book` marks live rows `stale`.
- [ ] 3. Replace the raw `SessionLocal()` calls in `position_sync`, `risk_manager` and `terminal` (S).
- [ ] 4. An OrderIntent with enums (S).
- [ ] 5. A `current_user_id` dependency (S). Remove the hard-coded default, terminal and dev users, and add a backfill.
  - `POST /api/trading/order` still answers 401 without `x-user-id` (header or `?x_user_id=`) outside DEV_MODE, so the terminal's `usePlaceOrder` sends `x_user_id=default_user` (one constant in `lib/queries/terminal.ts`). Drop it with this task.
- [ ] 6. Extract `paper_book.py`, with position-math and reversal tests (M). Started: the pure math and its tests are in `services/execution/paper_book.py`; the DB-backed book and square-off still live in `routers/trading.py`.
- [ ] 7. A broker contract (S-M): Quote and Ack types, `get_broker()`, and no more dual-key sniffing.
- [ ] 8. PaperVenue and LiveVenue (M-L). Delete terminal `/paper/order`, and add an import-guard test.
- [ ] 9. Typed service errors, mapped in `main.py` (M).
- [ ] 10. Trading dead code (S):
  - `/mode` and `check_large_order`
  - the `Paper*` and `LiveTrade` models
  - the unified `/close-position` and `/mode`
- [ ] 11. Position sync as a lifespan task (S).
- [ ] 12. Split `terminal.py` into `chart_service` and `options_order_service` (M).
- [ ] 13. A unit of work per request, with no commits inside services (M).
- [ ] 14. A RiskSnapshot provider and a pure RiskPolicy, with table-driven tests (M+M).

### Lane 4: platform
Files: `main.py`, `database.py`, `config.py`, `exceptions.py`, `utils/errors.py`.

- [x] 1. Shutdown hooks, and reuse `init_db` (S).
- [x] 2. `URL.create` and boolean env parsing (S-M). Delete `get_db_session` and `MODEL_*`.
- [ ] 3. Move routers off `utils/errors`, drop the alias, and fold in `data_layer/exceptions` (S).
- [ ] 4. pydantic Settings in place of `getenv`/Config (M). Do this last.
- [ ] 5. A decision note on migration tooling (S).
- [ ] 6. A CI grep that blocks any new `SessionLocal()` (S).

### Lane 5: universe and symbols
- [x] 3. Move the custom universe to `services/universe/custom.py` and delete `engines/` (M).
- [ ] 4. Put the loader and sync under the universe package, with a `repository.py` (S-M).

### Lane 6: VCP and backtest
- [x] 1. VCP pattern functions into `services/vcp/pattern.py` (M).
- [x] 2. VCP positions and status behind a quote port (M).
- [ ] 3. VCP scan, regime and config (M).
- [ ] 4. A VCP backtest module with a shared `trailing_stop` (M).
- [ ] 5. `UniverseService.members_as_of`, returning members with their provenance (M).
- [ ] 6. A BacktestResult type with a single persistence writer (M).
- [ ] 7. A Phase-1 strategy registry, with no-look-ahead tests (M).
- [ ] 8. Golden-value tests for the indicators. Switch callers to the shared indicators and delete the unused `calculate_*` (S-M).

### Lane 7: data and screener
- [x] 1. `paths.py` and `utils/dates.parse_iso_date` (S) (#61).
- [x] 2. A SnapshotStore, with tests using temporary parquet files (M).
- [ ] 3. Extract a ScreenerService (M).
- [ ] 4. Cap the live sort at the top K (S).
- [ ] 5. PriceHistory and QuoteSource ports (S).
- [ ] 6. Split DataRepository, and move `compute_features` out of the save path (M).
- [ ] 7. Consolidate the writers, with a bootstrap that replaces the `sys.path` hacks (L).

### Lane 8: terminal UI
- [x] 1. One `placeOrder` mutation through the typed client (S). `usePlaceOrder` in `lib/queries/terminal.ts` is the only call site; retries reuse the click's `client_order_id`, and a cut-off 200 is retried, not reported as placed. `x_user_id` stays until Lane 3 task 5 because the backend still needs it.
- [x] 2. A LIVE confirmation dialog showing every order field (S). `app/terminal/LiveOrderDialog.tsx` is a native modal `<dialog>`. It shows the snapshot taken at click time and sends exactly that; focus starts on Cancel; Escape and Cancel send nothing. On open it runs `POST /api/trading/risk-check` for that order and lists any warnings (or the failure) before Confirm; placing the order still re-checks, and the overridden warnings in its response show in the ticket message. Square-off never used `confirm()`.
- [x] 3. Wire up or remove the toolbar Buy/Sell buttons, and read `?symbol=` with `useSearchParams` (S). Removed: the ticket has no side to prefill (its own Buy/Sell submit), so they would have been a second submit path. The selected symbol now lives in `?symbol=`.
- [x] 4. Move the terminal into `features/terminal` and split it (L). (#74) `app/terminal/page.tsx` is a thin route over `features/terminal/TerminalScreen.tsx`, which composes `components/` (header, watchlist search and table, view toolbar, option chain, positions, order ticket, `LiveOrderDialog`) over `hooks/` (`useTerminalSymbols`, `useOptionsChain`, `useLiveRows`, `useSquareAction`, `useWatchlistSearch`, `useOrderTicket`). Lot sizing, order drafts, LIVE dialog rows and display rows are table-tested pure modules in `lib/`; the terminal flow tests moved to `features/terminal/__tests__`.

### Lane 9: dashboard
- [x] 1. Move the dashboard to quote-store selectors and Query, removing its 8 `useEffect`s (M). (#46)
- [x] 2. Split the page (M). (#57) The page composes `components/dashboard/*` sections over `useDashboardData`; formatting goes through `lib/format.ts`/`lib/time.ts`; the connection badge reads `useConnectionStatus`, so the dashboard no longer uses the `useWebSocket` shim; the post-market Commodities card gets Crude Oil from `/api/market/commodities`.

### Lane 10: screener UI
- [x] 1. A live overlay through `useQuote`, removing 8 `useEffect`s (M). The page reads `useQuotes` for the rendered rows and derives each row's live values during render; the REST fallback is `useLiveQuotePoll` in `lib/queries/screener.ts`, which during market hours also polls symbols whose streamed quote is over 5s old (store `receivedAt`, render-time clock from `lib/use-now.ts`), with the fresher quote winning; cell flashes compare against the previous render's value. No effects remain in `app/screener`.
- [x] 2. Delete the `useWebSocket` compatibility shim (S). `hooks/useWebSocket.ts` and the store's `onMessage`/`lastMessage` surface are gone; the fake socket used by the store tests is shared from `lib/realtime/__tests__/fake-socket.ts`.
- [x] 3. Split the page, with tests for sort and format (M). The page is now `useScreenerData` (filters, server-side sort, paging), `useLiveOverlay` (stream, poll and stale-quote fallback) and `components/screener/`; sort, poll selection, quote priority, cell formatting and paging are table-tested pure functions in `screener-view.ts`.

### Lane 11: strategies and backtest pages
- [x] 1. Split the strategies page, and turn queue, cancel and halt into mutations (M-L). Mutations in PR #46; the page is now `useStrategyData`, `useStrategyActions` and `components/strategies/`, every action stays disabled while one is in flight, and the local formatters are gone for `lib/format.ts`.
- [x] 2. Split the backtest results page (S-M). Header, metrics strip, run summary, charts and trade log are in `components/backtest/`; chart rows and tones are tested pure functions.

### Lane 12: shared frontend libraries and contracts
- [x] 1. `format.ts` and an IST `time.ts`, with table-driven tests. They replace `market-hours.ts`, including the `getDay` holiday bug (S-M).
  - Partial (PR #53): `lib/format.ts`, `lib/time.ts` and an IST `lib/market-hours.ts` with tests; screener, backtest and the command palette switched. Dashboard and its insight panels switched with Lane 9.2. Strategies switched with Lane 11.1; terminal and `lib/queries/terminal.ts` with Lane 8.1-3, which also deleted the `lib/utils.ts` shims.
- [ ] 3. Set up MSW for tests (S, optional).

## Out of scope

- Frontend 11, the design system, which the design agent owns.
- API auth and login. This is a single-user localhost project; trading safety (idempotency, risk checks, failing closed) still applies.
