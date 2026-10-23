# Architecture

Code is the source of truth; this is the map. Rules the code must keep: `invariants.md`. Decisions
in force: `decisions.md`. Operator procedures: `runbook.md`.

## Shape

One FastAPI process (`backend/app/main.py`) and one PostgreSQL database. All of HAT is the bounded
package `backend/app/hat/`. It imports only `app.base`, `app.database`, `app.paths` and
`app.utils.env_loader` from the rest of the backend (CONF `imports.*`). The runtime starts only when
`HAT_ENABLED` is true (default false). The frontend (`frontend/`) is a read-only dashboard; Telegram
is the control surface. Orders reach Fyers only from a HAT signal.

| Package | Holds |
| --- | --- |
| `data` | NSE MCP client, bhavcopy, reference data, ingest, quality gate, corporate actions, Parquet store (`store.py`, `bars_store.py`), features, the live feed (`feed.py`, `provider/`, `subscriptions.py`), 5-minute bars, gap scan, announcements |
| `engine` | Criteria library (`criteria/`), strategy config and seeds (`strategy/`), scan, trigger monitor, exits, stops, lifecycle, trades and shadow fills, approvals, portfolio (`portfolio/`: universe, regime, health, allocator, sleeve lifecycle), backtest day loop |
| `risk` | Risk policy and its versions, sizing, caps, exposure ladder, kill switches, modes, `evaluate_signal` |
| `execution` | `HatBroker` interface, Fyers adapter/broker/session/auth/tokens, router, order submit and tags, stop placement (`protect.py` over the pure planners in `stop_plans.py`), the swap, nightly verify and GTT-book check (`gtt_upkeep.py`), entry-remainder cancels (`entry_cancel.py`), trade management (`manage.py`), market price protection re-send (`mpp.py`), per-position lock and lost-answer settle (`position_desk.py`), GTT, reconciliation, mode gates |
| `telegram` | Bot API client, long-poll service, commands, approval cards, notifier, reports |
| `ops` | In-process scheduler, job runner, partitions, retention |
| `daily` | `HatRuntime` wiring, the daily job table (`daily_jobs`), nightly and intraday job bodies, production parts |
| `backtest` | Costs, fills, deflated Sharpe, run records, metrics |
| `models` | SQLAlchemy models on the shared `Base`; every table is `hat_*` |
| `api` | Read-only dashboard routes and the Fyers login routes behind the write guard |

Top-level helpers: `events.py` (event log), `money.py` (Decimal), `clock.py`, `ids.py` (uuid v7),
`settings.py`, `schema.py` (`init_db` upkeep, Postgres-only DDL, seeds), `admin.py` (owner CLI).

## Data flow

Nightly (the scheduler chains jobs with `after`; each job is keyed `(job, trade_date)`):

```text
ingest.nightly (NSE MCP from 18:00, bhavcopy fallback 22:00) -> raw bars (Parquet) + index/breadth
  -> quality.gate -> ca.adjust (corporate actions, adjusted view)
  -> features -> regime (3-day confirm) -> allocate (sleeve multipliers)
  -> scan (universe -> regime gate -> setup score) -> hat_watchlist_entry -> Telegram digest
```

Intraday (one `LiveFeed`, one Fyers data socket, owned by HAT):

```text
ticks -> 5-minute bars -> TriggerMonitor (pure) -> signal
  -> evaluate_signal (risk authority: sizing, caps, kill switches, invariants, price band)
  -> ExecutionRouter, mode of the bot: shadow (virtual fill) | confirm (Telegram card, Take/Skip,
     5-minute expiry, max-chase re-check) | auto (high-confidence only)
  -> reserve_order (row + tag committed) -> send_order -> HatBroker (Fyers)
  -> fill sync -> StopDesk.protect (intraday stop, swapped to a GTT around 15:25)
  -> TradeManager: exit stack (`exits.evaluate`), 15:25 MA exits, nightly GTT verify
```

Every state change and its event are written in one transaction (`events.record` into the
append-only `hat_event`); trade state replays from events (`journal.replay_state`).

## Key seams

- **Pure core shared by backtest and live:** `scan.scan_bot`, `exits.evaluate/apply`, the criteria
  and `decide` read no clock, database or file; `engine/backtest.py` calls the same code live runs.
- **One risk authority:** `risk/service.py:evaluate_signal` calls `decide`, the only caller of
  `size_position`. No override path exists.
- **One path to the broker:** `execution/submit.py` (`reserve_order`, `send_order`) is the only
  caller of `HatBroker`; `FyersHatBroker` is its only implementation. `FakeBroker`
  (`tests/hat/fakes/broker.py`) is the only fake.
- **One position desk per broker account** (`execution/position_desk.py:PositionDesk`, shared by
  `StopDesk` and `TradeManager`): one re-entrant write lock per position, held by every stop write
  and by a sale from its checks to its send; one settle rule for a lost answer (`settle_verdict`).
