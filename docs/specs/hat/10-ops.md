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
- **Verification:** TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.resumed_entry_waits_for_feed`, PBT `router.one_login_per_broker`, PBT `ops.stop_names_abandoned_jobs`, PBT `ops.stop_after_failed_start`
- **Acceptance:**
  - Property: in every behaviour that includes a restart, no order is sent to a broker account before a reconciliation of that account has completed successfully since the restart. Harm: the engine re-places orders that already exist or misses fills from while it was down.
  - Property: an entry left Triggered by the previous process is placed at most once after the restart, only after the feed has a fresh tick and the account is reconciled, and never on a later trade date than its own. Harm: an entry goes out on a stale price, twice, or a day late.
  - Property: a stop, including one after a start that failed part way, stops the feed once, leaves no tick listener and names every job body still running after its grace. Harm: a half-started runtime keeps the provider socket, or a cut-off close leaves no trace.

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
- **Verification:** TLA `JobScheduler!FailedJobRetryable`, PBT `ops.job_run_recording`, PBT `ops.retry_after_failed_run`, PBT `ops.chain_carries_past_midnight`
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

Jobs (PLAN §4.3), as `HatRuntime` registers them (`app/hat/daily/runtime.py`; PR 17). "Trading"
jobs run only for a trading day of `hat_trading_calendar` (HAT-OPS-012), "every" jobs for every
calendar day; "After" jobs start only once those jobs succeeded for the same trade date; "Until"
is the end of a window the job is never started at or after. `<account>` jobs exist once per
active broker account (their name carries the account id). `CONF ops.job_registry` reads this
table.

| Job | Days | Time (IST) | Until | After | PR (job logic) |
| --- | --- | --- | --- | --- | --- |
| `events.partitions` | every | 00:30 | | | 3 |
| `retention.purge` | every | 01:00 | | | 16 |
| `calendar.refresh` | every | 07:00 | | | 4 |
| `bands.nightly` | trading | 07:45 | | | 4 |
| `reference.weekly` | every, Saturday | 09:00 | | | 4 |
| `telegram.login_prompt` | trading | 08:30 | 09:05 | | 16 |
| `ca.rescale` (open positions across a split or bonus, HAT-EXEC-141) | trading | 08:40 | | | 21 |
| `live.prepare` | trading | 08:45 | | | 14 |
| `telegram.login_reprompt` | trading | 09:05 | 15:30 | | 16 |
| `modes.apply` | trading | 09:15 | | | 15b |
| `gapscan` | trading | 09:16 | 09:20 | `live.prepare` | 14 |
| `exits.close:<account>` | trading | 15:20 | 15:25 | | 15c |
| `exits.close_shadow` | trading | 15:20 | 15:25 | | 15c |
| `gtt.swap:<account>` | trading | 15:25 | | | 15c |
| `watchlist.expire` (HAT-EXEC-041: an untriggered row ends Expired) | trading | 15:30 | | | 11 |
| `trade.end_session:<account>` | trading | 15:31 | | | 15c |
| `telegram.rejections` | trading | 15:35 | | | 16 |
| `telegram.daily_report` | trading | 15:40 | | | 16 |
| `telegram.veto_digest` | trading | 15:45 | | | 16 |
| `ingest.nightly` (probe 18:00, poll 10 min, 22:00 bhavcopy fallback) | trading | 18:00 | | | 7 |
| `exits.close_gaps` | trading | 18:00 | | `ingest.nightly` | 15c |
| `quality.gate` | trading | 18:00 | | `ingest.nightly` | 7 |
| `ca.adjust` (and the next session's actions of held and watchlisted instruments, HAT-EXEC-140) | trading | 18:00 | | `quality.gate` | 6, 21 |
| `features` | trading | 18:00 | | `ca.adjust` | 6 |
| `regime` | trading | 18:00 | | `features` | 11 |
| `allocate` | trading | 18:00 | | `regime` | 11 |
| `snapshot.bots` (`hat_daily_bot_snapshot`, drawdowns → kill/health) | trading | 18:00 | | `allocate` | 10 |
| `scan` | trading | 18:00 | | `allocate` | 9 |
| `watchlist.digest` | trading | 18:00 | | `scan` | 16 |
| `gtt.verify:<account>` | trading | 23:00 | | | 15c |

Periodic steps of the runtime (not `hat_job_run` jobs; each step logs and survives its own
failure): the broker-session check and approval expiry every minute, the fill sync every minute
and every 5 seconds while a real entry of the day works or a stop's answer is not in yet
(HAT-EXEC-060), reconciliation every 5 minutes, the calendar read every 10 minutes, the live exit rules of the shadow positions
every 5 seconds while the market is open, and `announcements.poll` at its own interval (every 2
minutes 07:30–15:30, else 15). The pre-open gap capture (09:00–09:08) is done by the feed listener
(`LiveData`), not a job. `history.build` is a one-off CLI (PR 5).

Design (PR 17), where the wiring chose a default the requirements leave open:

- Each job body gets the trade date its run is kept under (`ScheduledJob.fn(day)`).
- Job bodies run on the scheduler's own threads, one per registered job, so catch-up runs after a
  restart (any number, however slow) never hold a windowed job such as `exits.close` past its
  window (PBT `ops.window_not_starved`). An overdue job without a window still runs late the
  same day: each such job (`live.prepare`, `modes.apply`, the housekeeping) is still useful then,
  and the morning jobs that are not (`telegram.login_prompt`, `gapscan`) have windows.
- A `gtt.verify` run that fails at 23:xx is retried every 10 minutes until midnight IST only; the
  next night's run covers every open position again.
- `snapshot.bots` runs nightly after `allocate` (it needs the day's allocation capital), and
  `exits.close_gaps` after `ingest.nightly` (it needs the official closes).
- The nightly chain belongs to the trade date it started on. A chained job without a window (one
  with `after`, such as `quality.gate`, `scan` or `allocate`) whose date has passed still starts
  for that date until 08:30 IST the next day, before `live.prepare` reads the night's watchlist
  (PBT `ops.chain_carries_past_midnight`). After that a failed job of a past date is not re-run
  by the scheduler [OWNER-PENDING: a manual re-run command].
- A failed run is retried `retry_after` (10 minutes) after it ended, not after it started, so a
  body slower than that is not started again at once (PBT `ops.retry_after_failed_run`).
- On stop the scheduler waits up to 25 s (inside the compose `stop_grace_period` of 30 s) for the
  running bodies, then logs the name of each one still running; the next start reaps its run as
  interrupted (PBT `ops.stop_names_abandoned_jobs`). A start that failed part way still stops
  the feed and leaves no tick listener (PBT `ops.stop_after_failed_start`).
- A restart reconciles every account and reads its fills before anything is placed. An entry
  left Triggered by the previous process is placed only once the feed has a fresh tick and the
  account is reconciled, and only on its own trade date (07, `SIGNAL_EXPIRED`). A restart after
  08:45 on a trading day re-arms the day: the bar builder gets back the day's opening ranges and
  volumes from the stored 5-minute bars (opening ranges only for symbols watched from the open,
  and no day low for the rest of the day: the restart is a feed gap, HAT-DATA-114), and the gap
  scan's EP candidates come back from its `gapscan.completed` record (the acceptance run
  `test_ops__simulated_day` restarts at 10:30, after its entries).
- Each broker login serves one active account: a second active account of the same broker
  refuses the start with `HatConfigError` (PBT `router.one_login_per_broker`), so equity is
  never counted twice from one set of funds.
- The quality state an entry reads for day d is the status of the previous trading day's
  `quality.gate` run; when that run did not finish or does not exist the state is unknown and
  entries fail closed (reason `missing_input`).
- An entry on day d reads the allocation `allocate` made on the previous trading day; when that
  night's allocation is missing (for example the broker funds could not be read) entries fail
  closed rather than trade on an older weight or an unmeasured drawdown (`snapshot.bots` runs
  after `allocate` and measures the drawdown kills on that allocation).
- `allocate` takes total equity from the broker funds read when it runs (the sum over active
  accounts; unavailable funds fail the job with `equity_unavailable`). The 05 default reads them
  at session start, but the allocation for the next session is made the evening before
  [OWNER-PENDING HAT-PORT-052].
- The live exit rules of shadow positions read the latest traded price every 5 seconds instead of
  acting on each tick; the 15:25 `close_below_sma` evaluation is the `exits.close_shadow` window.
- A restart after 08:45 on a trading day arms the day again from the watchlist (`live.prepare`'s
  body, with every key that already has a signal marked fired). The EP candidates of the 09:16 gap
  scan are not rebuilt: their pre-open open prices are process memory, so after a restart past
  09:16 no EP entry triggers that day (fail closed); open positions, exits and stops are
  unaffected.
- The runtime re-reads the trading calendar every 10 minutes and hands it to every broker
  desk, so a calendar loaded after start or a new year's rows reach the stop desks and exits.
- The daily inputs of the live engine (ATR and swing lengths) are shared across bots and fail
  closed (no candidate) when the active bots disagree.
- The nightly scan does not read price bands; the band is checked at entry (`risk.service.price_band_ok`, HAT-DATA-025).
- `ca.rescale` (PR 21) fails while some position could not be rescaled, so it is retried every 5 minutes (three tries before the 09:00 pre-open); that position stays manage-only and is alerted `corporate_action.pending` on each try.

Acceptance run (PR 17, PLAN §4.3): `backend/tests/hat/db/test_db_daily_cycle.py::test_ops__simulated_day`
(Postgres-marked, CI job `backend-postgres`) runs a whole day through `HatRuntime` with HAT enabled,
fixture data, `FakeBroker` and a recording Telegram transport, in shadow mode: every trading-day
job of the table above succeeds once and in order; a breakout and an EP gap become shadow entries
and a stop crossed in the session is exited by the live exits within the minute; an untriggered
watchlist row ends Expired; the nightly chain runs from the MCP, and the next night from the 22:00
bhavcopy fallback; a holiday runs only the every-day jobs; no order reaches the broker; `stop`
leaves no task. It is an end-to-end run, not a `DB` check (11 §1 keeps `DB` for invariants the
database enforces); its breaks are recorded in `verify-scheduler`.

#### HAT-OPS-020: Every daily-cycle job is registered at its time
- **Statement:** The scheduler registers exactly the jobs of the table above at the stated IST times.
- **Source:** MA "Daily cycle"; PLAN §4.3; T7.
- **PR:** 17
- **Verification:** CONF `ops.job_registry`
- **Acceptance:**
  - Property: the set of registered `(job, days, time, until, after)` rows equals the job table of this section, with times in `Asia/Kolkata`. Harm: a missing or mistimed job (for example `gtt.swap`) leaves positions unprotected overnight.

#### HAT-OPS-021: Nightly pipeline order
- **Statement:** After a successful ingest, the nightly jobs run in the order `quality.gate` → `ca.adjust` → `features` → `regime` → `allocate` → `scan` → `watchlist.digest`, each only after its predecessor succeeded for the same trade date.
- **Source:** MA "Daily cycle" step 7; DS "Nightly ingest" steps 5–9; PLAN §4.3.
- **PR:** 17
- **Verification:** TLA `NightlyPipeline!NightlyPipelineOrder`, PBT `ops.entry_inputs_fail_closed`
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
- **Verification:** TLA `DailyCycle!IntradayOrder`, PBT `router.entry_needs_live_session`, CONF `ops.route_matches_protocol`
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
- **Implementation (PR 15c):** `ops/trade_jobs.py` builds the `exits.close`, `gtt.swap` and `gtt.verify` jobs of one broker account (bodies `TradeManager.close_exits`, `swap_stops`, `verify_stops`; an incomplete run fails with a fixed code and is retried; a position whose close-phase evaluation raises leaves the others evaluated and fails the run). Each job's name carries its broker account (`exits.close:<account id>`), since a run is kept once per (name, trade date). PR 17 registers them. 07 §6 describes what each does.

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
- **Statement:** When a broker session drops, HAT retries with backoff and sends a health alert if the session is still down after 2 minutes; affected bots are manage-only until it recovers (`broker_health`, HAT-RISK-063). An outage is counted from 08:30 IST, when the login prompt goes out: a session that is not live between the midnight token expiry and 08:30 is not an outage.
- **Source:** MA "Failure handling"; BL "Kill switches" (broker health); SCH event `broker.session_lost`; design review 2026-10-22 item 5.
- **PR:** 13 (session), 16 (alert), 30 (08:30 start)
- **Verification:** TLA `KillSwitch!DataHealthManageOnly`, PBT `ops.session_backoff_alert`, PBT `ops.no_outage_before_the_login_window`
- **Acceptance:**
  - Property: for every outage of duration t, a `broker.session_lost` alert is sent iff t ≥ 2 minutes, exactly once per outage, and retry intervals never decrease within an outage. Harm: either alert spam or a silent outage during market hours.
  - Property: while the session is down, no entry order is attempted for bots on that broker account. Harm: orders fail or are retried blindly.
  - Property: checks before 08:30 IST never alert and reset the watch, so a session down since midnight alerts once, at 08:32, and only if it is still down then. Harm: a `broker.session_lost` alert every night at 00:02 trains the owner to ignore the one that matters.
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
- **Implementation (PR 15c):** `exits.close` records `exit.close_evaluated` (trade date, live price, rule ids, SMA levels, which fired, virtual or not) for every position with an enabled `close_below_sma` rule, fired or not. `live_trades.record_close_gaps(trade_date, closes)` then writes one `exit.close_gap` per evaluated position (official close, gap, gap %, the SMAs the live price and the close were below) once the nightly ingest has that instrument's close; a later run fills the closes still missing. PR 17 calls it after the nightly ingest.

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
- **Implementation (PR 17):** `deploy/hat/docker-compose.yml` (project `hat`): `postgres` (image `postgres:18.x`, named volume `hat_pgdata`, `pg_isready` healthcheck) and `backend` (built from `deploy/hat/backend.Dockerfile`: python:3.14-slim, `backend/requirements.txt` constrained to the CI lock plus fyers-apiv3 `--no-deps`; `HAT_ENABLED=true`, `HAT_DATA_ROOT=/data/hat` on volume `hat_data`, the Fyers token directory on `hat_fyers`; `/ping` healthcheck; starts once `postgres` is healthy); `frontend` only under the `frontend` profile. Every published port is `127.0.0.1:...` (the VPN reaches the host, Docker publishes nothing beyond loopback). Every service reads the repo-root `.env` through `env_file` (Postgres's own variables are interpolated from `--env-file .env`); the file holds no secret value. CONF `ops.compose_services` (`backend/tests/hat/conformance/test_conf_ops.py`) checks all of this.

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
- **Implementation (PR 17):** `scripts/hat_deploy_guard.py`, run first in every deploy (runbook §9). The window is 08:30 ≤ t < 15:35 IST. The guard runs on the host before the stack is up, so it cannot read `hat_trading_calendar`: it fails closed and treats every weekday as a trading day; `--calendar` takes an export of that table whose rows win (a holiday is free, a special weekend session is guarded). It exits 2 when refused; `--force` needs `--reason` and appends one JSON line to `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl`.

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
- **Implementation (PR 17):** `scripts/hat_backup.py run` writes one set per run (`hat.dump`, a custom-format `pg_dump` read from its stdout; `hat_data.tar`, every file under `HAT_DATA_ROOT`; `manifest.json`, size and sha256 of both files and of every member of the tar) to the local target, copies it to the remote target and re-reads both against the manifest; any missing, resized or changed file exits 1, and the host timer's `OnFailure=` unit sends the alert (runbook §9). Both targets are directories: the external disk and a mounted cloud bucket (rclone or s3fs), so the script holds no bucket keys. Sets beyond `--keep` (14) are pruned only after a verified run. The database password reaches `pg_dump` only through `PGPASSWORD` or `~/.pgpass`.
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

The dashboard's read routes (`app/hat/api/dashboard.py`, HAT-DEC-T3) are GETs under `/api/hat`
that read Postgres and change nothing; kill, re-arm (`/arm`), mode and flatten are Telegram
commands. Money, weights and scores are decimal strings. A failed read answers 503 with a fixed
message and no database text. The list routes take `limit` (1..200, default 50; anything else is
a 422).

| Route | Answer |
| --- | --- |
| `GET /api/hat/status` | `as_of`, `trade_date`, `kill_account`, `feed_stale_at` (latest `feed.stale` of today), `regime` (latest), `bots`, `kill_switches` (state of every scope, bot, channel key), `sessions` (today's login of every active account), `jobs` (today's `hat_job_run` rows) |
| `GET /api/hat/bots` | every bot: mode, status, lifecycle, `manage_only` and its reasons |
| `GET /api/hat/watchlist` | per bot, its latest watchlist on or before today, best score first |
| `GET /api/hat/signals?limit=` | the latest signals, newest first |
| `GET /api/hat/positions` | the open positions, oldest first |
| `GET /api/hat/allocations` | the latest nightly allocation day, one row per bot |
| `GET /api/hat/regime?limit=` | the regime history, newest first |
| `GET /api/hat/events?limit=` | the latest events of the log, newest first |

## §9 Runbook

The operator's procedures (PR 17). Each `OPS` check below is a checklist item: when it is
performed, add a row to the check log at the end of this section with the date, initials and
result. CONF `ops.runbook_complete` keeps one `###` procedure here for every `OPS` name used in
the specs and every §4 failure case.

