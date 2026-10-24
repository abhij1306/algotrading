# Architecture

The map. Rules: `invariants.md`. Decisions: `decisions.md`. Operations: `runbook.md`.

## Shape

One FastAPI process (`backend/app/main.py`), one PostgreSQL database, Parquet under
`HAT_DATA_ROOT`. All of HAT is `backend/app/hat/`, which imports only `app.base`,
`app.database`, `app.paths` and `app.utils.env_loader`. The runtime starts only with
`HAT_ENABLED=true`. The frontend (`frontend/`) is a read-only dashboard; Telegram is the control
surface; the owner's setup acts go through the admin CLI (`admin.py`).

| Package | Holds |
| --- | --- |
| `data` | NSE MCP and bhavcopy ingest, quality gate, reference data and calendar, universe, corporate actions (`corporate_actions`, imports `ca_import`/`ca_research`/`ca_dividends`, corrections `ca_corrections`, `known_on` rules `ca_known`, versions `ca_versions`, `point_in_time`, `ca_demergers`, notices
`demerger_notice`), ISIN lineage (`lineage`), history build, features, Parquet store (`store`), live feed and 5-minute bars, gap scan, announcements |
| `engine` | Criteria library, strategy config, scan, trigger monitor, exits, stops, entries, demerger exits (`demerger_exit`), shadow engine, positions and trade results (`positions`, `trade_dividends`), approvals, portfolio (regime, health, allocator, sleeve lifecycle) |
| `risk` | Risk policy versions, sizing, caps, exposure ladder, kill switches, drawdown, modes, `evaluate_signal` |
| `execution` | `HatBroker` and its adapters (Fyers `fyers_*`, Zerodha `kite_*`: payloads, transport, session, tokens, login, IPv4 pin), router, order submit and tags (`submit`), stop placement (`protect`, `stop_plans`), GTT upkeep, trade management (`manage`), fill sync (`fills`), position desk, reconciliation, mode gates |
| `telegram` | Bot API client, long poll, commands, approval cards, notifier, reports |
| `ops` | Scheduler, job runner, partitions, retention, runtime lock |
| `daily` | `HatRuntime` (start, recover, stop), the frozen `Assembly` and job table (`assembly.daily_jobs`), periodic loops, live-day and nightly job bodies |
| `backtest` | Day loop, evidence for the gates (`RunMetrics`), fills, deflated Sharpe, run records. `engine` and `risk` import only `spec` and `evidence` |
| `models`, `api`, `read` | SQLAlchemy `hat_*` models; read-only dashboard and broker login routes (`broker_auth`); the read model the dashboard and Telegram share (no writer import) |

Top level: `events.py` (event log), `money.py`, `costs.py` (slippage and the versioned cost
models in `cost_models/`), `clock.py`, `ids.py` (uuid v7), `settings.py`,
`schema.py` (`init_db` upkeep, `PG_DDL`, drift check), `admin.py`.

## Data flow

Nightly (jobs chained with `after`, keyed `(job, trade_date)`):

```text
ingest.nightly (NSE MCP from 18:00, bhavcopy 22:00) -> raw bars + index/breadth, then each after it:
  quality.gate ─┐
  ca.adjust ────┴> features -> regime -> allocate -> scan -> watchlist digest
  (ca.adjust: corporate actions, the day's dividends and demerger notices, lineage, trade dividends)
  coverage.check, exits.close_gaps
```

Intraday (one `LiveFeed`, one Fyers data socket):

```text
ticks -> 5-minute bars -> TriggerMonitor -> signal -> evaluate_signal
  -> ExecutionRouter by mode: shadow (virtual fill) | confirm (card, Take/Skip) | auto
  -> reserve_order -> send_order -> HatBroker -> fill sync -> StopDesk.protect (stop, then GTT)
  -> TradeManager: exit stack, 15:20-15:25 MA exits
```

## Key seams

- **Pure core shared by backtest and live:** `scan.scan_bot`, `exits.evaluate/apply`, the criteria
  and `decide` read no clock, database or file; `backtest/loop.py` calls the same code.
- **One strategy registry:** `engine/strategy/registry.py:STRATEGIES`, read from the seed files
  `engine/strategy/seeds/<name>.yaml` (a header with `sleeve` and `persona`, then the config).
  A strategy has one name: the file name, its config's `bot` and `hat_strategy.name`
  (`save_draft` refuses a config whose `bot` is another name), and every key reads it.
  Adding a strategy is one seed file plus any new criteria; its `p_max` and `base_weight` go into
  the risk policy as a new policy version.
- **One risk authority:** `risk/service.py:evaluate_signal` -> `decide`, the only caller of
  `size_position`.
- **One path to an order:** `execution/submit.py:send_order` is the only way an entry reaches
  the broker (stops, sales and reads go through `HatBroker` in `execution/`);
  one adapter serves it, the active broker's (`HAT_BROKER`: `FyersHatBroker` or `KiteHatBroker`,
  `accounts.active_adapters`), `tests/hat/fakes/broker.py:FakeBroker` the only fake.
- **One position desk per account** (`execution/position_desk.py`): one write lock per position
  for every stop write and sale.