- **One session owner per broker account** (`SessionRegistry.acquire`), one token store
  (`fyers_tokens.token_store`), one live socket (`LiveFeed`, built only in `main.py`).
- **One clock, one mode:** code that needs "now" takes a `clock.Clock` (`SYSTEM_CLOCK` in
  production); `send_order`, `adopt_broker_order` and `ReconciliationService` stamp from it.
  `risk/modes.py:Mode` is the only spelling of shadow/confirm/auto (the schema CHECKs read it);
  `queued_mode_at` is the queued change in force, `execution/modes.py:capability_gated` the gate.
- **Gates:** `execution/modes.py:gate_refusal` decides confirm/auto vs shadow; `ExecutionGuard`
  holds the per-trade-date reconciliation gate; kill switches live in `risk/kill.py`, `controls.py`.
- **Configs are data:** strategy versions are immutable JSONB with a canonical hash; the risk policy
  is a versioned account-level record; bots hold no risk numbers.
- **No private imports:** no HAT module imports another's underscore name (CONF `no_private_imports`).
- **Scheduler:** in-process asyncio, jobs recorded in `hat_job_run`, restart-safe (reconcile before
  acting, an overdue job runs late the same day unless it has a window).

## Storage

- PostgreSQL (`hat_*` tables, `init_db` creates them; no Alembic; HAT refuses SQLite at runtime):
  reference (market, instrument (one per ISIN, `data/identity.py`), symbol history, bands,
  calendar, corporate actions, announcements), strategy/risk/bot (criterion types, strategy versions, risk policy versions,
  broker accounts, bots, kill switches, exposure), portfolio (universes, regime, allocations),
  runtime (watchlist, signal, approval, order, fill, position, trade result), ops (event, job run,
  quality, Telegram, snapshots, backtest runs). The event table is monthly-partitioned on Postgres.
- Parquet under `HAT_DATA_ROOT`, date-partitioned, read through DuckDB: `bars_daily_raw`
  (immutable), `index_daily`, `features_daily`, `breadth_daily`, `market_breadth`, 5-minute bars
  (`bars_5m`, raw basis; the minute-history loader's refusals in `bars_5m_rejects`).

## Daily cycle (IST, trading days; `daily/runtime.py:daily_jobs`)

| Time | Job |
| --- | --- |
| 00:30 / 01:00 | events.partitions, retention.purge |
| 07:00-07:45 | calendar.refresh, bands.nightly (Saturday 09:00 reference.weekly) |
| 08:30, 09:05 | Telegram login prompts (no entries until logged in) |
| 08:40-08:45 | ca.rescale open positions, gtt.check (GTT book; the fill sync re-reads it every 5 min), live.prepare |
| 09:15-09:20 | modes.apply (queued mode changes), gapscan |
| 09:20-15:20 | triggers, routing, stops on fill |
| 15:20-15:25 | exits.close (MA exits on live price); 15:25 gtt.swap |
| 15:30-15:45 | watchlist.expire, trade.end_session, Telegram rejections, daily report, veto digest |
| 18:00 on | the nightly chain above |
| 23:00 / 23:15 / 23:30 | gtt.verify, journal.check, snapshot.bots (drawdown kills at the close marks) |

Every job has a deadline after its time (`daily/runtime.py:JOB_DEADLINES`); a job that fails,
misses its deadline or runs past it raises one URGENT alert per job and trade date
(`job.failed`, `job.missed`, `job.running_long`). A calendar with no trading day more than a week
ahead alerts `calendar.short` daily.

Periodic loop beside the jobs: session watch, fill sync, approval expiry, reconciliation (5 min),
live exits of shadow positions, the drawdown kills on equity marked to market (every minute of
the session), announcements poll, the calendar check.

## Verification layout

TLA+ models in `verification/tla/` (ExecutionRouter, OrderReconciliation, StopPlacement,
KillSwitch, ApprovalToken, TradeLifecycle, JobScheduler, NightlyPipeline, DailyCycle,
RegimeConfirmation, SleeveLifecycle; each with `broken/` variants), Hypothesis properties in
`backend/tests/hat/properties/`, Postgres checks in `tests/hat/db/` (only what a real server
enforces: triggers, partial unique indexes, CHECK constraints, row and advisory locks), conformance checks in
`tests/hat/conformance/`. The `verify-*` skills in `.claude/skills/` say
how to run and update each area and record a digest of the files they cover.

CI (`.github/workflows/ci.yml`, every PR and push to main): `backend`, `backend-postgres`,
`verification-conformance`, `verification-properties` (four shards), `frontend`, `api-schema`.
`.github/workflows/tla.yml` runs the TLC models and their broken variants only when
`verification/**` or a Python file named in a model's `code:` comments changes, weekly, and on
demand.