| OPS check | When | What |
| --- | --- | --- |
| `ops.host_checklist` | Before enabling confirm or auto; after any host change | NTP offset, static IP registered with Fyers, UPS, failover link, VPN-only dashboard |
| `ops.env_permissions` | After any `.env` change | Mode 0600, service-user owner, encrypted disk |
| `ops.deploy_window` | Every deploy | Outside 08:30–15:35 IST on trading days; reconcile on start |
| `ops.failover_drill` | Quarterly | Switch to 4G; confirm no orders, alert sent, stops intact |
| `ops.backup_restore_drill` | Monthly | Restore latest backup to a scratch DB; compare event count and latest trade date |
| `gapscan.latency_observed` | Daily for the first 2 weeks of live data | 09:16 gap-scan finish time against the 09:20 bar |

The decision rows also name two of these checks by older names: `host_static_ip_ups_failover`
(HAT-DEC-T9) is `ops.host_checklist`, and `env_file_permissions` (HAT-DEC-T13) is
`ops.env_permissions`.

Conventions. Commands run from the repository root on the host, as the service user unless they
say otherwise. `$COMPOSE` stands for
`docker compose --env-file .env -f deploy/hat/docker-compose.yml`. Database commands take the
password from `PGPASSWORD` or `~/.pgpass`, never from the command line. "Telegram" is the one
allowlisted chat. Every procedure ends by recording what happened (check log below, or the
incident in the PR or issue tracker).

