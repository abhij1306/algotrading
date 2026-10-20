# Architecture improvement plan: trading / risk / positions / DB wiring

Scope read: routers/trading.py, terminal.py (order and options parts), unified.py, strategies.py (head), services/order_execution_service.py, risk_manager.py, position_sync_service.py, startup_validator.py (head), brokers/plugins/fyers.py + base.py, database.py, main.py, config.py, exceptions.py, utils/errors.py, models/__init__.py, tests/. Not read end to end: terminal.py chart code, vcp_service, most of models/*.
Paths are relative to backend/app unless noted. Ranked by value/effort, best first. Items marked "verify" were inferred from grep or partial reads.

## Severe issues hit on the way (verified by reading)
- S1. Broker quote shape is read three different ways. fyers.get_quote returns d[0] (fields under "v"). order_execution_service.py:39-44 reads quote["v"]["lp"]. risk_manager.py:99-102 reads quote["ltp"] / quote["last_price"], which the Fyers payload does not carry, so price = 0 and a LIVE MARKET order fails PRICE_ESTIMATE_FAILED (risk_manager.py:279-287) unless RISK_DEFAULT_PRICE is set. check_large_order (risk_manager.py:499) reads "lp" at top level, silently falls back to price 100 and under-reports notional. Fix: proposal 2 (one Quote type).
- S2. Live SL / SL-M orders lose their trigger. order_execution_service._submit_live_order (210-221) never forwards trigger_price. fyers.place_order (brokers/plugins/fyers.py ~118-125) maps every non-MARKET type to type=1 (limit) and hardcodes stopPrice 0. A live SL-M with price 0 goes out as a limit at 0; a live SL goes out as a plain limit. Fix: pass trigger_price, map LIMIT=1, MARKET=2, SL-M=3, SL=4, set stopPrice; validate type/side once (proposal 1).
- S3. Paper identity fragmentation. Paper positions are built only for user_id "default_user" (trading.py:28, 259-270), but orders are stored with the x-user-id header value, "dev_user" in DEV_MODE (trading.py:430-443) or "terminal_user" (terminal.py:652, 687). Those paper orders never appear in /positions/book. Terminal /paper/order (terminal.py:670-715) also stores status SUBMITTED and never fills. Square-off always uses default_user. Fix: proposal 1.
- S4. Live money endpoints have weak auth and no confirmation. trading.py:588 /positions/exit always calls the live broker with no mode, ack or auth. /positions/square mode=LIVE (564-573) the same. Cancel and modify (458-482) have no ownership check. Live modify skips risk checks (order_execution_service.py:318-343). "Auth" for order placement is any non-empty x-user-id header (417-439). utils/auth.py (get_current_user_id) exists but no trading route uses it. CORS uses allow_credentials with env origins (main.py). Fix: proposals 1 and 6.
- S5. A broker outage zeroes stored live positions. fyers.get_positions returns [] when the client is missing (fyers.py:196-198); position_sync_service.py:155-159 then sets net_qty=0 for every DB position not in the broker list. The closed check also compares symbol only, ignoring product type. Fix: proposal 2d.
- S6. No idempotency / commit-after-broker. place_order commits only after the broker call (order_execution_service.py:299-311). If commit fails after the broker accepted the order, the live order is unrecorded and the caller sees ERROR; a retry duplicates the live order. Fix: proposal 4.
- Corrected non-issue: OrderResponse is a TypedDict (brokers/base.py:7), so the isinstance(dict) check at order_execution_service.py:223 is fine.

## Ranked proposals

### 1. One paper/live seam: an ExecutionVenue protocol (HIGH value, M)
Current: the split is an `if effective_mode == "LIVE"` inside OrderExecutionService.place_order (order_execution_service.py:299-308), again in modify (324) and cancel (380). Routers bypass it: trading.py:298-325 and 588-616 build FyersBroker() directly (also 626, 643); terminal.py:670 writes LiveOrder rows itself; terminal.py:578 calls risk_manager directly. The paper position engine (fill math, positions, square-off) lives in routers/trading.py:154-385. The table is named LiveOrder but stores paper rows (is_paper flag), and live risk counters count paper rows too (risk_manager.py:366, 460).
Problem: any new caller can mix modes; user-id and mode rules are per endpoint (S3, S4); paper and live positions come from different code with different shapes.
Target:
- services/execution/venue.py with `ExecutionVenue` protocol: place(intent), modify, cancel, positions(user), square(user, symbol|None).
- PaperVenue (simulator + paper book, moved out of trading.py and _simulate_paper_fill; imports no broker module) and LiveVenue (broker plugin, risk checks, confirmation ack).
- OrderIntent (pydantic): side in BUY/SELL, type enum, int qty > 0, price >= 0, trigger required for SL/SL-M, product enum, mode enum. Validated once.
- get_venue(mode) is the only place that reads mode. Routers: validate, then get_venue(mode).place(intent).
- One `Order` table with a mode column (rename is_paper later); risk counters filter mode=LIVE.
- One user-id resolver dependency used by all trading and terminal endpoints; delete the "default_user" / "terminal_user" / "dev_user" literals.
Slices:
 a. OrderIntent + enums used in trading.place_order and terminal options/paper endpoints (S, ~200 lines).
 b. current_user_id dependency (wraps utils/auth, dev fallback only when DEV_MODE), applied to every order/position endpoint; backfill user_id on existing paper rows with a script (S).
 c. Extract the paper engine to services/execution/paper_book.py with unit tests (M).
 d. PaperVenue/LiveVenue; route modify/cancel/square/exit through them; delete terminal /paper/order and /positions/exit duplication (M).
 e. mode filter in risk counters (S).
Risk: medium; live behaviour must not change except the fixes. Add a test that only live_venue.py imports brokers.plugins.fyers among execution code.

### 2. Brokers: one typed contract, one Quote, normalised errors (HIGH, S-M)
Current: base.py declares TypedDicts but cancel/modify return raw dicts (`{"s": "ok"}` vs `{"status": "ERROR"}`), and callers sniff both (trading.py:286, 306, 598; order_execution_service.py:329, 383). get_quote returns the raw vendor dict (S1). FyersBroker() is built per request in routers (trading.py:301, 593, 628, 645) while services keep their own lazy instances (order_execution_service.py:29, risk_manager.py:74, position_sync_service.py:34): four or more broker objects, while the circuit breaker is class-level.
Target: base.py defines Quote(ltp, bid, ask, ts), OrderAck, ModifyAck/CancelAck with the same shape, and BrokerError. Only FyersBroker knows "s", "v", "lp". A get_broker() accessor (cached, overridable in tests) is used everywhere. get_positions raises on an unavailable client.
Slices: (a) Quote + parsing in get_quote, fix three callers (S); (b) uniform Ack types, drop dual-key sniffing (S); (c) get_broker() and replace the router constructions (S); (d) get_positions raises, sync skips on error and closes by (symbol, product) (S).
Risk: low-medium; needs a FakeBroker fixture (proposal 8).

### 3. Move business logic out of routers (HIGH, M)
Current: routers/trading.py has ~230 lines of fill/position/square logic (154-385). routers/terminal.py (725 lines) mixes chart assembly (71-345), option symbol building, risk calls and an order write (670-715), with direct SessionLocal() (409, 678). unified.py is a stub: /close-position returns "success" without doing anything, /mode pretends to switch, /pnl swallows errors. trading.get_positions takes db but does not use it (495-504).
Target: routers only parse, authorise, call one service, map errors. New modules: services/execution/ (proposal 1), services/chart_service.py, services/options_order_service.py (preview + place). Remove or 501 the fake unified.py routes after grepping frontend usage.
Slices: (a) delete unified.py fake endpoints (S); (b) move chart helpers to chart_service, mechanical (M); (c) paper engine move (1c); (d) options order build/preview to a service (S).
Risk: low; check frontend calls first.

### 4. DB session and transaction ownership (MED-HIGH, S-M)
Current: three styles. get_db (request) never commits. db_session_scope (database.py:137-151) is used only by history_service and vcp_service. Raw SessionLocal() in position_sync_service.py:88,195, risk_manager.py:181,252,363,403,437, terminal.py:409,678, screener, audit_logger, excel_parser, services/universe; get_db_session (database.py:131) is another bypass. AGENTS.md says services should use db_session_scope. place_order commits inside the service and swallows every exception into an error dict (order_execution_service.py:310-316). No client order id / idempotency (S6).
Target: services never commit a caller-owned session; request path uses a unit-of-work dependency (commit on success, rollback on error); background work uses db_session_scope. For LIVE: persist the row (status PENDING, client_order_id) in its own committed transaction before calling the broker, then update; a retry with the same client id returns the existing row.
Slices: (a) replace raw SessionLocal() in position_sync/risk_manager/terminal (S); (b) client_order_id column + unique index + lookup in place_order, with a migration (M); (c) commit-before-broker for LIVE (S); (d) request-level unit of work, remove in-service commits (M).
Risk: medium for b-d (live path); test with FakeBroker.

### 5. Startup/lifecycle wiring in main.py (MED, S)
Current: lifespan runs a StartupSequence, but the broad `except Exception` still reaches `yield`, so a failed required step (database) leaves the app serving. No shutdown work (only a log line): live_market and ws manager are not stopped, engine not disposed. position_sync_service.start() is defined but never called (grep), so positions refresh only via GET, and each GET /positions and /positions/book does a blocking broker call plus DB write (trading.py:502, 516). PositionSyncService creates asyncio.Lock() at import time. main._step_validate_database duplicates database.init_db(). Schema evolution is hand-written ALTERs (database.py ensure_*_schema) while migrations/ also exists. DATABASE_URL does not URL-encode DB_PASSWORD; the SQLite switch is USE_SQLITE_TEST == "True".
Target: lifespan raises on a failed required step; starts position sync as a managed task; shutdown stops sync, live_market, ws manager, disposes engine; main calls init_db(); build the URL with sqlalchemy.engine.URL.create; choose one migration mechanism.
Slices: (a) fail-fast + shutdown hooks (S); (b) start/stop position sync, GET returns cached data with last-sync age (S); (c) URL.create + init_db reuse (S); (d) migration tooling decision (separate plan).
Risk: low; (a) intentionally changes behaviour when the DB is down.

### 6. Config/settings consolidation and auth (MED, M)
Current: ~36 raw os.getenv/environ sites in app; three env-loading paths (main.py, database.py get_env_file, config.py); Config class does file IO at class-definition time (config.py:16-40); RiskConfig is a hard-coded dataclass (risk_manager.py:41) with no env or per-user override; DEV_MODE and RISK_DEFAULT_PRICE read ad hoc (trading.py:434, risk_manager.py:107); JWT secret defaults to "your-secret-key-change-in-production" (utils/auth.py:12) with no startup check.
Target: app/settings.py (pydantic-settings) with nested risk, database, dev_mode, cors_origins, jwt_secret; get_settings() cached and overridable in tests; startup refuses the default JWT secret unless dev_mode; RiskManager receives RiskConfig from settings.
Slices: (a) Settings + replace getenv in main/database/trading/risk (M); (b) JWT startup guard + auth dependency on trading/terminal (S, with 1b); (c) delete Config once fyers_client and data_fetcher use Settings (S).
Risk: low-medium; keep secrets out of logs.

### 7. Risk manager boundaries (MED, M)
Current: RiskManager mixes DB reads, broker calls (get_funds, get_quote) and policy (risk_manager.py:114-433), opens its own sessions and broker. pre_trade_check returns only on FAIL (130-159), so the WARNING from the daily-loss 80% branch is discarded and the risk_override_reason flow (order_execution_service.py:160-167) never sees it. Daily loss sums LivePosition across all users (188-200) from stale synced rows. quantity compared without int coercion (237). check_large_order is a separate path never used by live placement.
Target: pure policy `RiskPolicy.evaluate(order, snapshot) -> list[RiskFinding]` plus an injected RiskSnapshot provider (positions, funds, today's LIVE order count, quote). pre_trade_check returns the worst finding so WARNINGs propagate; scoped per user and mode.
Slices: (a) propagate WARNING in pre_trade_check + tests (S); (b) RiskSnapshot provider, inject broker/db (M); (c) pure policy functions + table-driven tests (M).
Risk: medium, tightening can block orders; log-only rollout first.

### 8. Test suite structure and seams (MED-HIGH, M)
Current: tests/ is flat (10 files, ~1.8k lines). Nothing tests order_execution_service, the paper fill simulator, paper position math (trading.py:154-282), square-off, risk checks (one session-ownership test), position_sync, startup failure behaviour or the Fyers order mapping. test_runtime_safety_contracts.py::test_trading_order_uses_request_mode mocks place_order, so it only proves the router forwards mode. test_portfolio.py hits only root/health-style routes. pytest.ini runs coverage + html on every run (slow for targeted runs); unit/integration/slow markers are declared but unused. Stray test.db and log files sit in backend/ (verify tracked via git ls-files).
Target: tests/unit (paper_book, simulate_fill, risk policy, OrderIntent), tests/api (TestClient + FakeBroker via get_broker override + sqlite session), tests/contract (broker plugin vs recorded Fyers payloads), existing files under tests/integration. Fixtures: fake_broker, frozen_market_hours, settings_override, db_session.
Tests to add first: paper fills per order type and side (including SELL limit/SL); paper position reversal and average-price math; PAPER order never touches broker (FakeBroker call count 0); LIVE requires ack; LIVE SL forwards trigger (guards S2); risk WARNING flow; sync during broker outage keeps positions (S5); startup fails fast on DB error; idempotent retry returns the same order.
Slices: (a) fixtures + FakeBroker (S); (b) simulator and paper book unit tests (S); (c) API tests for place/cancel/modify/square per mode (M); (d) make coverage opt-in (S).
Risk: none to prod.

### 9. Error layering: exceptions.py vs utils/errors.py (LOW-MED, S)
Current: exceptions.py defines the SmartTraderError hierarchy with status_code/to_dict and a back-compat alias SmartTraderException (exceptions.py:35; main.py:24, 253). utils/errors.py offers handle_api_error / SafeHTTPException building HTTPException directly; only auth, universe, upload, websocket use it. trading/terminal hand-roll `HTTPException(500, detail=f"... {str(e)}")`, leaking internals (trading.py:507, 539, 554, 578, 616, 633, 649, 673, 686). The service returns `{"status":"ERROR"}` dicts, forcing routers to re-raise as 400 (trading.py:447, 471, 480), and place_order catches all exceptions including programming errors (order_execution_service.py:313-316). data_layer/exceptions.py is a third hierarchy.
Target: services raise typed errors (OrderValidationError 400, RiskRejected 422 with code, BrokerUnavailable 503, OrderNotFound 404); the existing main.py handler maps them; routers carry no try/except-to-HTTP; utils/errors.py shrinks to sanitize helper or is deleted; drop the alias.
Slices: (a) trading error classes + handler mapping, convert order_execution_service to raise (M); (b) remove router try/except in trading/terminal (S); (c) migrate four routers off utils.errors and delete (S).
Risk: low-medium; keep the `detail` response shape the frontend reads.

### 10. Dead / duplicated code to delete (low risk, S each)
- app/models.py (48 lines of pydantic models) is shadowed by package app/models/ (`from . import models`, database.py:12). Grep for StockFeatures / ScreenerResponse / HealthResponse imports, then delete or move to schemas/.
- models/order.py PaperOrder/PaperPosition/PaperTrade/PaperFund/ActionCenter and models/live_trade.py LiveTrade: no references outside models/ (grep). They duplicate LiveOrder with is_paper and create unused tables via create_all. Confirm prod tables empty first.
- access_control.py (AccessControl, AgentType): no importers found (verify).
- unified.py fake endpoints (see 3); trading.py /mode GET/POST no-ops (391-406); terminal /paper/order.
- database.py: get_db_session, MODELS_IMPORTED, MODEL_EXPORTS, duplicated init logic.
- risk_manager.check_large_order + trading /risk/large-order-check duplicate the preview logic in terminal.py:554-612; keep one.
- trading.get_orders returns ORM objects with response_model=None (trading.py:485); add a response schema.
One PR per group, with grep proof in the description.

## Suggested order of work
1. S1/S2/S5 and 7a (quote shape, trigger forwarding, sync-on-outage, WARNING propagation): smallest PRs fixing live-money defects.
2. Proposal 8a-b (fixtures + paper engine tests) to protect later refactors.
3. Proposal 1a-b (OrderIntent, single user resolver), then 1c-e.
4. Proposals 4 and 5 (sessions/idempotency, lifecycle).
5. Proposals 3, 9, 6, 10 as parallel mechanical cleanups.
