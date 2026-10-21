# 10: Operations

How HAT runs: process layout, scheduler and job idempotency, the daily cycle, failure handling,
deployment, secrets, backups, observability and the runbook. Requirement format and verification
methods: see README.md and 11-verification.md.

Sources: MA "Runtime", "Daily cycle", "Failure handling", "Deployment and security", "Risks";
SCH `hat_job_run`, `hat_quality_check`; DS "Nightly ingest", "Live data"; PLAN C3, C4, §4.3, §7,
Q2, Q7, Q9. Decisions: T7, T9, T13, T15, I14, OWN-20OCT.

## §1 Runtime layout

The docs describe four processes (scheduler, live service, broker layer, API + Telegram). PLAN C4 /
Q2: SmartTrader's live feed is a singleton and HAT must never open a second WebSocket, so in the MVP
HAT runs inside the backend process as lifespan-managed asyncio tasks, gated by `HAT_ENABLED`
(default `false`). The four-process split stays logical: separate modules, restart-safe jobs. OWN-20OCT:
there is no OpenAlgo process; the broker layer is the in-process Fyers adapter.

#### HAT-OPS-001: HAT is off unless HAT_ENABLED is true
- **Statement:** With `HAT_ENABLED` unset or false, the backend starts no HAT scheduler, live listener, broker session or Telegram poller.
- **Source:** PLAN C4, Q2; OWN-20OCT (in-repo module).
- **PR:** 2 (setting), 17 (lifespan wiring)
- **Verification:** PBT `ops.disabled_starts_nothing`
- **Acceptance:**
  - Property: for every environment in which `HAT_ENABLED` is not a true value, the lifespan creates no HAT task, makes no Telegram or broker call, and the only startup work is the event loop and the database schema (K6). Harm: HAT trades or polls on a developer machine or in tests that did not opt in.
- **[OWNER-PENDING]** single-process layout instead of the docs' four processes: default in-process tasks (PLAN Q2); revisit for P2.

#### HAT-OPS-002: HAT refuses to run on SQLite
- **Statement:** With `HAT_ENABLED` true, HAT refuses to start when the database dialect is not PostgreSQL.
- **Source:** PLAN §3 "Migrations" item 6 (DB-enforced governance is part of the design); SCH "Summary" (the database enforces governance).
- **PR:** 17
- **Verification:** PBT `ops.refuses_non_postgres`
- **Acceptance:**
  - Property: for every dialect other than `postgresql`, enabling HAT fails closed with a logged reason and starts no HAT task. Harm: the stop-only-tightens trigger, the append-only event log and version immutability silently disappear.

#### HAT-OPS-003: Restart safety: reconcile before acting
- **Statement:** After any (re)start, HAT reconciles positions and open orders with each broker before its first order and resumes jobs from `hat_job_run`.
- **Source:** MA "Runtime" (live service reconciles before its first order), "Failure handling" (machine down → on restart, reconcile before acting).
- **PR:** 17 (wiring), 13 (reconciliation)
- **Verification:** TLA `OrderReconciliation!ReconcileBeforeFirstOrder`
- **Acceptance:**
  - Property: in every behaviour that includes a restart, no order is sent to a broker account before a reconciliation of that account has completed successfully since the restart. Harm: the engine re-places orders that already exist or misses fills from while it was down.

#### HAT-OPS-004: Exits and stops never depend on the nightly pipeline
- **Statement:** Exit evaluation and protective stops run whether or not the nightly ingest, gate, features or scans succeeded.
- **Source:** MA "Runtime" ("Exits and protective stops never depend on the nightly pipeline"), "Failure handling".
- **PR:** 17
- **Verification:** TLA `NightlyPipeline!ExitsIndependentOfPipeline`
- **Acceptance:**
  - Property: in every behaviour where any nightly job fails or never runs, the live exit monitor, `exits.close` and `gtt.swap` still run on schedule for every open position. Harm: a data outage leaves open positions without exits or overnight stops.

## §2 Scheduler and job idempotency

In-process asyncio scheduler (T7); job runs recorded in `hat_job_run (id, job, trade_date,
started_at, finished_at, status, calls, rows, error)` with unique `(job, trade_date)`.