### Start and stop

1. Start (or bring up after a host reboot): `python scripts/hat_deploy_guard.py && $COMPOSE up -d`.
   Docker restarts the containers on its own after a crash (`restart: unless-stopped`); the guard
   is for planned starts.
2. Check: `$COMPOSE ps` shows `postgres` and `backend` healthy; `curl -fsS http://127.0.0.1:8000/ping`
   answers; Telegram `/status` answers.
3. Stop (planned, outside the window): `python scripts/hat_deploy_guard.py && $COMPOSE stop backend`.
   The backend has 30 s to stop the feed, the scheduler and the engine: running job bodies get
   25 s, and any still running then are logged by name (look for `abandoned` in the log and
   check those jobs after the next start). Stop `postgres` only after the backend.
4. Logs: `$COMPOSE logs -f backend` (one JSON object per line, HAT-OPS-070).
5. Never `$COMPOSE down -v`: it deletes the database and data volumes.

### Morning login (daily Fyers login)

1. 08:30 IST: Telegram sends the Fyers login link (`login.prompt`). Open it and log in at Fyers.
2. Fyers redirects to the registered `REDIRECT_URI`; copy the `auth_code` from the redirected URL
   into the login dialog in the app header. Or send `POST /api/hat/auth/fyers/token` with the
   header `X-HAT-Request: 1` (HAT-DEC-BD5) and the JSON body `{"auth_code": "<code>"}`. The token must be for the HAT Fyers account: a login
   with another Fyers user is refused once that account is onboarded.
