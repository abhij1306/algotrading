# Architecture

The map. Rules: `invariants.md`. Decisions: `decisions.md`. Operations: `runbook.md`.

## Shape

One FastAPI process (`backend/app/main.py`), two PostgreSQL databases on one server (hat_live,
hat_state), JSON configs and Parquet under `HAT_DATA_ROOT` (Storage). All of HAT is `backend/app/hat/`, which imports only `app.base`,
`app.database`, `app.paths` and `app.utils.env_loader`. The runtime starts only with
`HAT_ENABLED=true`. The frontend (`frontend/`) is a read-only dashboard and a Backtests page (read,
compare and start research runs) whose components sit in atomic layers under `frontend/components/`: `atoms/` and `molecules/` take props only,
`organisms/` own the query hooks, `templates/` lay out the shell and page. Telegram is the control
surface; the owner's setup acts go through the admin CLI (`admin.py`).

| Package | Holds |
| --- | --- |
| `data` | NSE MCP and bhavcopy ingest, quality gate, reference data (`instruments`, symbol history `symbol_history`, broker symbols `broker_symbols`, ISIN classes `isin`) and calendar, universe, corporate actions (`corporate_actions`, the bars gate `ca_gate`, the importers' shared apply step `ca_apply`, one-off imports `ca_import`/`ca_research` (rows `ca_research_rows`)/`ca_dividends`, dividends `ca_payouts`, the nightly `bc` file `ca_bc` and its import `ca_nightly`, corrections `ca_corrections`, `known_on` rules `ca_known`, versions `ca_versions` (its `Ledger` the one writer of `hat_corporate_action`), `point_in_time`, `ca_demergers`, point-in-time index membership `index_membership`, the daily data update `update` (NSE's text files `nse_files`), notices
`demerger_notice`), the adjusted series' input (`chains`), ISIN lineage (`lineage`, its journal `lineage_store`, `lineage_review`, `lineage_override`, `nse_changes`), history build, features (its state and staleness `features_state`), Parquet store (`store`, its coverage reports `store_coverage`), the data verification against independent references (`verify`, runbook), live feed and 5-minute bars, gap scan, announcements |
| `engine` | Criteria library, strategy config and versions (`strategy`: `versions`, the release gate `release`, the criterion catalogue `catalogue`), scan, trigger monitor, exits, stops, entries, demerger exits (`demerger_exit`), shadow engine, positions and trade results (`positions`, `trade_dividends`), approvals, portfolio (regime, health, allocator, sleeve lifecycle) |
| `risk` | Risk policy versions, sizing, caps, exposure ladder, kill switches, drawdown, modes, `evaluate_signal` |
| `execution` | `HatBroker` and its adapters (Fyers `fyers_*`, Zerodha `kite_*`: payloads, transport, session, tokens, login, IPv4 pin), router, order submit and tags (`submit`), stop placement (`protect`, `stop_plans`), GTT upkeep, trade management (`manage`), fill sync (`fills`), position desk, reconciliation, mode gates |
| `telegram` | Bot API client, long poll, commands, approval cards, notifier, reports |
| `ops` | Scheduler, job runner, partitions, retention, runtime lock, the out-of-process health watch (`watch`) |
| `daily` | `HatRuntime` (start, recover, stop), the frozen `Assembly` and job table (`assembly.daily_jobs`), periodic loops, live-day and nightly job bodies (`nightly`, `ca.adjust` in `nightly_ca`, their inputs `nightly_deps`) |
| `backtest` | Day loop, evidence for the gates (`RunMetrics`), fills, deflated Sharpe, run folders (`folder`, `standard`, `trials`), promotion criteria and review (`criteria`, `review`). `engine` and `risk` import only `spec` and `evidence` |
| `configs` | The config records (`records`: strategies, versions, risk policy versions, broker accounts, bots, regime models, criterion types), their file store (`store`) and a session's unit of work over them (`unit.configs_of`) |
| `models`, `api`, `read` | SQLAlchemy `hat_*` models; read-only dashboard and broker login routes (`broker_auth`); the read model the dashboard and Telegram share (no writer import) |