#### HAT-OPS-010: A job keyed (job, trade_date) runs at most once to success
- **Statement:** For each `(job, trade_date)` the job body succeeds at most once; a second run after success returns the stored result without re-executing.
- **Source:** MA "Runtime" (scheduler: every job idempotent, keyed by trade date); SCH `hat_job_run` (unique (job, trade_date)); T7.
- **PR:** 2
- **Verification:** TLA `JobScheduler!AtMostOnceSuccess`, DB `ops.job_run_unique`
- **Acceptance:**
  - Property: in every interleaving of concurrent or repeated `run_job(name, trade_date, fn)` calls, including crashes mid-run, `fn` completes successfully at most once per `(name, trade_date)` and every later call returns the stored `succeeded` result. Harm: a re-run double-ingests data, double-sends digests or double-places GTT orders.
  - Property: the database rejects a second `hat_job_run` row with the same `(job, trade_date)`. Harm: app-level races bypass idempotency.

#### HAT-OPS-011: A failed job can be retried
- **Statement:** A job whose last run failed (or was interrupted) can run again for the same trade date, and its status, calls, rows and error are recorded.
- **Source:** SCH `hat_job_run` (status, calls, rows, error); MA "Runtime".
- **PR:** 2
- **Verification:** TLA `JobScheduler!FailedJobRetryable`, PBT `ops.job_run_recording`
- **Acceptance:**
  - Property: from every state where the latest run of `(job, trade_date)` is `failed` or was interrupted, a retry is allowed and eventually either succeeds or records a new error; a crashed run never stays `running` forever after restart. Harm: one transient failure blocks that day's job permanently.
  - Property: for every run, `finished_at ≥ started_at` and a failed run has a non-empty `error` with no raw broker or DB error text. Harm: failures are untraceable or leak internals.

#### HAT-OPS-012: Scheduler skips non-trading days
- **Statement:** Trade-date jobs run only on trading days of `hat_trading_calendar`.
- **Source:** DS "Reference data" (Holidays: scheduler skips non-trading days); SCH `hat_trading_calendar` ("Scheduler and bar alignment read this").
- **PR:** 17
- **Verification:** PBT `ops.calendar_skip`
- **Acceptance:**
  - Property: for every calendar and every date d with `is_trading = false`, no trade-date job (ingest, scans, login prompts, live preparation, exits) starts for d. Harm: bots act on a holiday, alert on missing data that never existed, or send pointless login links.

#### HAT-OPS-013: Scheduler does not run when HAT is disabled
- **Statement:** The scheduler loop is created only when `HAT_ENABLED` is true.
- **Source:** PLAN PR 2 scope (`scheduler.py`: not started unless `HAT_ENABLED`); PLAN C4.
- **PR:** 2
- **Verification:** PBT `ops.disabled_starts_nothing`
- **Acceptance:**
  - Property: for every settings object with `HAT_ENABLED` false, importing and starting the backend creates no scheduler task. Harm: jobs run in environments that never opted in.

## §3 Daily cycle (IST, NSE)

MA "Daily cycle":

1. **08:30**: Telegram sends a login link per connected broker; no entries until logged in.
2. **08:45**: load watchlist; attach overnight NSE announcements to EP candidates.
3. **09:00–09:15**: pre-open: record gap % per watchlist and EP-scan symbol.
4. **09:20**: first 5-min bar closes; opening-range highs fixed.
5. **09:20–15:20**: triggers fire; risk manager decides; orders routed per execution mode; stop placed on fill.
6. **15:20–15:25**: moving-average exits evaluated on live price; orders sent.
7. **~18:00 onward**: poll NSE MCP until the bulk-quote date equals today; then quality gate → corporate-action adjustment → features → scans → Telegram watchlist.
8. **22:00 cutoff**: still no data → fall back to NSE bhavcopy download; still nothing → skip scans, alert, bots stay manage-only.
9. **Nightly**: confirm a GTT stop exists for every open position.

Jobs (PLAN §4.3):