3. Check: the header shows Fyers connected; `/status` shows the broker session live.
4. No entries are placed until the login is done; stops and exits that rest at the broker are not
   affected.

### Missed login

1. 08:32 IST: if the session is still down, Telegram sends one `broker.session_lost` alert
   (HAT-OPS-031). 09:05 IST: Telegram sends the second prompt, URGENT when real positions are
   open (HAT-TG-021), and each bot on the account is manage-only (`broker_health`, HAT-RISK-063)
   until the session is live. If it is missed too, there are no entries that day (HAT-DEC-T14).
   Only the broker-side stops protect open positions: without a session there are no ticks, no
   reconciliation and no GTT changes, so breakeven, trim and the MA exits are off.
2. Late login: send `/login` in Telegram for a fresh link, then follow "Morning login". Entries
   resume from the next trigger after the session is live; missed triggers are not replayed.
3. If the token was revoked mid-day, `GET /api/hat/auth/fyers/status` shows disconnected: log in
   again the same way.

### Deploy and planned restart: `ops.deploy_window`

1. Pick a time outside 08:30–15:35 IST on a trading day (weekends and NSE holidays are free).
2. Run the guard: `python scripts/hat_deploy_guard.py`. Exit 2 means refused. It treats every
   weekday as a trading day unless given `--calendar` with an export of `hat_trading_calendar`:
   `$COMPOSE exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -At -F, -c "select date, is_trading from hat_trading_calendar" > /tmp/hat_calendar.csv`.