Top level: `events.py` (event log), `money.py`, `costs.py` (slippage and the dated cost
schedules in `cost_models/<version>/<effective_from>.yaml`), `clock.py`, `ids.py` (uuid v7), `settings.py`,
`schema.py` (`init_db` upkeep, `PG_DDL`, drift check), `storage.py` (which database a table lives
in, `HatSession`), `admin.py`.

## Data flow

Nightly (jobs chained with `after`, keyed `(job, trade_date)`):

```text
ingest.nightly (NSE MCP from 18:00, bhavcopy 22:00) -> raw bars + index/breadth, then each after it:
  quality.gate ─┐
  ca.adjust ────┴> features -> regime -> allocate -> scan -> watchlist digest
  (ca.adjust: corporate actions, the day's dividends and demerger notices, lineage, trade dividends,
  the version-ledger projection check)
  coverage.check, exits.close_gaps
```

Without the live service (research), `python -m app.hat.data.update` (`data/update`, a systemd user
timer at 19:30 IST) runs the data jobs of this chain without the MCP servers, plus the download
tools, with per-step catch-up (runbook, Daily data update).

Intraday (one `LiveFeed`, one Fyers data socket):

```text
ticks -> 5-minute bars -> TriggerMonitor -> signal -> evaluate_signal
  -> ExecutionRouter by mode: shadow (virtual fill) | confirm (card, Take/Skip) | auto
  -> reserve_order -> send_order -> HatBroker -> fill sync -> StopDesk.protect (stop, then GTT)
  -> TradeManager: exit stack, 15:05-15:10 MA exits
```

## Key seams

- **Pure core shared by backtest and live:** `scan.scan_bot`, `exits.evaluate/apply`, the criteria
  and `decide` read no clock, database or file; `backtest/loop.py` calls the same code. A run
  passes the scan a cache (`ScanInputs.memo`) that holds only values computed from bars up to
  their own date, and reads back `ScanResult.funnel`, the stock-days passing each setup gate.
- **Rotation strategies** (a config's `rotation` block) skip the scan and the exit stack: the
  pure ranking and order rules are `engine/rotation.py`, the backtest's book `backtest/rotation.py`.
- **One strategy registry:** `engine/strategy/registry.py:STRATEGIES`, read from the seed files
  `engine/strategy/seeds/<name>.yaml` (a header with `sleeve`, `persona` and `regime`, then the
  config). `regime` names the strategy's row in the regime model's multiplier table; a strategy
  whose row is missing, or one not registered, gets each state's least multiplier.
  A strategy has one name: the file name, its config's `bot` and its strategy record's `name`
  (`save_draft` refuses a config whose `bot` is another name), and every key reads it.
  Adding a strategy is one seed file plus any new criteria; its `p_max` and `base_weight` go into
  the risk policy as a new policy version.
- **A playbook's handling comes from its criteria, not its name:** a playbook is pre-open
  (`Playbook.preopen`) when one of its criteria declares `preopen` in its `data_needs`; the gap
  scan builds its candidates (gapped at least the least enabled `gap_up` minimum of the active
  bots' pre-open playbooks, `gap_floor`, each playbook's own minimum applied at its trigger; the
  setup judged on the bars to the previous session, `ep_setup_passes`), so its trigger level can only be the opening range (refused on
  save otherwise) and the nightly scan skips it. The backtest picks its candidates the same way
  each session (`loop._gappers`) and decides them on the stored 5-minute bars, or on the
  calibrated daily estimate where none are stored (`intraday.EP_ESTIMATE`); a playbook a run's
  known gaps name has its trades left out of comparisons, per strategy version and playbook. The trigger inputs are built by one function each,
  for live, the backtest and the scan: the stop history at each rule's own length
  (`stops.history_inputs`), the entry stop (`trigger.entry_stop`) and the entry-filter context
  (`trigger.daily_context`, `entry_filter_context`).
- **One risk authority:** `risk/service.py:evaluate_signal` -> `decide`, the only caller of
  `size_position`.
- **One path to an order:** `execution/submit.py:send_order` is the only way an entry reaches
  the broker (stops, sales and reads go through `HatBroker` in `execution/`);
  one adapter serves it, the active broker's (`HAT_BROKER`: `FyersHatBroker` or `KiteHatBroker`,
  `accounts.active_adapters`), `tests/hat/fakes/broker.py:FakeBroker` (the Fyers transport
  beneath `FyersHatBroker`) the only fake.