- **One runtime per database** (`ops/runtime_lock.py`, a session advisory lock); one session owner
  per account (`SessionRegistry`), one token store per broker (`fyers_tokens`, `kite_tokens`), one
  live socket (Fyers: market data stays on Fyers whichever broker takes orders).
- **One clock:** code that needs "now" takes a `clock.Clock`. `governance.py:Mode` is the only
  spelling of shadow/confirm/auto; `execution/modes.py` holds the gates.
- **Events:** `events.record` writes each state change's event in its transaction; trade state
  replays from events (`journal.replay_state`).
- **Conformance:** `tests/hat/conformance/` enforces the structural rules (imports, no private
  imports, read-only routes, one Decimal boundary, admin CLI calls services).

## Storage

- PostgreSQL `hat_*` tables (created by `init_db`; `hat_event` monthly-partitioned): reference
  (instruments one per ISIN, symbol history, calendar, bands, corporate actions and their
  versions, lineage), strategy, risk and bots, portfolio, runtime (watchlist, signal, approval,
  order, fill, position, trade result, holding baseline), ops (events, job runs, Telegram, snapshots, backtest runs).
- Parquet, date-partitioned, read through DuckDB: `bars_daily_raw` (immutable) and its views in
  `data/store.py` (`bars_daily_cap` price basis, `bars_daily` total return), `index_daily`,
  `features_daily`, breadth, `bars_5m` (raw) and `bars_5m_rejects`.
- `features_daily/_state.json` holds the inputs digest and per-day fingerprints: changed days,
  chains or inputs are recomputed; the backtest refuses stale features
  (`backtest_features_stale`).

## Daily cycle (IST, trading days; `daily/assembly.py:daily_jobs`)

| Time | Job |
| --- | --- |
| 00:30 / 01:00 | events.partitions, retention.purge |
| 07:00-07:45 | calendar.refresh, bands.nightly (Saturday 09:00 reference.weekly) |
| 08:30, 09:05 | Telegram login prompts |
| 08:40-08:45 | ca.rescale, gtt.check, live.prepare |
| 09:15-09:20 | modes.apply, gapscan |
| 09:20-15:20 | triggers, routing, stops on fill |
| 15:20-15:25 | exits.close; 15:25 gtt.swap |
| 15:30-15:45 | watchlist.expire, trade.end_session, reports and digests |
| 18:00 on | the nightly chain |
| 23:00 / 23:15 / 23:30 | gtt.verify, journal.check, snapshot.bots |

Each job has a deadline (`JOB_DEADLINES`); failing, missing or overrunning it raises one URGENT
alert. Periodic loops (`daily/periodic.py`, every 5 s): live exits and intraday drawdown kills;
housekeeping (session watch, fill sync, approval expiry, reconciliation every 5 min, stranded
entries); announcements. A stalled loop alerts `periodic.stalled`. Broker calls have timeouts and
a circuit breaker; every DB transaction runs under a 120 s `statement_timeout`.

## Verification and CI

TLA+ models in `verification/tla/`, Hypothesis properties in `backend/tests/hat/properties/`,
Postgres-only checks in `tests/hat/db/`, conformance in `tests/hat/conformance/`.
`.github/workflows/ci.yml` (every PR and push to main): `backend`, `backend-postgres`,
`verification-conformance`, `verification-properties`, `frontend`, `api-schema`. `tla.yml` runs
the TLC models when `verification/**` or a Python file named in a model's `code:` comments
changes, weekly and on demand.

## Startup and routes

Startup (`main.py` lifespan): `init_db` (a database that is down stops the app), then
`check_schema_drift` refuses any difference from the models and logs the DDL. With
`HAT_ENABLED=true` it then checks the Fyers token, and `daily/runtime.py` refuses a non-Postgres
database, a proxy variable that would carry Fyers (or, with `HAT_BROKER=zerodha`, Kite) traffic,
missing Kite credentials when Zerodha is the active broker and a bad Telegram setup, reconciles
every account, and starts the scheduler, live feed, periodic loops and Telegram. Shutdown stops
the runtime, then the engine.

Routes: `GET /`, `GET /ping`; the Fyers login under `/api/hat/auth/fyers` (`GET /status`, `GET
/url`, `POST /token`, `POST /disconnect`; no callback: paste the `auth_code`), and the same four
under `/api/hat/auth/zerodha` (paste Kite's `request_token`; `status` says which broker is active); the read-only
dashboard under `/api/hat` (`/status`, `/bots`, `/watchlist`, `/signals`, `/positions`,
`/allocations`, `/regime`, `/events`), polled every 15 s by `frontend/app/hat/page.tsx`.

Environment: `.env.example` lists every variable: `DB_*`; the Fyers app `CLIENT_ID`,
`SECRET_KEY`, `REDIRECT_URI`; `HAT_BROKER` and the Kite app `KITE_API_KEY`, `KITE_API_SECRET`,
`KITE_REDIRECT_URI`; `HAT_ENABLED`, `HAT_USER_ID`, `HAT_DATA_ROOT`, the MCP URLs and
Telegram secrets; `BACKEND_HOST`/`BACKEND_PORT` and logging for `backend/start_server.py`.