3. Deploy: `git pull` (or check out the release), then
   `python scripts/hat_deploy_guard.py && $COMPOSE up -d --build`.
4. Emergency deploy inside the window only for a fix that cannot wait:
   `python scripts/hat_deploy_guard.py --force --reason "<why>"`. The forced run is appended to
   `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl`; copy the reason into the check log.
5. After the start, follow "Restart recovery: reconcile, then resume".

### Restart recovery: reconcile, then resume

Covers every restart: planned, a crash, or the machine coming back after being down.

1. On start the scheduler marks any run left `running` by the previous process as interrupted, so
   it can run again (`recover_interrupted`); jobs resume from `hat_job_run` and a job that
   already succeeded for today is not run again (HAT-OPS-010).
2. Before any entry, every account's book is reconciled with the broker's order and trade books
   and its fills are read (HAT-OPS-003). A broker that cannot be read, or an HT order HAT does
   not know, places no entry and sends an alert; stops and sales still go out (D118). Entries left Triggered by the previous process
   are placed once the feed is fresh, and only on their own trade date; a restart after 08:45
   re-arms the day from the stored 5-minute bars and the recorded gap scan.
3. Check on Telegram: `/status` (session live, kill switches, feed), `/positions` (every open real
   position shows its stop). If the session is not live, follow "Morning login".
