# 00: Overview

Scope, vocabulary, module boundaries and how HAT sits inside SmartTrader. Requirement format and
verification methods: see [README.md](README.md) and [11-verification.md](11-verification.md).

Hire a Trader (HAT) is a deterministic swing-trading engine added to SmartTrader: nightly scans, live
triggers, one risk manager, and three execution modes per bot (shadow, confirm, auto). The MVP is two
configurable bots on NSE for one user.

## §1 Scope and phases

| Phase | Scope | Architecture change |
| --- | --- | --- |
| MVP | Minervini-style (S1) and Qullamaggie-style (S2) bots, NSE, single user, Telegram | Build the engine, criteria library, portfolio layer, broker interface with a direct Fyers adapter |
| P2 | Same two sleeves on US markets; ML regime model; screener-driven universes | Market abstraction (calendar, currency, data source, US broker); regime model v2; fundamentals feed |
| P3 | Additional sleeves (new strategies or universes) | New criteria and rule packs; no engine change if the MVP is right |

Source: MA "Summary", "Phase progression". The MA lists Fyers, Zerodha and Kotak Neo adapters through
OpenAlgo for the MVP; the owner's build decisions of 20 Oct narrow that to a direct Fyers adapter
(HAT-DEC-BD2, HAT-DEC-BD3).

#### HAT-OVR-001: Market rules stay out of the engine
- **Statement:** Every instrument, strategy and calendar row carries `market_id`, and the engine reads session times, holidays, price bands and currency only from market reference data, never from constants.
- **Source:** MA "Phase progression"; SCH "Summary and conventions" (Market); T1.
- **PR:** 2, 4
- **Verification:** CONF `market_id_on_market_tables`, PBT `calendar.session_from_reference_data`
- **Acceptance:**
  - Property: every `hat_*` table that the schema spec marks as market-scoped (instruments, strategies, calendars, universes, regime) has a non-null `market_id` column. Harm: adding the US market in P2 would need a schema change across live tables.
  - Property: for every market row and every date, the session open/close the engine uses equals the `hat_trading_calendar` row (or the `hat_market` default when the day is a normal trading day). Harm: hard-coded NSE hours would place US orders at the wrong time in P2.

#### HAT-OVR-002: MVP scope is two sleeves, one user, one broker
- **Statement:** The MVP runs exactly two sleeves (S1 `stage2-contraction-breakout`, S2 `momentum-breakout-ep`) for one user on NSE, executes through Fyers only, and is controlled through one Telegram chat.
- **Source:** MA "Summary", "Decisions register" (I12/I13); OWN-20OCT.
- **PR:** 8, 13, 16
- **Verification:** CONF `mvp_seed_scope`
- **Acceptance:**
  - Property: after seeding, the set of strategies is exactly {`stage2-contraction-breakout`, `momentum-breakout-ep`}, the set of markets is {NSE}, every `hat_broker_account.broker` is `fyers`, and every owned row carries the single configured `HAT_USER_ID`. Harm: an unreviewed strategy, market or broker reaches a live path the MVP never verified.

## §2 Glossary

| Term | Meaning |
| --- | --- |
| Bot | A deployed strategy version on one broker account with an execution mode. A bot is data, not code. In the schema, `hat_bot` is also the sleeve. |
| Sleeve | One strategy version on one universe version (PL1). The unit the allocator weights. |
| Strategy version | An immutable, versioned configuration: universe, regime rule, setup, trigger, entry filters, stop rule, exits. |
| Playbook | A named set of setup, trigger, entry filters and stop inside one bot (S2 has `breakout` and `episodic_pivot`). |
| Criterion | A library type (24 in the MVP) with a typed parameter schema; a pure function over bars, features or live bars. |
| Hard / score | A hard criterion must pass; a scored one contributes a value in [0,1] to the weighted setup score. |
| Setup score | `Σ w_i s_i / Σ w_i` over scored setup criteria; the bot's threshold (default 0.6) gates the watchlist. |
| Pivot | The price level whose intraday break is the entry trigger. |
| R | The initial risk per share, `P_entry − P_stop`; results are stated in multiples of R. |
| ADR / ADTR / ADV | Average daily range %, average daily true range, average daily volume (20 days unless stated). |
| EP | Episodic pivot: a gap-up on a catalyst after a period of neglect. |
| Execution mode | `shadow` (virtual fills only), `confirm` (you take or skip each entry), `auto` (orders sent directly). Default shadow. |
| Manage-only | No new entries; stops and exits keep running. |
| Kill switch | A trip that puts all bots (account scope) or one bot into manage-only. |
| Risk policy | The account-level, versioned record holding every risk number. Bots hold none. |
| Regime | The portfolio-level market state (4 states in v1) that scales sleeve weights; distinct from a strategy's own binary regime gate. |
| Lifecycle state | A sleeve's evidence state: research, backtested, shadow, live, probation, retired. |
| Trade date | The exchange-local date (`trade_date date`) a job, bar or signal belongs to. |
| Invariant | One of the six rules no configuration can change (06 §7). |