| Job | Time (IST) | PR (job logic) | Wired in |
| --- | --- | --- | --- |
| `reference.weekly` (equity list, industry) | weekly | 4 | 17 |
| `calendar.refresh` | yearly + on change | 4 | 17 |
| `bands.nightly` | nightly | 4 | 17 |
| `ingest.nightly` (probe 18:00, poll 10 min, 22:00 bhavcopy fallback) | 18:00–22:00 | 7 | 17 |
| `quality.gate` → `ca.adjust` → `features` → `regime` → `allocate` → `scan` → `watchlist.digest` | after ingest | 7, 6, 9, 11, 16 | 17 |
| `login.prompt` | 08:30, 09:05 | 16 | 17 |
| `live.prepare` (subscribe watchlist + positions) | 08:45 | 14 | 17 |
| `preopen.gaps` / `gapscan` | 09:00–09:08 / 09:16 | 14 | 17 |
| `announcements.poll` | every 2 min 07:30–15:30, else 15 min | 14 | 17 |
| `exits.close` (MA / partial-after-days / time stops on live price) | 15:20–15:25 | 15 | 17 |
| `gtt.swap` (intraday stop → GTT) | ~15:25 | 15 | 17 |
| `digest.rejections` 15:35, `pnl.daily` 15:40 | — | 16 | 17 |
| `gtt.verify` (GTT exists for every open position) | nightly | 15 | 17 |
| `snapshot.bots` (`hat_daily_bot_snapshot`, drawdowns → kill/health) | nightly | 10 | 17 |
| `events.partitions` | nightly | 3 | 17 |
| `history.build` (one-off CLI) | manual | 5 | — |

#### HAT-OPS-020: Every daily-cycle job is registered at its time
- **Statement:** The scheduler registers exactly the jobs of the table above at the stated IST times.
- **Source:** MA "Daily cycle"; PLAN §4.3; T7.
- **PR:** 17
- **Verification:** CONF `ops.job_registry`
- **Acceptance:**
  - Property: the set of registered `(job, schedule)` pairs equals the job table of this section, with times in `Asia/Kolkata`. Harm: a missing or mistimed job (for example `gtt.swap`) leaves positions unprotected overnight.

#### HAT-OPS-021: Nightly pipeline order
- **Statement:** After a successful ingest, the nightly jobs run in the order `quality.gate` → `ca.adjust` → `features` → `regime` → `allocate` → `scan` → `watchlist.digest`, each only after its predecessor succeeded for the same trade date.
- **Source:** MA "Daily cycle" step 7; DS "Nightly ingest" steps 5–9; PLAN §4.3.
- **PR:** 17
- **Verification:** TLA `NightlyPipeline!NightlyPipelineOrder`
- **Acceptance:**
  - Property: in every behaviour, a job of the pipeline starts for trade date d only after every earlier job of the pipeline has status `succeeded` for d. Harm: scans run on unadjusted prices or stale features and produce wrong watchlists.
  - Property: if `quality.gate` fails for d, no `scan` runs for d and every bot is manage-only for new entries. Harm: the bot trades on data the gate rejected.

#### HAT-OPS-022: Ingest polling and the 22:00 fallback
- **Statement:** From 18:00 IST the ingest probes every 10 minutes until the bulk-quote date equals the trade date; at 22:00 it falls back to the bhavcopy download; if that also yields nothing, scans are skipped, an alert is sent and bots stay manage-only.
- **Source:** MA "Daily cycle" steps 7–8; DS "Nightly ingest" step 1; I1.
- **PR:** 7 (logic), 17 (wiring)
- **Verification:** TLA `NightlyPipeline!FallbackAt2200`
- **Acceptance:**
  - Property: in every behaviour, either MCP data for d is accepted before 22:00 IST, or the bhavcopy fallback is attempted at 22:00; and if both yield nothing, eventually a health alert is sent, no scan runs for d, and every bot is manage-only for new entries. Harm: the system waits forever or scans on yesterday's data.
  - Property: consecutive probes are at least 10 minutes apart and none starts before 18:00 IST. Harm: hammering the MCP server risks throttling or a terms breach.