4. If a `reconcile.mismatch` alert arrives, follow "Reconciliation mismatch".

### Reconciliation failed or a row skipped

1. Alert `reconcile.failed` (account): the first reconciliation of the trade date failed (the
   broker could not be read, or the database failed; the backend log says which). No entry opens on that account until one succeeds (the 5-minute pass retries);
   stops and sales still go out. Check the Fyers session (`/status`, "Morning login").
2. Alert `reconcile.unreadable_row` (rows): order rows without an HT tag (manual or foreign
   orders) that HAT could not read were skipped; the reconciliation went on. Check the Fyers
   order book for an odd manual order. An HT-tagged row HAT cannot read fails the run instead.

### Failure: Machine down

1. While the host is down, broker-side stops (intraday SL-M or GTT) protect every open position;
   nothing else is needed during market hours.
2. Bring the host back, then `$COMPOSE up -d` (no guard needed for an unplanned outage; record it).
3. Follow "Restart recovery: reconcile, then resume", then check missed jobs as in "Failed nightly
   job".

### Failure: Data missing or fails the quality gate

1. Alert: `quality.failed` or `ingest.failed`. The data-health kill makes the affected bots
   manage-only: no new scans or entries; exits and stops continue (HAT-OPS-030).
2. Look at the job rows: `select job, trade_date, status, error_code from hat_job_run where trade_date = current_date order by started_at;`
   and the backend log for the failing job.
3. Fix the cause (source late, MCP endpoint down, a bad row). The scheduler retries a failed job
   10 minutes after each failed run while it is still that date (a chained job such as the gate
   until 08:30 the next morning); a retry that fails the same checks sends no second alert. Once
   the gate passes, the health kill clears on
   its own and the chain (ca.adjust, features, regime, allocate, scan, watchlist) continues.
4. Do not re-arm by hand: a data-health kill re-arms automatically when the check passes.

### Failure: 22:00 fallback fails

1. Alert at 22:00: neither the MCP nor the NSE bhavcopy download has the day. No scan runs; bots
   stay manage-only for the next session (HAT-OPS-022).
2. Check NSE's site for a delayed publication and the backend log for the fallback's error code.
3. When the file is out, a failed `ingest.nightly` is retried by the scheduler while it is still
   the same date. After midnight the scheduler does not run a past trade date again; the next
   night's ingest runs as usual and the missed day stays a gap: record it as an incident.
   **[OWNER-PENDING]** a CLI to re-run one job for a past trade date.

### Failure: Broker session drops

1. HAT retries with backoff (1 s doubling to 30 s); after 2 minutes Telegram gets one
   `broker.session_lost` alert (HAT-OPS-031). Bots on that account are manage-only; stops rest at
   the broker.
2. Check Fyers status and the host's internet link. If the token expired or was revoked, log in
   again ("Morning login").
3. If the host is on the 4G link, see "Failover drill": no entries until the static line returns.
4. Recovery is automatic once the session is back; check `/status`.

### Failure: Feed stale

1. Alert: `feed.stale`. Bars and triggers stop for the stale symbols; broker-side stops are not
   affected; MA exits at 15:20 use the live price, so a feed still stale then is an incident.
2. Check the internet link and Fyers status; `$COMPOSE logs backend | grep feed`.
3. If the socket does not come back within a few minutes, restart the backend (a planned restart
   inside the window needs `hat_deploy_guard.py --force --reason "feed stale"`), then follow
   "Restart recovery: reconcile, then resume".

### Failure: Order rejected

1. Alert: `order.rejected` with its reason. HAT retries a rejected order at most once
   (HAT-OPS-032); a second rejection ends the signal.
2. Read the reason: price band, margin, freeze, session, unregistered IP (see "Failover drill").
3. Fix the cause; do not place the order by hand (there is no manual trading). A rejected stop is
   placed again by the next fill sync (`stop.not_placed`); if it keeps failing, see "GTT missing".

### Failure: Duplicate trigger

1. A second trigger for the same bot, playbook, instrument and trade date places no second order:
   the signal id is the idempotency key (HAT-OPS-033). Nothing to do unless an alert says a
   second order reached the broker.