- **One position desk per account** (`execution/position_desk.py`): one write lock per position
  for every stop write and sale.
- **One runtime per hat_state** (`ops/runtime_lock.py`, a session advisory lock); one session owner
  per account (`SessionRegistry`), one token store per broker (`fyers_tokens`, `kite_tokens`), one
  live socket (Fyers: market data stays on Fyers whichever broker takes orders).
- **One clock:** code that needs "now" takes a `clock.Clock`. `governance.py:Mode` is the only
  spelling of shadow/confirm/auto; `execution/modes.py` holds the gates.
- **Events:** `events.record` writes each state change's event in its transaction; trade state
  replays from events (`journal.replay_state`).
- **Conformance:** `tests/hat/conformance/` enforces the structural rules (imports, no private
  imports, read-only routes, one Decimal boundary, admin CLI calls services).

## Storage

- **hat_live** (`app.base.Base`, `DB_NAME`): the stock data only. Instruments one per ISIN, symbol
  history, calendar, bands, industries, corporate actions and their versions, lineage, index
  membership by date, announcements, demerger notices, broker symbols, universes, quality checks,
  and the quarterly financial results (`hat_financial_result`, one row per filing version,
  append-only; `hat_financial_growth`, derived from it; `data/results/`, read point in time with
  `results_as_of`), and the fundamentals store built from the same cache
  (`hat_fundamental_filing`, `hat_fundamental_fact`, `hat_fundamental_metric`;
  `data/fundamentals/`, docs/fundamentals.md).
  Only the data jobs and importers write it (`HatDataSessionLocal`, `db_session_scope(writes_data=True)`:
  the nightly ingest, `ca.adjust`, quality gate, reference, bands, calendar, announcements poll,
  the `app.hat.data` command lines and the admin data commands `admin.DATA_COMMANDS`); every other
  connection is `default_transaction_read_only`, so a stray write fails.
- **hat_state** (`app.base.StateBase`, `DB_STATE_NAME`, default `hat_state`; same server and
  credentials): the trading and runtime state. Watchlist, signal, approval, order, fill, position,
  trade result, holding baseline, kill switches, mode changes, exposure, regime state, allocations,
  events (`hat_event` monthly-partitioned), job runs, broker sessions, Telegram, snapshots, the
  runtime's heartbeat (`hat_heartbeat`).
- **Data root and data repo**: what lives under `HAT_DATA_ROOT` and beside it is `docs/data-root.md`;
  the private data repo holds the store manifests and reference CSVs (`app.hat.data.record`, pushed
  by the daily update) and the snapshot releases (`scripts/hat_snapshot.py`; runbook, Data record
  and Snapshot and restore).
- **Configs** (`app.hat.configs`): `HAT_DATA_ROOT/admin/config/<kind>/<id>/<rev>.json`, one file
  per revision, schema-validated on load and never changed once written (`<kind>/<id>` is the
  record's uuid; a criterion type's is `<code>@<version>`). `<kind>/in_force.json` names each
  record's revision in force; it is replaced atomically and each of its states is kept in
  `<kind>/pointers/<seq>.json`. A session reads a kind once per transaction (`configs_of`), and
  its commit writes each changed record as its next revision, refused (`config_conflict`) when
  another writer moved that record first. The per-record rules the old CHECKs and triggers kept
  are `records.record_rules`.