Source: BL "Summary", "Configuration model", "Portfolio layer", "Risk management".

## §3 Module boundaries and package layout

HAT is one bounded module in the SmartTrader backend (HAT-DEC-BD1):

| Package | Holds |
| --- | --- |
| `backend/app/hat/data` | MCP client, reference data, ingest, quality gate, Parquet store, features, bars, announcements |
| `backend/app/hat/engine` | Criteria library, config model, scan, stops, exits, lifecycle, portfolio (regime, allocator), backtest |
| `backend/app/hat/risk` | Risk policy, sizing, caps, exposure ladder, kill switches, invariants (pure functions) |
| `backend/app/hat/execution` | Broker interface, Fyers adapter, router, reconciliation, sessions |
| `backend/app/hat/telegram` | Bot API client, commands, approval cards, notifier |
| `backend/app/hat/ops` | Scheduler, job runner, partitions, retention |
| `backend/app/hat/models` | SQLAlchemy models on the shared `app.base.Base` |

The MA names the Telegram module `hat/notify`; this spec uses `hat/telegram` (plan layout).

#### HAT-OVR-003: Criteria import no broker, DB session or UI module
- **Statement:** `hat/engine/criteria` imports no broker, DB session or UI module, so backtest and live run one code path.
- **Source:** MA "SmartTrader extension / Boundary rules"; T12.
- **PR:** 2, 8
- **Verification:** CONF `import_graph_boundaries`
- **Acceptance:**
  - Property: in the static import graph of `backend/app`, no module under `app.hat.engine.criteria` reaches (directly or transitively) `app.brokers`, `app.hat.execution`, `app.database`, `sqlalchemy.orm`, `app.routers`, `app.hat.telegram` or `fastapi`. Harm: criteria that read live broker state or the DB would behave differently in backtest and live, so backtests stop predicting live results.

#### HAT-OVR-004: Only the router reaches the broker
- **Statement:** Nothing in `hat/engine`, `hat/data` or `hat/risk` imports the broker layer; every order passes the risk manager and then the execution router.
- **Source:** MA "Component architecture"; I7.
- **PR:** 2, 13, 15
- **Verification:** CONF `import_graph_boundaries`, TLA `ExecutionRouter!RiskBeforeRoute`
- **Acceptance:**
  - Property: no module under `app.hat.engine`, `app.hat.data` or `app.hat.risk` imports `app.brokers` or `app.hat.execution.broker`/`fyers_adapter`. Harm: a code path could place an order without the risk check.
  - Property: in every reachable state of the router model, a broker `place_order` for an entry happens only after a `RiskDecision` of approve for that signal. Harm: an unsized or uncapped order reaches the exchange.

#### HAT-OVR-005: HAT owns only `hat_` tables
- **Statement:** Every table HAT creates is prefixed `hat_`, and HAT tables hold no foreign keys to non-HAT tables; existing SmartTrader tables are inputs for seeding only.
- **Source:** MA "Boundary rules"; SCH "Summary and conventions" (Prefix); PLAN §4.2.
- **PR:** 2, 3
- **Verification:** CONF `hat_tables_prefixed_and_isolated`
- **Acceptance:**
  - Property: every SQLAlchemy table defined under `app.hat.models` has a name starting `hat_`, and every foreign key on those tables targets a `hat_` table. Harm: the module cannot be extracted later without untangling shared tables.

#### HAT-OVR-006: One live feed
- **Statement:** HAT reads live market data through SmartTrader's `LiveMarketService` and `SubscriptionRegistry` (owner `"hat"`), never through a second WebSocket to the broker.
- **Source:** MA "Boundary rules"; I3; PLAN C4.
- **PR:** 14
- **Verification:** CONF `single_feed_import_rule`
- **Acceptance:**
  - Property: no module under `app.hat` imports the Fyers data-socket SDK or opens a WebSocket; live data arrives only through `LiveMarketService.add_tick_listener` and `SubscriptionRegistry`. Harm: a second socket to the same broker breaks the session or doubles the symbol limit use.