2. If one did: `/kill`, then compare `/positions` with the Fyers order book and treat it as
   "Reconciliation mismatch". Record it as a bug.

### Failure: Close vs live price

1. Known gap, not an incident: MA exits at 15:25 use the live price, NSE's official close is the
   last-30-minute weighted average. Each evaluation records `exit.close_evaluated`; after the
   nightly ingest `exit.close_gap` records the gap (HAT-OPS-034).
2. Weekly in shadow mode: review the gaps (`select payload from hat_event where event_type = 'exit.close_gap' order by created_at desc limit 50;`)
   and raise a spec change if exits are systematically biased.

### Kill switch and re-arm via Telegram

1. Kill: send `/kill`. Every bot becomes manage-only and pending approvals are cancelled; exits
   and stops keep running (HAT-TG-034). Drawdown and data-health kills trip on their own.
2. Before re-arming: find why it tripped (`/status`, the `kill.tripped` event), fix it, and check
   `/positions` shows every open position with its stop.
3. Re-arm: send `/arm`, then press Confirm on the confirmation card (second confirmation,
   HAT-TG-033). `kill.rearmed` is recorded. `/resume` does not lift a kill switch.
4. A data-health kill re-arms automatically once the data check passes; do not `/arm` it.

### Flatten

1. Only to get out of everything now: send `/flatten`, then press Confirm. HAT sells every open
   real position through exit-only orders and records `flatten.executed` as a discipline breach
   (HAT-TG-035).
2. Check `/positions` and the Fyers positions page are both empty; `/kill` too if no new entries
   should follow.

### Reconciliation mismatch

1. Alert: `reconcile.mismatch` (kind, tag or symbol, bot). Entries for that bot are blocked; HAT
   never auto-closes or adopts a position (HAT-BRK-111).
2. Compare the Fyers order book, trades and positions with `/positions` and `hat_order`.
3. A broker position HAT does not know: close or keep it at Fyers yourself; it is not HAT's.
   A HAT position missing at the broker or a quantity difference: find the fill or cancel that HAT
   missed (backend log, Fyers trade book) and record it as a bug.
4. The block clears at the next reconciliation that matches. Record the incident.

### GTT missing

1. Alerts: `gtt.missing`, `gtt.unverified`, `gtt.swap_failed` or `stop.not_placed`. `gtt.verify`
   at 23:00 refreshes and re-places GTTs; `gtt.swap` at 15:25 retries every minute and keeps the
   intraday stop until the GTT is confirmed.
2. Check the Fyers GTT page against `/positions`: every open real position needs one GTT at its
   current stop.
3. If the broker keeps refusing: check the session (log in again), the price band and the
   quantity. A position that stays unprotected overnight is an incident: decide whether to
   `/flatten` it the next morning.

### Failed nightly job

1. Alert or `/status` shows a failed job. List today's runs:
   `select job, status, error_code, started_at, finished_at from hat_job_run where trade_date = current_date order by started_at;`
2. A failed run is retried by the scheduler every 10 minutes while it is still that date; jobs
   after it in the chain wait for it (HAT-OPS-021). Exits and stops never wait (HAT-OPS-004).
3. Fix the cause from the error code and the backend log, then watch the next retry succeed.
4. A run stuck in `running` after a crash is reaped at the next start; restart only outside the
   window (deploy guard).

### Gap-scan latency: `gapscan.latency_observed`

1. For the first two weeks of live data, each trading day after 09:20, read the `gapscan` run:
   `select started_at, finished_at from hat_job_run where job = 'gapscan' and trade_date = current_date;`
2. Record the finish time in the check log. A finish at or after 09:20:00 also raises a health
   alert (HAT-DATA-122). If it is late on more than one day, raise the scan's latency assumption
   with the owner.

### Backup and restore drill: `ops.backup_restore_drill`

Nightly backup (HAT-OPS-060): a systemd timer on the host runs, at 23:30 IST, as root (it reads
the Docker volume) with the `.env` variables loaded (`EnvironmentFile=`):

```
python scripts/hat_backup.py run \
  --local "$HAT_BACKUP_LOCAL" --remote "$HAT_BACKUP_REMOTE" \
  --data-root "$(docker volume inspect -f '{{ .Mountpoint }}' hat_hat_data)"
```