- Every session (`HatSession`) binds both databases and the config store; a statement naming
  tables of both databases is refused (`CrossDatabaseError`), so a key from one database to the
  other, or to a config record, is a plain column checked in code (`bot_id`, `strategy_version_id`,
  `risk_policy_version_id`, `instrument_id`, `broker_account_id`, `announcement_id`,
  `market_id`, `model_version_id`, `job_run_id`, `backtest_run_id`).
- Parquet, date-partitioned, read through DuckDB: `bars_daily_raw` (immutable) and its views in
  `data/store.py` (`bars_daily_cap` price basis, `bars_daily` total return), `index_daily` (the
  nightly indices, and NIFTY 500 TRI fetched by the nightly ingest from niftyindices), `features_daily`, breadth, `bars_5m` (raw) and `bars_5m_rejects`. `raw_tri/` keeps niftyindices' TRI answers `verify-tri --fetch` saved (one per year and fetch, never rewritten). `ca_bc/` keeps each night's
  NSE `bc` file as fetched (`bc_<trade date>.csv`, immutable; a later, different copy of that
  date is kept beside it as `bc_<trade date>_<sha256>.csv`). `raw/nse-results/` and
  `raw/secondary-results/` keep every answer the results fetch got, gzipped under its sha256 with a
  `manifest.jsonl` of URLs (`data/results/raw.py`).
- `backtests/runs/<run_id>/` holds every run's folder in the Backtest Result Standard
  (`docs/backtests/result-standard.md`: `run.json` validated against `run.schema.json`,
  `config.json`, the CSVs, `audit.json`, `data_audit.json` and, for a completed run,
  `evidence.json`; `backtest/folder.py`), `backtests/data_audits/` each data audit by data
  snapshot, window, 5-minute use, golden file and verification report (`data/audit.py`),
  `audits/golden/` the golden samples and their check results (`data/golden.py`,
  `docs/backtests/golden-sample.md`), `audits/verify-<date>/` the data verification reports
  (`data/verify/`, each check against an independent reference; `verify_reference/` keeps the
  public reference files it downloaded, each written once), and `backtests/trials.json` the DSR trials of each strategy, universe and window
  (`backtest/trials.py`); no backtest writes the database. The process:
  `docs/backtests/process.md`. Promotion and the gates read a completed run whose `audit.json`
  passed or warned, through its `evidence.json` (metrics and trade buckets,
  `backtest/evidence.py`); a strategy version names its evidence by run id.
  `backtests/jobs/<job id>/` holds an app-started job's variants file and its child's stdout and
  stderr (`backtest/jobs.py`).
- `etf/` holds the idle-cash ETF overlay's data (NIFTYBEES, GOLDBEES, LIQUIDBEES daily and
  5-minute bars, LIQUIDBEES' total-return series, NIFTY 50 and the reference TRIs, a manifest),
  written by `scripts/data/overlay_etf.py store` and read by an overlay run
  (`backtest/etf_data.py`).
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
| 09:20-15:05 | triggers, routing, stops on fill |
| 15:05-15:10 | exits.close; 15:10 gtt.swap; 15:15 sales cut-off (continuous trading ends, closing auction) |
| 15:30-15:45 | watchlist.expire, trade.end_session, reports and digests |

The session's own stored hours move the times: live.prepare, modes.apply, gapscan, the regime
gate, the pre-open capture and the bar grid follow its open (a 14:20 Muhurat open: prepare 13:50,
modes 14:20, gapscan 14:21-14:25); exits.close, exits.close_shadow, gtt.swap, the sales'
cut-off and the trigger window's end follow its continuous-trading end (15 minutes before its
close, for NSE's closing auction), and watchlist.expire and trade.end_session its close.
| 18:00 on | the nightly chain |
| 23:00 / 23:15 / 23:30 | gtt.verify, journal.check, snapshot.bots |