#### HAT-OPS-023: Intraday sequence
- **Statement:** On a trading day, login prompts (08:30), live preparation (08:45), pre-open gap capture (09:00–09:08), the gap scan (09:16), the first trigger window (from 09:20), MA exits (15:20–15:25) and the GTT swap (~15:25) happen in that order.
- **Source:** MA "Daily cycle" steps 1–6; DS "Live data" window table.
- **PR:** 14, 15 (logic), 17 (wiring)
- **Verification:** TLA `JobScheduler!IntradayOrder`
- **Acceptance:**
  - Property: in every behaviour of a trading day, no trigger is acted on before the 09:20 bar is final, and no entry order is placed for a broker account without a live `hat_broker_session` for that trade date. Harm: entries fire on an unformed opening range or without a valid broker session.
  - Property: `gtt.swap` for a position runs only after that day's MA exits for it have been evaluated. Harm: a GTT is placed for a position that is being exited, leaving an orphan stop that could open a short.

#### HAT-OPS-024: Nightly GTT verification
- **Statement:** Each night, `gtt.verify` confirms that every open non-virtual position has a GTT stop at the broker and alerts on any that does not.
- **Source:** MA "Daily cycle" step 9; BL "Stop placement at the broker" (B17).
- **PR:** 15 (logic), 17 (wiring)
- **Verification:** TLA `StopPlacement!NightlyVerifyCovers`
- **Acceptance:**
  - Property: after every `gtt.verify` run, every open non-virtual position either has a matching active GTT at the broker or a health alert naming it has been sent. Harm: a position sits overnight without a protective stop and nobody knows.

## §4 Failure handling

MA "Failure handling" (verbatim):

- **Data missing or fails the quality gate** → no new scans; exits and stops continue
- **Broker session drops** → retry with backoff; alert after 2 minutes; stops already rest at the broker
- **Machine down** → broker-side GTT/stop orders protect positions; on restart, reconcile before acting
- **Order rejected** → logged, alerted, retried at most once
- **Duplicate trigger** → signal ID is the idempotency key; second order refused
- **Close vs live price** → NSE's official close is a weighted average of the last 30 minutes, so a 15:25 exit can disagree with the next morning's close. Logged as a known gap, measured in shadow mode

#### HAT-OPS-030: Data failure blocks scans, never exits
- **Statement:** Missing data or a failed quality gate stops new scans and new entries, while exits and stops continue.
- **Source:** MA "Failure handling"; BL "Kill switches" (data health check fails → affected bots manage-only; re-arm automatic when healthy); I14.
- **PR:** 7, 10
- **Verification:** TLA `KillSwitch!DataHealthManageOnly`, TLA `NightlyPipeline!ExitsIndependentOfPipeline`
- **Acceptance:**
  - Property: in every state where the data-health check for the current trade date is failing, no entry order is placed and every exit rule still fires; once the check passes again the health manage-only state clears without manual action. Harm: trading on bad data, or positions abandoned because data failed.

#### HAT-OPS-031: Broker session drop: retry with backoff, alert after 2 minutes
- **Statement:** When a broker session drops, HAT retries with backoff and sends a health alert if the session is still down after 2 minutes; affected bots are manage-only until it recovers.
- **Source:** MA "Failure handling"; BL "Kill switches" (broker health); SCH event `broker.session_lost`.
- **PR:** 13 (session), 16 (alert)
- **Verification:** TLA `KillSwitch!DataHealthManageOnly`, PBT `ops.session_backoff_alert`
- **Acceptance:**
  - Property: for every outage of duration t, a `broker.session_lost` alert is sent iff t ≥ 2 minutes, exactly once per outage, and retry intervals never decrease within an outage. Harm: either alert spam or a silent outage during market hours.
  - Property: while the session is down, no entry order is attempted for bots on that broker account. Harm: orders fail or are retried blindly.
- **[OWNER-PENDING]** backoff schedule: the docs give none; default exponential backoff starting at 1 s, capped at 30 s; owner to confirm.

#### HAT-OPS-032: Order rejected: retried at most once
- **Statement:** A broker-rejected order is logged (`order.rejected`), alerted and retried at most once.
- **Source:** MA "Failure handling"; SCH event `order.rejected`.
- **PR:** 15
- **Verification:** TLA `OrderReconciliation!RetryAtMostOnce`
- **Acceptance:**
  - Property: in every behaviour, for each signal and purpose there are at most two broker submissions of an order after rejections (the original and one retry), and every rejection produces one `order.rejected` event and one health alert. Harm: a retry loop floods the broker or places duplicate positions.