`HAT_BACKUP_LOCAL` is the external disk and `HAT_BACKUP_REMOTE` a mounted cloud bucket. The run
exits 1 when any file is missing or differs at either target, and the timer's `OnFailure=` unit
sends the alert. pg_dump must be version 18 (or pass `--pg-dump` with the postgres container's,
see the script's help).

Monthly drill:

1. Pick the latest set on the remote target and verify it: `python scripts/hat_backup.py verify "$HAT_BACKUP_REMOTE/<set>"`.
2. Restore the dump into a scratch database:
   `createdb -h 127.0.0.1 -U "$DB_USER" hat_restore_drill` and
   `pg_restore -h 127.0.0.1 -U "$DB_USER" -d hat_restore_drill --no-owner "$HAT_BACKUP_REMOTE/<set>/hat.dump"`.
3. Compare with the live database (both queries on each): `select count(*) from hat_event;` and
   `select max(trade_date) from hat_job_run where status = 'succeeded';`. The restored counts
   equal the live ones as of the backup time (the live event count can only be larger).
4. Extract the data tar into a scratch directory (`tar -xf .../hat_data.tar -C /tmp/hat_restore`)
   and check the newest Parquet partition and raw zip are there.
5. Clean up: `dropdb -h 127.0.0.1 -U "$DB_USER" hat_restore_drill` and remove the scratch
   directory. Never point the backend at the scratch database. Record the drill in the check log.

### Restore from backup

1. Stop the backend (`$COMPOSE stop backend`); outside the window unless the database is lost.
2. Verify the set (`hat_backup.py verify`). Recreate the database:
   `$COMPOSE exec -T postgres dropdb -U "$DB_USER" "$DB_NAME"`, `... createdb -U "$DB_USER" "$DB_NAME"`,
   then `pg_restore -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" --no-owner <set>/hat.dump`.
3. Restore the data root into the volume:
   `$COMPOSE run --rm --no-deps -v "<set>:/restore:ro" backend tar -xf /restore/hat_data.tar -C /data/hat`.
4. Start the backend and follow "Restart recovery: reconcile, then resume". Positions changed at
   the broker since the backup show as reconciliation mismatches: resolve each one.

### Failover drill: `ops.failover_drill`

1. Quarterly, outside market hours with no order expected: switch the host to the 4G/5G link.
2. Confirm that broker order calls are refused (the failover IP is not registered with the Fyers
   app) or not attempted, that a health alert reaches Telegram over the 4G link, and that every
   open position still shows its GTT on the Fyers GTT page (HAT-OPS-043).
3. Switch back to the static line, check `/status`, and record the drill.
4. In a real failover during market hours: no new entries until the static line returns; do not
   force entries; broker-side stops cover the positions.

### Host checklist: `ops.host_checklist`, `host_static_ip_ups_failover`

Before confirm or auto mode is enabled on any bot, and after any host change (HAT-OPS-041):

1. Linux host, NTP synchronised with an offset under 1 s: `timedatectl` shows
   `System clock synchronized: yes`; `chronyc tracking` (or `ntpq -p`) shows the offset.
2. The ISP static IP is the one registered with the Fyers app: `curl -fsS https://ifconfig.me`
   from the host, compared with the Fyers app settings.
3. The UPS carries the host and router through a pulled plug for at least 10 minutes; the 4G link
   comes up on its own when the static line is unplugged ("Failover drill").
4. The dashboard and API are reachable only over Tailscale/WireGuard: from outside the VPN,
   ports 8000, 3000 and 5432 do not answer; `docker compose ps` shows every port on 127.0.0.1.
5. Record each item with its value in the check log.

### Env file permissions: `ops.env_permissions`, `env_file_permissions`

After any change to `.env` (HAT-OPS-050):

1. `chmod 600 .env` and `chown <service-user>: .env`; check with `stat -c '%a %U' .env`
   (expect `600 <service-user>`).
2. The disk holding the repository and `.env` is encrypted (LUKS): `lsblk -f` shows `crypto_LUKS`
   under it.
3. `git status --ignored .env` shows it ignored; `.env` is never copied into an image (the
   `.dockerignore` files keep it out of the build context) or a backup set.
4. Record the check in the check log.

### Check log

| Date | Check | Initials | Result |
| --- | --- | --- | --- |

#### HAT-OPS-090: Runbook covers every OPS check and failure
- **Statement:** The runbook has one procedure for each OPS check and each MA failure case listed in §4.
- **Source:** MA "Failure handling", "Deployment and security"; PLAN PR 17 scope (runbook in this section).
- **PR:** 17
- **Verification:** CONF `ops.runbook_complete`
- **Acceptance:**
  - Property: every `OPS` verification name used anywhere in `docs/specs/hat/` and every bullet of §4 has a matching procedure heading in this section. Harm: at 09:20 on a bad day the operator has no tested procedure to follow.