Each job has a deadline (`JOB_DEADLINES`); failing, missing or overrunning it raises one URGENT
alert. Periodic loops (`daily/periodic.py`, every 5 s): live exits and intraday drawdown kills;
housekeeping (session watch, fill sync, approval expiry, reconciliation every 5 min, stranded
entries, positions opened after live.prepare priced and subscribed, a failed feed-health kill
write retried); announcements. Each loop, the scheduler's jobs and the trigger writes run on
their own threads, never the loop's default pool. A stalled loop alerts `periodic.stalled`. Broker calls have timeouts and
a circuit breaker (failing two minutes in a row: `broker.unreachable`); every DB transaction runs
under a 120 s `statement_timeout`. The scheduler stamps `hat_heartbeat` every tick; the host's
health watch (`ops/watch.py`, a 5-minute systemd timer, runbook) alerts a stale heartbeat, a
database down, a low disk, a failed or late daily data update and a failed verify check, once
per event.

## Verification and CI

TLA+ models in `verification/tla/`, Hypothesis properties in `backend/tests/hat/properties/`,
Postgres-only checks in `tests/hat/db/`, conformance in `tests/hat/conformance/`.
`.github/workflows/ci.yml` (every PR and push to main): `backend`, `backend-postgres`,
`verification-conformance`, `verification-properties`, `frontend`, `api-schema`. `tla.yml` runs
the TLC models when `verification/**` or a Python file named in a model's `code:` comments
changes, weekly and on demand.

## Startup and routes

Startup (`main.py` lifespan): `init_db` creates both databases' tables and runs their upkeep (a
database that is down stops the app), then `check_schema_drift` refuses any difference from the
models in either and logs the DDL. With
`HAT_ENABLED=true` it then checks the Fyers token, and `daily/runtime.py` refuses a non-Postgres
database, a proxy variable that would carry Fyers (or, with `HAT_BROKER=zerodha`, Kite) traffic,
missing Kite credentials when Zerodha is the active broker and a bad Telegram setup, reconciles
every account, and starts the scheduler, live feed, periodic loops and Telegram. Shutdown stops
the runtime, then the engines.

Routes: `GET /`, `GET /ping`; the Fyers login under `/api/hat/auth/fyers` (`GET /status`, `GET
/url`, `POST /token`, `POST /disconnect`; no callback: paste the `auth_code`), and the same four
under `/api/hat/auth/zerodha` (paste Kite's `request_token`; `status` says which broker is active); the read-only
dashboard under `/api/hat` (`/status`, `/watchlist`, `/signals`, `/positions`,
`/allocations`, `/events`), polled every 15 s by the panels `frontend/app/hat/page.tsx` composes;
the backtests under `/api/hat/backtests` (`/runs`, `/runs/{run_id}` and
`/runs/{run_id}/files/{name}` read the run folders; `/options`, `/jobs`, and `POST` to start a
research run or a sweep as a job: the backtest command line in a child process, one at a time,
never a promotion run; documented in `app/hat/api/backtests.py`), read (runs view, run page,
compare) and started from `frontend/app/hat/backtests/` (charts: Recharts).
A panel whose poll fails keeps its last good read on screen, dimmed, under a stale notice; when several
reads fail together the page shows one banner instead. The page header shows the backend's `as_of` in IST; the broker login status polls on the same cadence. The design
tokens live only in `frontend/app/globals.css` `@theme`, which resets Tailwind's palette, type scale
and weights, so an undefined colour class or a weight above 500 generates nothing.

Environment: `.env.example` lists every variable: `DB_*`; the Fyers app `CLIENT_ID`,
`SECRET_KEY`, `REDIRECT_URI`; `HAT_BROKER` and the Kite app `KITE_API_KEY`, `KITE_API_SECRET`,
`KITE_REDIRECT_URI`; `DB_STATE_NAME` (hat_state's name); `HAT_ENABLED`, `HAT_USER_ID`, `HAT_DATA_ROOT`, `HAT_DATA_REPO`, the MCP URLs and
Telegram secrets; `BACKEND_HOST`/`BACKEND_PORT` and logging for `backend/start_server.py`.