#### HAT-OPS-033: Duplicate trigger is refused
- **Statement:** The signal ID is the idempotency key; a second trigger for the same bot, playbook, instrument and trade date places no second order.
- **Source:** MA "Failure handling"; SCH `hat_signal` unique (bot_id, playbook, instrument_id, trade_date).
- **PR:** 3 (constraint), 15 (router)
- **Verification:** TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`
- **Acceptance:**
  - Property: in every interleaving of trigger events, at most one entry order exists per `(bot_id, playbook, instrument_id, trade_date)`. Harm: the same setup is bought twice and risk doubles.

#### HAT-OPS-034: 15:25 exit vs official close is logged
- **Statement:** For every MA exit evaluated at 15:25 on live price, both the live price used and the next official close are recorded so shadow mode can measure the gap.
- **Source:** MA "Failure handling" (close vs live price), "Risks" (15:25 exit price ≠ official close); BL "Defaults that are assumptions" (exit at 15:25 vs official close); PLAN Q10.
- **PR:** 15
- **Verification:** PBT `ops.close_gap_recorded`
- **Acceptance:**
  - Property: for every `close_below_sma` evaluation at 15:25, the event payload records the live price used and, once the nightly ingest has the official close, the gap between them is recorded against the same position. Harm: the backtest/live discrepancy cannot be measured, so a systematic exit bias goes unnoticed.
- **[A]** exit at 15:25 on live price: default 15:25 live price; validated by comparing both prices in shadow mode (BL "Defaults that are assumptions").

## §5 Deployment

MA "Deployment and security" with PLAN C3 (Docker Compose is additive; Windows dev keeps working)
and OWN-20OCT (no OpenAlgo container).

| Item | Choice |
| --- | --- |
| Host | Always-on machine, Linux preferred (dev stays Windows-first) |
| IP | ISP static IP, registered with each broker app |
| Power and network | UPS + second internet link (4G/5G failover for alerts and manual login only) |
| Packaging | Docker Compose: Postgres + backend (HAT runs in the backend process); frontend optional |
| Remote access | Tailscale (or WireGuard); no inbound ports |
| Clock | NTP sync |
| Updates | Manual deploy outside market hours only; no restarts between 08:30 and 15:35 |

#### HAT-OPS-040: Compose deployment
- **Statement:** `deploy/hat/docker-compose.yml` brings up Postgres and the backend with HAT enabled, with no OpenAlgo service and no published inbound port other than over the private VPN.
- **Source:** MA "Deployment and security" (Packaging, Remote access); PLAN C3, C5; OWN-20OCT; T9.
- **PR:** 17
- **Verification:** CONF `ops.compose_services`
- **Acceptance:**
  - Property: the Compose file defines exactly the services `postgres` and `backend` (plus an optional `frontend`), no `openalgo` service, and every published port is bound to a loopback or VPN address. Harm: an open port exposes the trading box, or a stray service holds broker credentials.

#### HAT-OPS-041: Host prerequisites
- **Statement:** The HAT host runs Linux with NTP sync, a registered ISP static IP, a UPS, a second internet link for alerts, and Tailscale (or WireGuard) for remote access.
- **Source:** MA "Deployment and security"; MA "Risks" (home static IP outage); PLAN §7 (static IP since 1 Apr 2026).
- **PR:** 17
- **Verification:** OPS `ops.host_checklist`
- **Acceptance:**
  - Property: before confirm or auto mode is enabled on any bot, the runbook checklist records NTP offset under 1 s, the static IP registered with the Fyers app, UPS and failover link tested, and dashboard reachable only over the VPN. Harm: bars misalign at 09:15, orders are rejected from an unregistered IP, or the box goes dark on a power cut.
- **[OWNER-PENDING]** NTP tolerance: the docs say "NTP sync" without a bound; default maximum offset 1 s; owner to confirm.

#### HAT-OPS-042: No restarts or deploys during market hours
- **Statement:** Deploys and planned restarts happen only outside 08:30–15:35 IST on trading days.
- **Source:** MA "Deployment and security" (Updates: manual deploy outside market hours only; no restarts between 08:30 and 15:35).
- **PR:** 17
- **Verification:** OPS `ops.deploy_window`, PBT `ops.deploy_window_guard`
- **Acceptance:**
  - Property: for every instant on a trading day between 08:30 and 15:35 IST, the deploy script refuses to run unless forced, and a forced run is logged. Harm: a restart mid-session misses triggers, stop moves or the GTT swap.

#### HAT-OPS-043: 4G failover places no orders
- **Statement:** When the host is on its failover link (a non-registered IP), new entries wait until the static line returns; broker-side stops cover open positions.
- **Source:** MA "Deployment and security" (consequence of the 4G failover).
- **PR:** 17
- **Verification:** OPS `ops.failover_drill`
- **Acceptance:**
  - Property: during a failover drill, broker order calls are rejected or not attempted, a health alert is sent, and all open positions keep their broker-side stops. Harm: entries fail mid-placement and leave half-states without stops.

## §6 Secrets

MA T13 as overridden by OWN-20OCT and PLAN C5: no OpenAlgo, so there is no OpenAlgo key and no
second broker-secret store. Fyers credentials stay where SmartTrader keeps them today (`.env`
Fyers variables and `fyers/config/access_token.json`). New HAT secrets (PLAN §7) live in `.env`.

| Variable | Purpose | Needed by |
| --- | --- | --- |
| `HAT_TELEGRAM_TOKEN` | Telegram bot token | PR 16 |
| `HAT_TELEGRAM_CHAT_ID` | The single allowlisted chat | PR 16 |
| `HAT_APPROVAL_SECRET` | HMAC secret for approval tokens (32+ random bytes) | PR 16 |
| `HAT_MCP_BHAVCOPY_URL` | NSE `nse-bhavcopy` MCP endpoint | PR 4, 7 |
| `HAT_MCP_CMMARKET_URL` | NSE `cm-market` MCP endpoint | PR 4, 14 |
| `DB_*` | Postgres credentials (existing variables) | All |

#### HAT-OPS-050: Secrets only in .env outside the repo
- **Statement:** HAT reads its secrets only from environment variables loaded from a `.env` file outside the repo, readable by the service user only; no secret is committed or written to a log.
- **Source:** MA "Deployment and security" (Secrets), "Decisions register" T13 (as overridden by OWN-20OCT: no OpenAlgo); PLAN C5, §7.
- **PR:** 2, 17
- **Verification:** CONF `ops.no_secrets_in_repo`, OPS `ops.env_permissions`
- **Acceptance:**
  - Property: no tracked file contains a value for any variable of the table above, and `.env.example` lists every one of them with an empty value. Harm: a pushed secret gives control of the bot or the broker account.
  - Property: on the host, `.env` is mode 0600 (or stricter), owned by the service user, on an encrypted disk. Harm: another local user reads broker and bot credentials.

#### HAT-OPS-051: Fyers credentials are not duplicated
- **Statement:** One Fyers login and one token store: the Fyers adapter reads Fyers credentials and tokens through HAT's own `TokenStore` and login service (`app/hat/execution/fyers_tokens.py`, `fyers_auth.py`, `fyers_session.py`), the only HAT modules that touch them; the legacy token store, client and login route are subclasses or re-exports of those (until K4 and K5 delete them), and HAT adds no second copy. [Reworded in K2; it said HAT reads SmartTrader's existing modules.]
- **Source:** OWN-20OCT (direct Fyers adapter); PLAN C5; cleanup plan K2.
- **PR:** 13
- **Verification:** CONF `ops.single_fyers_secret_store`
- **Acceptance:**
  - Property: across `app/hat/**`, every Fyers credential or token access goes through HAT's token-store, login and session modules (one login route, `/api/hat/auth/fyers`), and no HAT table has a column holding a broker secret. Harm: two credential stores drift and one leaks.

## §7 Backups

#### HAT-OPS-060: Nightly pg_dump and Parquet sync
- **Statement:** Every night a `pg_dump` of the HAT database and a sync of the HAT Parquet root (`HAT_DATA_ROOT`) are copied to an external disk and to one cloud bucket.
- **Source:** MA "Deployment and security" (Backups: nightly `pg_dump` + Parquet sync to external disk and one cloud bucket; the event log is the system of record); SCH "Retention" (raw zips kept forever); PLAN Q9.
- **PR:** 17
- **Verification:** OPS `ops.backup_restore_drill`, PBT `ops.backup_manifest`
- **Acceptance:**
  - Property: for every backup run, the manifest lists the dump file and every Parquet and raw-zip file under `HAT_DATA_ROOT` with matching size and checksum at both targets, and a missing or mismatched file fails the run with an alert. Harm: losing the event log or raw files makes trades unexplainable and history unrebuildable.
  - Property: a restore drill from the latest backup reproduces the event-log row count and the latest trade date. Harm: backups exist but cannot be restored.
- **[OWNER-PENDING]** backup target and credentials (external disk path, cloud bucket keys): PLAN §7; owner to provide.

## §8 Observability and alerts

#### HAT-OPS-070: Structured logs
- **Statement:** HAT logs are structured (one JSON object per record) through the repo logger, carrying job, trade date, bot and signal IDs where they apply.
- **Source:** MA "Decisions register" T15 (structured logs + Telegram alerts); PLAN §4.1 T15 (`utils/logger.py`).
- **PR:** 2, 17
- **Verification:** PBT `ops.log_records_structured`
- **Acceptance:**
  - Property: for every log record emitted under the `app.hat` logger, the record serialises to a JSON object with `ts`, `level`, `msg` and, when present in context, `job`, `trade_date`, `bot_id`, `signal_id`. Harm: incidents cannot be traced across jobs and trades.

#### HAT-OPS-071: Health alerts go to Telegram
- **Statement:** Data gate failures, feed staleness, broker session loss beyond 2 minutes and order rejections raise a Telegram health alert.
- **Source:** DS "Telegram / Outbound messages" (Health alert); MA "Failure handling"; T15.
- **PR:** 16, 17
- **Verification:** PBT `telegram.report_content`
- **Acceptance:**
  - Property: every `quality.failed`, `feed.stale`, `broker.session_lost` (after 2 minutes) and `order.rejected` event results in exactly one health alert through the `Notifier`. Harm: failures that block trading or protection go unnoticed.

#### HAT-OPS-072: Runtime status is observable
- **Statement:** `/status` and `GET /api/hat/status` report broker sessions, feed health, regime per bot and kill-switch state from Postgres.
- **Source:** DS "Telegram / Commands" (`/status`); MA "Runtime" (API + Telegram stateless; reads Postgres); T3.
- **PR:** 16, 17
- **Verification:** PBT `ops.status_matches_db`
- **Acceptance:**
  - Property: for every DB state, the status payload's broker-session, kill-switch, regime and feed fields equal the latest corresponding rows. Harm: the user believes the bot is protected or halted when it is not.

## §9 Runbook

Skeleton. PR 17 fills in the procedures; each `OPS` check below is a checklist item with a date
and initials recorded in this section when performed.

| OPS check | When | What |
| --- | --- | --- |
| `ops.host_checklist` | Before enabling confirm or auto; after any host change | NTP offset, static IP registered with Fyers, UPS, failover link, VPN-only dashboard |
| `ops.env_permissions` | After any `.env` change | Mode 0600, service-user owner, encrypted disk |
| `ops.deploy_window` | Every deploy | Outside 08:30–15:35 IST on trading days; reconcile on start |
| `ops.failover_drill` | Quarterly | Switch to 4G; confirm no orders, alert sent, stops intact |
| `ops.backup_restore_drill` | Monthly | Restore latest backup to a scratch DB; compare event count and latest trade date |

Procedures to write in PR 17: morning login, missed login, data gate failure, 22:00 fallback
failure, broker session loss, feed stale, kill switch and re-arm, flatten, reconciliation mismatch,
GTT missing, restore from backup.

#### HAT-OPS-090: Runbook covers every OPS check and failure
- **Statement:** The runbook has one procedure for each OPS check and each MA failure case listed in §4.
- **Source:** MA "Failure handling", "Deployment and security"; PLAN PR 17 scope (runbook in this section).
- **PR:** 17
- **Verification:** CONF `ops.runbook_complete`
- **Acceptance:**
  - Property: every `OPS` verification name used anywhere in `docs/specs/hat/` and every bullet of §4 has a matching procedure heading in this section. Harm: at 09:20 on a bad day the operator has no tested procedure to follow.