#### HAT-OVR-007: HAT is off unless enabled
- **Statement:** HAT's runtime tasks start only when `HAT_ENABLED` is true (default false), so existing SmartTrader behaviour and tests are unchanged.
- **Source:** PLAN C4, Q2.
- **PR:** 2, 17
- **Verification:** TLA `JobScheduler!NoJobWhenDisabled`, CONF `hat_enabled_default_false`
- **Acceptance:**
  - Property: in every run of the lifespan with `HAT_ENABLED` unset or false, no HAT job, listener or Telegram poller is started. Harm: a developer machine or test run starts scheduled jobs or Telegram polling by accident.

## §4 Reuse map

What HAT reuses from SmartTrader (R), extends (M) or builds new (N). This is plan section 4.1/4.5,
condensed; PR numbers refer to the plan's PR list.

| Capability | Status | SmartTrader code | PR |
| --- | --- | --- | --- |
| Live ticks and subscriptions | R + M (tick-listener hook) | `services/live_market_service.py`, `services/subscription_registry.py` | 14 |
| Market hours, clock | N (HAT-owned copies since K1; the holiday calendar from `hat_trading_calendar` builds the policy, nothing is installed process-wide, no `DEV_MODE`) | `app/hat/data/sessions.py`, `app/hat/clock.py` (copied from `utils/market_hours.py`, `utils/clock.py`) | 4, K1 |
| Symbols | N (HAT-owned conversion since K2; the legacy `SymbolMaster` inherits it and keeps only options, futures and metadata) | `app/hat/data/symbols.py` | 4, K2 |
| Risk authority | N (`evaluate_signal` in HAT since K1; the legacy `RiskManager` no longer imports HAT) | `app/hat/risk/service.py` | 10, K1 |
| Order intent, Decimal money | R + N (`to_decimal` / `to_money` copied into HAT since K1) | `services/execution/intent.py`, `app/hat/money.py` | 10, 13, K1 |
| Broker | N (HAT interface, GTT; the Fyers broker, session, login and token store moved into HAT in K2, the legacy classes inherit them until K4 and K5) | `app/hat/execution/fyers_broker.py`, `fyers_session.py`, `fyers_auth.py`, `fyers_tokens.py`, `app/hat/api/fyers_auth.py` | 13, K2 |
| VCP detector | M (polars port) | `services/vcp/pattern.py` | 8 |
| Backtest metrics | N (copied into HAT in K1; the VCP detector's frozen outputs are in `tests/hat/fixtures/vcp_oracle.json`) | `app/hat/backtest/metrics.py` | 12, K1 |
| Bhavcopy parsing ideas | M | `data_platform/pipelines/phase1_build.py`, `backend/scripts/load_bhavcopy.py` | 5 |
| Universe constituents | R (seed input) | `services/universe/`, `services/index_universe_loader.py` | 9 |
| Test fakes | N (HAT-owned since K1) | `tests/hat/fakes/` (`FakeBroker`, the data-socket fake, the Fyers SDK recorder), `app/hat/clock.py` (`FixedClock`) | all, K1 |
| Criteria, scan, exits, lifecycle, allocator, regime, Telegram, scheduler, event log, Parquet store | N | — | 2-17 |

#### HAT-OVR-040: One fake broker and one risk authority
- **Statement:** HAT keeps one `FakeBroker` (`tests/hat/fakes/broker.py`) and one risk authority (`evaluate_signal` in `app/hat/risk/service.py`) instead of parallel versions. The legacy `FakeBroker`, `RiskManager` and `LiveMarketService` are legacy copies that the cleanup PRs (K3 to K5) delete; HAT order paths never call them. [Reworded in K1; it said HAT extends the legacy `RiskManager` in place.]
- **Source:** MA "Reuse vs build"; PLAN §2, §6 preamble.
- **PR:** 10, 13, 14
- **Verification:** CONF `no_duplicate_fakes_or_authorities`
- **Acceptance:**
  - Property: across `backend/`, HAT has exactly one class implementing the broker fake (`tests/hat/fakes/broker.py:FakeBroker`) and exactly one risk authority function (`evaluate_signal`, defined only in `app/hat/risk/service.py`); the only other definitions of these names are the listed legacy copies. Harm: two risk authorities or two fakes drift apart and a check passes against the wrong one.

#### HAT-OVR-041: HAT stands alone from the legacy code
- **Statement:** Nothing under `app/hat` or `tests/hat` imports `app.main`, a legacy router, a legacy model or a legacy test helper, and no HAT suite loads the legacy app or its manual-trading singletons. The few legacy modules HAT still imports (the Fyers stack, the symbol master, the live-feed service, the database and paths modules) are listed in the check and move into HAT in K2, K3 and K6. The legacy risk manager imports nothing from HAT.
- **Source:** Cleanup plan K1 (D59, D60).
- **PR:** K1
- **Verification:** CONF `imports.hat_decoupled_from_legacy`, CONF `imports.hat_suites_boot_no_legacy_app`, CONF `imports.legacy_does_not_import_hat`
- **Acceptance:**
  - Property: the set of legacy modules imported by HAT code or HAT tests equals the listed set, and collecting every HAT suite in a fresh interpreter loads none of `app.main`, `app.routers`, the risk manager, the order execution service, the position sync service or the live exit service. Harm: deleting a legacy module silently breaks HAT, or a HAT test passes only because the legacy app booted around it.

## §5 Repo conflict resolutions (C1-C12)

Where the design docs and the repo differ, these resolutions apply (PLAN §3). The docs win on intent;
the resolution says how it lands in this repo.

| # | Docs say | Repo today | Resolution |
| --- | --- | --- | --- |
| C1 | Own polars indicators (T11); Parquet + DuckDB (T6) | pandas everywhere; no duckdb | `app/hat/**` uses polars + duckdb; existing modules stay pandas. Arrow bridge only at boundaries. VCP detector ported to polars with a parity property against `services/vcp/pattern.py`. |
| C2 | One risk authority = SmartTrader governance layer, extended | `RiskManager` (order-level checks, float config) | `evaluate_signal(...)` composes the market-hours and price-band outcomes with HAT policy, sizing, caps, kill switches and invariants (pure functions in `app/hat/risk/`). Since K1 it lives in `app/hat/risk/service.py`, not in `RiskManager` (which imports nothing from HAT); the legacy order paths are unchanged. HAT money math is Decimal. |
| C3 | Docker Compose; Linux host | Windows-first `start.bat` | Additive `deploy/hat/docker-compose.yml`; Windows dev keeps working. |
| C4 | Four processes | One FastAPI process; live feed singleton | HAT runs in the backend process as lifespan tasks gated by `HAT_ENABLED` (default false). Logical split kept: separate modules, restart-safe jobs. |
| C5 | OpenAlgo behind the broker interface; keys in OpenAlgo | Fyers creds in `.env` + `fyers/config/access_token.json` | Direct Fyers adapter. Fyers secrets stay where they are. `hat_broker_account.openalgo_ref` kept as a nullable column. |
| C6 | Regime + allocator set sizing; risk 0.25% | VCP module: NIFTY50 200DMA BULL/BEAR → 1%/0.5%, own halt flag and positions | VCP module left intact during the MVP; S1 supersedes it. Retirement of `services/vcp*` is a follow-up. Kill-switch interplay per Q6. |
| C7 | `hat_trading_calendar` drives sessions | `MarketHoursPolicy.holidays` empty | PR 4 loads NSE holidays into `hat_trading_calendar`. Since K1 HAT's own `MarketHoursPolicy` (`app/hat/data/sessions.py`) is built from the loaded calendar (`MarketCalendar.policy`) and passed to its users; nothing is installed into the legacy `market_hours`, which no longer sees holidays. |
| C8 | 41 tables, uuid v7 PKs, JSONB, triggers, partitioned event log, `revoke` | `create_all` + `ensure_*`; integer PKs; SQLite tests | Follow the repo mechanism (03 §10). |
| C9 | Data root for 2010+ history | `data_system/` "2025+ active scope" | `HAT_DATA_ROOT = DATA_ROOT / "hat"` in `app/paths.py`, env-overridable, gitignored. |
| C10 | Telegram `/mode [bot] signal\|confirm\|auto` | — | Superseded by B1 (revised): `shadow\|confirm\|auto`. |
| C11 | `hat_event` append-only via trigger and `revoke update, delete` | One DB user | Trigger only in the MVP; role split per Q7. |
| C12 | Tag = compact signal ID + purpose | Fyers tag `^[A-Za-z0-9]{1,20}$`, prefix `ST` used by the terminal | `HT` + 2-character purpose code + 16-character encoding of the signal uuid (20 characters). |

#### HAT-OVR-050: Conflict resolutions C1-C12 hold in the code
- **Statement:** Each resolution C1-C12 is implemented as stated in the §5 table, and each is restated as a requirement in the spec file that owns its area.
- **Source:** PLAN §3.
- **PR:** 2-17 (per row: C1 5/6/8, C2 10, C3 17, C4 2/17, C5 13, C6 —, C7 4, C8 2/3, C9 2, C10 16, C11 3, C12 13)
- **Verification:** CONF `conflict_resolutions_traced`
- **Acceptance:**
  - Property: every resolution C1-C12 is cited by `PLAN C<n>` in the **Source:** line of at least one requirement in 01-10. Harm: a resolution is agreed in the plan but nobody implements or verifies it.

#### HAT-OVR-051: Existing SmartTrader paths are unchanged except where a resolution says so
- **Statement:** HAT PRs leave existing SmartTrader behaviour unchanged, except the holiday calendar fix (C7) and the additive hooks named in §4.
- **Source:** PLAN C2, C4, C6, C7.
- **PR:** 2-17
- **Verification:** CONF `existing_suite_green`
- **Acceptance:**
  - Property: on every HAT PR, the pre-existing backend and frontend test suites pass without edits to existing test files, except the holiday regression added for C7. Harm: HAT silently changes terminal, screener or VCP behaviour the owner relies on.

#### HAT-OVR-052: HAT data lives under `HAT_DATA_ROOT`
- **Statement:** All HAT Parquet, raw bhavcopy zips and backtest outputs live under `HAT_DATA_ROOT` (default `data_system/hat/`, env-overridable), which is gitignored.
- **Source:** PLAN C9, Q9.
- **PR:** 2, 5
- **Verification:** CONF `hat_data_root_paths`
- **Acceptance:**
  - Property: every path the `app.hat` package writes to is derived from `app.paths.HAT_DATA_ROOT`, and `.gitignore` excludes that directory. Harm: gigabytes of market data get committed or written outside the backed-up tree.

## §6 Non-goals and deferred items

| Item | Status | Source |
| --- | --- | --- |
| OpenAlgo | Not in the MVP; `openalgo_ref` column kept nullable | OWN-20OCT, PLAN C5 |
| Zerodha and Kotak Neo adapters | Post-MVP; the broker interface allows them | I8; OWN-20OCT |
| US markets, US broker (IBKR) | P2 | MA "Phase progression"; I17 |
| Regime model v2 (HMM/GMM) | P2 (spec only in 05 §7) | PL3 |
| Screener-driven universes and fundamentals (XBRL) | P2 | PL5; I5 |
| Multi-user | Decided in P3 | MA "Phase progression" |
| WhatsApp | Out of scope | I12/I13 |
| Parabolic short, any short | Out of scope (long only) | BL "Qullamaggie-style bot" |
| Hire a Trader MCP server for chat | Later, optional, chat only | DS "MCP role" |
| Model-generated PDF summaries for approval cards | Later, advisory only, never a gate | DS "Announcements for EP catalysts" |
| Manual exit button for a single position | Never (engine-only exits) | BL "Governance" |
| Auto mode as a product feature | Never without SEBI empanelment; personal accounts only | MA "Execution modes" |

#### HAT-OVR-060: Deferred items are absent from MVP code paths
- **Statement:** No MVP code path places orders through OpenAlgo or a non-Fyers broker, shorts a stock, or lets a model output gate a trade.
- **Source:** MA "Execution modes", "Phase progression"; BL "Qullamaggie-style bot"; DS "Announcements for EP catalysts"; OWN-20OCT.
- **PR:** 13, 15
- **Verification:** TLA `ExecutionRouter!LongOnly`, CONF `no_deferred_integrations`
- **Acceptance:**
  - Property: in every reachable state of the router model, every entry order has side BUY and every exit order sells at most the open quantity. Harm: a short or over-sell creates an unintended position the risk policy never sized.
  - Property: no module under `app.hat` imports an OpenAlgo, Zerodha, Kotak or LLM client. Harm: an unverified integration enters an order or gating path.

#### HAT-OVR-061: Auto mode is personal-account only
- **Statement:** Auto mode is a personal switch for the owner's own accounts and is not exposed as a product feature.
- **Source:** MA "Execution modes".
- **PR:** 15, 16
- **Verification:** CONF `auto_mode_single_user`
- **Acceptance:**
  - Property: every `hat_broker_account` a bot in `auto` mode is bound to has `user_id` equal to the configured `HAT_USER_ID`, and no API route can set a bot's mode (only Telegram from the allowlisted chat can). Harm: orders are placed for someone else's account without SEBI empanelment.
