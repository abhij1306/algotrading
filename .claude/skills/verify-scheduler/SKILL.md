---
name: verify-scheduler
description: How to run, read and update the checks behind Hire a Trader's job scheduler, job-run recording and the verification harness itself (TLA+ JobScheduler, ops properties, job_run uniqueness, CONF checks of the TLA toolchain, Hypothesis profile, CI job and verify skills). Use before and after any change to backend/app/hat/ops, hat_job_run, verification/tla, the CI verification job or a verify skill.
---

# verify-scheduler

Checks that a scheduled job runs to success at most once per (job, trade_date), that a failed or interrupted run can be retried, that nothing starts while HAT is disabled, that the windowed `exits.close` job runs only inside 15:20-15:25 IST and sends nothing from 15:30 on, that the 15:25 live price of each MA exit is kept next to the official close, that the deployment (Compose file, deploy-window guard, backup manifest) and the runbook hold their rules, and that the verification machinery (TLC runner, Hypothesis profile, CI job, skills) is itself intact. Requirement IDs (index: `docs/invariants.md`): (HAT-OPS-034), (HAT-OPS-040, 042, 060, 090), (HAT-EXEC-074, HAT-DEC-B18), (HAT-SCH-072), (HAT-VER-030..034, 040, 050, 060, 070..072).

## Properties

- `TLA JobScheduler!AtMostOnceSuccess` One body is running at a time and at most one success is ever recorded for a key, even when callers race, retry or restart. Harm: a nightly job such as ingest or allocation runs twice and double-counts rows or orders.
- `TLA JobScheduler!FailedJobRetryable` A call that finds a failed or interrupted run is allowed to run it again. Harm: one transient failure leaves the day's job dead until someone edits the database.
- `TLA JobScheduler!NoJobWhenDisabled` With `HAT_ENABLED` false the scheduler never exists and no job is ever started. Harm: a developer machine or a test run starts scheduled HAT jobs by accident.
- `TLA JobScheduler!NoStuckRunning` A run never stays `running` forever: a crash is reaped at the next start. Harm: a crashed run blocks its key and the job never runs again that day.
- `TLA JobScheduler!ExitsCloseInWindow` `exits.close` evaluates only between 15:20 and 15:25 IST (the scheduler never starts it from 15:25 on and its body refuses outside the window), succeeds at most once a day, and none of its sales reaches the broker from 15:30 on, even from a run that started at 15:24. Harm: MA exits fire on a stale or after-hours price, twice, or as orders the exchange will not take.
- `TLA NightlyPipeline!FallbackAt2200` A day is ingested from the MCP only after a probe matched, never probed at or after 22:00 or twice at once, and the bhavcopy fallback runs only at 22:00 with no match. Harm: stale or wrong-day data enters the store, or the fallback fires early.
- `TLA NightlyPipeline!NoDataNoScan` If neither source has the day by 22:00 the run fails with an alert and the data-health kill, and no scan runs. Harm: bots act on yesterday's data.
- `TLA NightlyPipeline!NoScanAfterGateFail` A failed quality gate fails the job, trips the health kill and no scan runs after it. Harm: a bad day is scanned.
- `TLA NightlyPipeline!ExitsIndependentOfPipeline` Exits and stops are sent whatever state the nightly pipeline is in. Harm: a data outage leaves positions without protection.
- `TLA NightlyPipeline!NightlyPipelineOrder` The steps run in order ingest, gate, then the later steps, each only after the steps it waits for succeeded (the adjustment after the ingest, features after the gate and the adjustment, PR 33). Harm: a scan reads a day not yet ingested or gated.
- `TLA NightlyPipeline!AdjustIndependentOfGate` An ingested day's corporate-action adjustment eventually succeeds, whatever the gate decides (PR 33). Harm: a failed gate hides the next session's split of a held stock.
- `TLA NightlyPipeline!SnapshotEveryNight` The bot snapshot (drawdown kills) runs every trading night, waiting on no step (PR 33); the model has no 23:30 (its clock ends at 22:00), so the time itself is in the job table (`daily/runtime.py`). Harm: a failed gate or allocation leaves a drawdown kill unmeasured.
- `TLA NightlyPipeline!IngestDecides` By 22:00 the ingest has either its data or a failure, never neither (a lost fallback leaves the pipeline waiting forever). Harm: the nightly job hangs and nobody is told.
- `TLA DailyCycle!IntradayOrder` The intraday jobs first start in the order of the daily job table (login prompt 08:30, live preparation 08:45, pre-open gaps 09:00-09:08, gap scan 09:16, the regime-gate refresh 09:20, `exits.close` 15:20-15:25, `gtt.swap` 15:25, session end 15:30, reports 15:40); no `exits.close` run starts once `gtt.swap` has; every entry order is placed inside 09:20-15:20, after today's gate refresh and with a live broker session. Harm: entries fire on an unformed opening range, yesterday's gate or without a session, or a GTT is placed for a position that is still being exited.
- `TLA DailyCycle!AllocateAfterRegime` `allocate` starts for a trade date only after `regime` succeeded for it, and an allocation never changes an open position's quantity or stop. Harm: weights use yesterday's regime, or a weight cut forces sales the strategy did not call for.
- `CONF ci_runs_models_for_merged_targets` Every model a README row names once all its PRs have merged is checked by CI: `verification-tla` runs `run.sh` and `run.sh --broken` unfiltered and unconditionally, `run.sh` with no argument checks every `*.tla`, and the model has its cfg and a broken variant expecting each property. Harm: a merged high-harm promise ships with its model never run.
- `PBT ops.job_run_recording` On the real `run_job`, random calls, body failures, simulated crashes and restarts always leave one row per key, with a stable error code (never raw exception text), correct timestamps, and at most one successful body per key. Harm: an audit row lies about what ran, or an error message leaks a secret.
- `PBT ops.disabled_starts_nothing` With HAT disabled `HatRuntime.start` creates no task (scheduler, feed, Telegram poller) and touches no logging, database or runtime part. Harm: HAT trades or polls where nobody opted in.
- `PBT ops.refuses_non_postgres` With HAT enabled on any dialect but `postgresql`, `HatRuntime.start` raises `HatConfigError` before a task, a log handler, its parts or the database exist. Harm: HAT runs without the database triggers that protect it.
- `CONF hat_runtime_requires_postgres` The same refusal for the named non-Postgres dialects, and `app/main.py` hands `HatRuntime` the engine's own dialect. Harm: the guard checks a constant instead of the real database.
- `PBT ops.calendar_skip` For every calendar of holidays and every instant, a trade-date job is never due on a non-trading day or a day the calendar does not know, an every-day job is not held back, and `HatScheduler.due_jobs` starts exactly the due jobs. Harm: bots act on a holiday or alert about data that never existed.
- `CONF existing_suite_green` CI runs on every pull request with the backend, Postgres, verification, frontend and API-schema jobs, none skippable by `if:` or allowed to fail. Harm: a red suite merges.
- `PBT ops.close_gap_recorded` Every `close_below_sma` evaluation of the close window records the live price it used, fired or not; once the nightly ingest has the official close, exactly one `exit.close_gap` per position records that close and its gap, against the same position; a close still missing is recorded by a later run and a repeated run changes nothing. Harm: the backtest/live gap of MA exits cannot be measured.
- `PBT ops.log_records_structured` Every record under `app.hat` renders as one JSON line with `ts`, `level`, `msg` and each context key given. Harm: logs cannot be parsed during an incident.
- `PBT ops.backup_manifest` For every generated file set, `scripts/hat_backup.py`'s manifest lists each file with its size and sha256, survives a JSON round trip, equals the listing of the tar of the set, and verifies clean at both targets; one flipped byte, missing file or size change at either target is reported for that file at that target only. Harm: a damaged or partial backup passes as good and the event log or raw files cannot be restored.
- `PBT ops.deploy_window_guard` For every instant in any time zone and every calendar flag, `scripts/hat_deploy_guard.py` refuses exactly when the IST time is in 08:30-15:35 on a trading day and the run is not forced; an unlisted day trades iff it is a weekday (fail closed); the script exits non-zero exactly when refused and a forced run inside the window appends one log line with its reason. Harm: a restart mid-session misses triggers, stop moves or the GTT swap.
- `CONF ops.compose_services` `deploy/hat/docker-compose.yml` has exactly `postgres` and `backend` (the `frontend` only under a profile), Postgres 18, every published port on 127.0.0.1, every secret-like variable interpolated rather than written, the backend reading the repo `.env`, running with HAT enabled, its data root on a volume, after a healthy database. Harm: an open port exposes the trading box, or a stray service holds broker credentials.
- `CONF ops.runbook_complete` `docs/runbook.md` has a `###` procedure with steps whose heading names every `OPS` check of `docs/invariants.md`. Harm: at 09:20 on a bad day the operator has no tested procedure to follow.
- `PBT ops.status_matches_db` For every database state of bots, kill-switch rows, job runs, regime rows, broker accounts and sessions and `feed.stale` events, `GET /api/hat/status` reports each bot's mode, status and manage-only reasons, the state of every (scope, bot, channel) kill key and the account kill, the latest regime, today's session of every active account, today's job runs and the latest `feed.stale` of today, exactly as the rows hold them. Harm: the user believes the bot is protected or halted when it is not.
- `PBT dashboard_routes.positions_are_the_open_rows_as_decimal_strings` `/api/hat/positions` lists exactly the positions with no close time and a quantity left, oldest first, each price a decimal string equal to the stored one. Harm: the dashboard hides an open position, shows a closed one, or rounds a stop.
- `PBT dashboard_routes.allocations_are_the_latest_day_exactly` `/api/hat/allocations` shows only the latest allocation day, each weight and the capital a decimal string equal to the stored value. Harm: the user reads yesterday's sleeve sizes or a weight rounded through a float.
- `PBT dashboard_routes.watchlist_is_the_latest_day_on_or_before_today` `/api/hat/watchlist` shows each bot's latest watchlist day on or before today (never a future day), best score first, scores as decimal strings. Harm: tomorrow's or a stale list is read as today's.
- `PBT dashboard_routes.lists_hold_the_newest_rows_up_to_the_limit` `/api/hat/signals`, `/regime` and `/events` return min(rows, limit) rows (50 by default), newest first. Harm: the dashboard shows a stale slice, or an unbounded read loads the whole event log.
- `PBT dashboard_routes.a_limit_out_of_bounds_is_refused` A `limit` below 1, above 200 or not an integer is a 422 that reads nothing. Harm: one request reads the whole event log.
- `PBT dashboard_routes.a_failed_read_is_a_fixed_message` When the database cannot be read every dashboard route answers 503 with the fixed message and no driver text. Harm: SQL, table names or credentials leak to the browser.
- `CONF hat_routes_read_only` Every route of every router under `app/hat/api` is a GET except the two Fyers login POST routes listed in `WRITE_ROUTES` (and each listed route exists), and no GET endpoint reaches, through any `app.hat` function it calls, a session write (`add`, `commit`, `delete`, `flush`, `merge`, ...), an `insert`/`update`/`delete` statement or an event `record` (HAT-DEC-T3). Harm: the dashboard becomes a second control surface that bypasses Telegram's confirmations and the risk authority.
- `CONF hat_writes_guarded` (HAT-DEC-BD5, PR 38) Every route under `app/hat/api` that is not a GET or HEAD depends on `guard.frontend_write` (read from the route's dependency tree). Harm: a new write route ships open to cross-site requests.
- `CONF api_loopback_no_auth_wiring` (HAT-DEC-BD5, PR 38) `start_server.py` defaults `BACKEND_HOST` to `127.0.0.1`, and `app/main.py` sets `app.state.frontend_origin` to `is_frontend_origin` imported from `.utils.origins` (both read as source, never imported). Harm: the auth-less API listens on every interface, or the write guard has no rule or a looser one than CORS.
- `CONF frontend_sends_write_header` (HAT-DEC-BD5, PR 38) `frontend/lib/api/client.ts` declares `WRITE_HEADER` as `guard.HEADER`, sets it to `guard.HEADER_VALUE` and has exactly one `client.use(` middleware. Harm: a rename on either side makes every login and logout from the HAT page a 403.
- `PBT ops.entry_inputs_fail_closed` An entry on day d reads the quality state and the allocation of the night before only (the previous trading day of the calendar): the state is failed or passed only when that night's `quality.gate` run failed or succeeded, else unknown, and the allocation is that night's row or none, never an older one; without a calendar both are unknown. Harm: entries trade on an unchecked day's data, or on an old weight with an unmeasured drawdown.
- `PBT ops.window_not_starved` After a restart at 15:19 that starts any number of overdue morning jobs whose bodies block, the 15:20 tick still runs `exits.close` inside its window while they all still run (bodies run on the scheduler's own threads, one per job). Harm: catch-up runs hold the close-phase exits past 15:25 and positions carry overnight risk the strategy meant to shed (audit money-safety-engine-ops F12).
- `CONF ops.route_matches_protocol` `LiveRoute.__call__` takes exactly the `Route` protocol's parameters (`session, request, *, decide`) and `ExecutionRouter.route` does not (it also needs `now` and `check`), so the trigger service is wired to `LiveRoute`, which reads the placement check and calls the router. Harm: the router wired as the route raises on every trigger, and the trigger service logs it and drops the entry, shadow included.
- `E2E ops.simulated_day` (the plan's acceptance run, `test_db_daily_cycle.py`, Postgres-marked; not a `DB` check) A whole shadow day through `HatRuntime` (HAT enabled, PostgreSQL, fixture market, `FakeBroker`, recording Telegram): every trading-day job succeeds once and in order, a breakout and an EP gap become shadow entries, a stop crossed at 10:58 is exited before 11:00 by the live exits, the untriggered watchlist row ends Expired, the nightly chain runs from the MCP and the next night from the 22:00 bhavcopy fallback, a holiday runs only the every-day jobs, the broker gets no order, a restart at 09:21 (after the gap scan and the first bar) still makes both entries from the recorded gappers and the stored bars, and `stop` leaves no task. Harm: the parts pass alone and the assembled day does not run, or a restart mid-session loses the day's entries.
- `DB ops.job_run_unique` The database rejects a second `hat_job_run` row for one (job, trade_date), including under concurrent inserts. Harm: idempotency rests on application code alone and a race runs a job twice.
- `CONF verification harness` The TLC runner is pinned by version and sha256 and refuses a mismatching jar; every model has a cfg, stated bounds and assumptions, a code location per action and a broken variant per property; the `ci` Hypothesis profile is registered and loaded; the CI verification jobs (`verification-tla`, `verification-conformance`, sharded `verification-properties`) run everything, each inside the 5-minute target; every DB check has a broken variant; every spec requirement is well formed; verify skills are complete and current. Harm: a check silently stops running or stops being able to fail.
- `PBT ops.retry_after_failed_run` A failed run is retried `retry_after` after it ended, not after it started, so a body slower than the retry interval is not started again at once. Harm: a slow failing job runs back to back and hammers its source.
- `PBT ops.chain_carries_past_midnight` A chained job without a window whose head finished late still starts for its own date on the next IST date until 08:30, and not after. Harm: a nightly chain whose ingest succeeded at 23:58 is dropped and the next day has no watchlist.
- `PBT ops.stop_names_abandoned_jobs` `stop` waits up to its grace for running bodies and returns (and logs) the name of each one still running. Harm: a close or swap cut off by a shutdown leaves no trace of which job was abandoned.
- `PBT ops.subscription_limit_alerts` (HAT-DATA-112, PR 73) The runtime's `subscription_alert` turns every sync whose wanted set (symbols and heartbeat) is at or above the limit into one `subscription_limit` alert of the clock's trade date, and none below it; every alert is sent on `ALERT_POOL`, never the syncing thread, and a failed send stops no later one. Harm: the owner never hears that symbols past the limit get no ticks, or a slow transport stalls every sync.
- `CONF ops.subscription_alert_wired` (HAT-DATA-112, PR 73) Every `LiveSubscriptions(...)` under `app/hat` is given its alert (fourth argument or `alert=`), and the runtime builds one. Harm: `subscription_limit` reaches only the log, as it did before PR 73.
- `PBT ops.stop_after_failed_start` After a start that failed part way, `stop` runs each step on what exists and stops the feed exactly once with no tick listener left, even when a step raises. Harm: a failed start leaves the provider socket and login hooks behind.
- `PBT ops.daily_tick_matches_reference` `DailyData.tick` reads only the two closes `reference_close` can pick (each date's from its newest ingest run) and gives the tick `tick_for_date` gives over every close of the window. Harm: a stop or limit off the exchange's tick grid is rejected.
- `PBT ops.ex_date_job_behind_flag` (PR 47, HAT-EXEC-145) For every value of `HAT_EXDATE_GTT_CHECK`, `gtt.ex_date:<account>` (08:40, retried every minute) is registered exactly when it is a true value, and every other job of the account is unchanged. Harm: the check runs on an unconfirmed broker behaviour, or the switch turns it on without a job.

## Run

```bash
# Java 17+ is needed (Temurin 21 is what CI uses): export JAVA_HOME=/path/to/jdk
verification/tla/run.sh              # every model must pass
verification/tla/run.sh --broken     # every broken variant must fail with its EXPECT line
cd backend
CI=1 python -m pytest tests/hat/properties -q --no-cov          # ops, ids, time, config, risk policy
python -m pytest tests/hat/conformance -q --no-cov              # CONF checks, no database needed
# database checks need Postgres (a throwaway database whose name ends in _test):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  python -m pytest -m postgres tests/hat/db -q --no-cov
```

`run.sh` downloads TLC v1.7.4 once into `~/.cache/hat-tla` (override with `TLA_CACHE_DIR`).

## Bug or model error

When a check fails, decide which side is wrong before touching anything.

- TLC prints a counterexample as numbered states, each naming the action (`Claim`, `FinishOk`, `Restart` and so on). Replay it against the code: each action names its function in the `\* code:` comment above it. If the real function can take those steps in that order, it is a bug in the code: fix the code, or record it as a finding in the PR. If the code cannot take a step (a guard or transaction the model leaves out), it is a model error: add the guard to the model and say so in "Scope changes".
- A failing property test prints the falsifying example and a `reproduce_failure` blob. A state-machine failure lists the steps (`call_run_job(...)`, `restart()`). Re-run that sequence by hand with the real functions.
- Never weaken a property or shrink a strategy to make a check pass. Narrowing is allowed only with a reason recorded under "Scope changes".
- Known window, a finding kept visible: a crash after a job body returns but before its success is recorded re-runs the body at the next start (`Restart` in the model). Job bodies must therefore be idempotent or commit their effects with their recording.

## Updating

- A new or changed job rule: edit `verification/tla/JobScheduler.tla`, keep a `\* code: backend/app/hat/...py:function` comment on every action, add or adjust a broken variant under `verification/tla/broken/` (file `JobScheduler__<mode>.cfg` with an `EXPECT: <Property>` line and the mode listed in the model's `ASSUME`), run both `run.sh` commands, then the property and database checks.
- A new bound or constant: list it under `Bounds:` in the model header, or `tla_bounds_declared` fails.
- A new model: needs a cfg, header, code comments and broken variants, and a line in `docs/invariants.md` and `docs/architecture.md`.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-scheduler` prints the digest.

## Coverage

TLA+ model and runner:
- `verification/tla/JobScheduler.tla` and `verification/tla/JobScheduler.cfg`
- `verification/tla/broken/JobScheduler__retry_after_success.cfg`, `verification/tla/broken/JobScheduler__split_claim.cfg`, `verification/tla/broken/JobScheduler__no_retry.cfg`, `verification/tla/broken/JobScheduler__ignore_enabled.cfg`, `verification/tla/broken/JobScheduler__no_reap.cfg`, `verification/tla/broken/JobScheduler__no_window.cfg`, `verification/tla/broken/JobScheduler__late_send.cfg`
- `verification/tla/NightlyPipeline.tla` and `verification/tla/NightlyPipeline.cfg`
- `verification/tla/broken/NightlyPipeline__probe_twice.cfg`, `verification/tla/broken/NightlyPipeline__probe_at_cutoff.cfg`, `verification/tla/broken/NightlyPipeline__fetch_without_match.cfg`, `verification/tla/broken/NightlyPipeline__fallback_always.cfg`, `verification/tla/broken/NightlyPipeline__nodata_no_kill.cfg`, `verification/tla/broken/NightlyPipeline__nodata_no_alert.cfg`, `verification/tla/broken/NightlyPipeline__scan_ignores_gate.cfg`, `verification/tla/broken/NightlyPipeline__gate_fail_no_kill.cfg`, `verification/tla/broken/NightlyPipeline__exit_needs_pipeline.cfg`, `verification/tla/broken/NightlyPipeline__skip_order.cfg`, `verification/tla/broken/NightlyPipeline__no_fallback.cfg`
- `verification/tla/DailyCycle.tla` and `verification/tla/DailyCycle.cfg`
- `verification/tla/broken/DailyCycle__gapscan_before_preopen.cfg`, `verification/tla/broken/DailyCycle__no_gate_refresh.cfg`, `verification/tla/broken/DailyCycle__no_session_check.cfg`, `verification/tla/broken/DailyCycle__swap_before_exits.cfg`, `verification/tla/broken/DailyCycle__trigger_after_1520.cfg`, `verification/tla/broken/DailyCycle__allocate_ignores_regime.cfg`, `verification/tla/broken/DailyCycle__allocate_resizes.cfg`
- `verification/tla/run.sh`

Code under check:
- `backend/app/hat/ops/jobs.py`: `run_job`, `_claim`, `_finish`, `recover_interrupted`, `error_code`
- `backend/app/hat/ops/scheduler.py`: `runs_on`, `due`, `HatScheduler.tick`, `HatScheduler.due_jobs` (the calendar, the `until` window, `after` and `retry_after`)
- `backend/app/hat/daily/runtime.py`: `HatRuntime.start` (the enabled and Postgres gates), `daily_jobs`, `HatRuntime.periodic` (the `sync` step runs every pass; `Desks.sync_due` picks the accounts, PR 26)
- `backend/app/hat/daily/intraday.py`, `backend/app/hat/daily/nightly.py`, `backend/app/hat/daily/accounts.py`, `backend/app/hat/daily/live.py`, `backend/app/hat/daily/market.py`: the job bodies of the daily cycle
- `backend/app/hat/ops/trade_jobs.py`: `trade_jobs` (`exits.close`, `gtt.swap`, `gtt.verify`)
- `backend/app/hat/execution/manage.py`: `TradeManager.close_exits`, `TradeManager._sell` (the 15:30 check at sending)
- `backend/app/hat/engine/live_trades.py`: `record_close_evaluation`, `record_close_gaps`
- `backend/app/hat/logs.py`: `JsonFormatter`
- `backend/app/hat/models/ops.py`: `HatJobRun`
- `scripts/hat_backup.py`: `build_manifest`, `archive_manifest`, `compare_entries`, `verify_manifest`, `write_data_tar`
- `scripts/hat_deploy_guard.py`: `in_market_window`, `deploy_allowed`, `is_trading_day`, `parse_calendar`, `main`
- `deploy/hat/docker-compose.yml`
- `backend/app/hat/api/dashboard.py`: every route, `_read`, `_status`, `_kill_switches`, `_bot_view`
- `backend/app/main.py`: the `app.state.frontend_origin` wiring (PR 38)
- `backend/start_server.py`: the `BACKEND_HOST` default (PR 38)
- `frontend/lib/api/client.ts`: the write-header middleware (PR 38)

Checks:
- `backend/tests/hat/properties/test_ops.py`: `test_job_run_recording`, `test_disabled_starts_nothing`, `test_refuses_non_postgres`, `test_calendar_skip`, `test_log_records_structured`, `test_job_error_codes_are_stable_codes`, `test_close_gap_recorded`, `test_backup_manifest`, `test_deploy_window_guard`, `test_status_matches_db`, `test_retry_after_failed_run`, `test_chain_carries_past_midnight`, `test_stop_names_abandoned_jobs`, `test_stop_after_failed_start`, `test_daily_tick_matches_reference`
- `backend/tests/hat/conformance/test_conf_ops.py`: `test_compose_services`, `test_runbook_complete`
- `backend/tests/hat/properties/test_dashboard_routes.py`
- `backend/tests/hat/hat_dashboard_support.py`
- `backend/tests/hat/conformance/test_conf_api.py`: `test_hat_routes_read_only`, `test_hat_writes_guarded`, `test_api_loopback_no_auth_wiring`, `test_frontend_sends_write_header`
- `backend/tests/hat/conformance/test_conf_runtime.py`: `test_existing_suite_green`, `test_route_matches_protocol`
- `backend/tests/hat/conformance/test_conf_boundaries.py`: `test_hat_runtime_requires_postgres`
- `backend/tests/hat/db/test_db_portfolio_ops.py`: `test_ops__job_run_unique`
- `backend/tests/hat/db/test_db_daily_cycle.py`: `test_ops__simulated_day`
- `backend/tests/hat/hat_day_support.py`
- `backend/tests/hat/db/hat_db_rules.py`: `check_ops__job_run_unique`
- `backend/tests/hat/db/test_db_broken_variants.py`
- `backend/tests/hat/conformance/test_conf_verification.py`
- `backend/tests/hat/conformance/hat_conf_skills.py`
- `backend/tests/hat/conformance/hat_conf_spec.py`
- `backend/tests/hat/conftest.py`
- `.github/workflows/ci.yml`: jobs `verification-tla`, `verification-conformance`, `verification-properties`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `7fa132ddf74c0f2fb976c4cc2bb7dae11380226be5e6c223f55690f56c2e174f`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 69: `MERGED_PRS` gains 69 (the Backtested gate, HAT-PORT-061); no scheduler or harness check changed.
- PR 75a: `MERGED_PRS` gains 75; `engine/live_trades.py` imports its shared helpers from `engine/positions.py` (`verify-execution`). No scheduler check changed.
- PR 73b (PR 39 follow-up): `daily/runtime.py` builds `LiveSubscriptions` with `subscription_alert(self.notifier, self._now)` (sent off the loop, the clock's trade date), and `daily/production.py` refuses the start while a proxy would carry Fyers traffic (`verify-execution`). `test_ops.py` and `test_conf_ops.py` gain one check each. Not covered by a property: the alert's detail (none: `LiveSubscriptions` passes the code only). Nothing was narrowed.
- PR 73: `test_conf_verification.py` lists PR 73 as merged, `test_conf_ops.py` allows the admin command line `decimal`, `engine.rescale` and `engine.trades`, and `execution/accounts.py` gains `revoke_ddpi` (`verify-portfolio`). No job, job-run or harness rule changed. Nothing was narrowed.
- PR 52: `daily/accounts.py:Desks.recover` remembers a Take left unplaced as well as an auto entry in Triggered (`_left_unplaced`), and `resume_entries` also takes the signals whose placement raised in this process (`ExecutionRouter.take_stranded`); checked by `verify-execution`'s `router.crash_resumes_entry_once` (breaks recorded there). The `expire` job is unchanged (every minute); its sweep now also ends an earlier trade date's stranded entry. No job or schedule changed.
- PR 49: `HatRuntime.periodic` gains the `entries` step (every pass while the market is open: `Desks.cancel_stale_entries` with the runtime's `EntryWatch`, a `LiveData` bar listener); checked by `verify-execution`'s `manage.unfilled_entry_cancelled` (breaks recorded there). No scheduled job changed.
- PR 48: `daily/accounts.py:Desks` and `daily/runtime.py` pass the sale band and the `HAT_MPP_RESEND`/`HAT_MPP_PAGE_AFTER` settings to the fill sync and the trade manager (`verify-execution`'s `mpp.*`). No job or schedule changed.
- `ops.job_run_recording` runs 60 state-machine examples of 40 steps rather than the profile's 500 examples: each step is a database transaction on a real SQLite engine, and 500 sequences would exceed the 15-minute job budget. The invariants are not weakened.
- `JobScheduler` models one key (job, trade_date) because keys are independent rows under one unique index; two callers and three calls each stand for "any number" of racers (bounds in the model header).
- The model counts recorded successes, not body executions: a crash between the body and its recording is the `Restart` action and re-runs the body (see "Bug or model error", known window).
- `TypeOK` has no broken variant: it is a type-correctness invariant, not a promise.
- PR 3 updated the harness files this skill covers, without narrowing any check: `MERGED_PRS` now includes 3; `KNOWN_GAPS` gained two TLA names that belong to PR 13 and PR 15 (see `verify-events`); the DB-check coverage test accepts `events.partitions_ahead` through its own broken-ensure variants; the catalog comparison orders by name and definition.
- PR 13 updated the harness files this skill covers, without narrowing any check: `MERGED_PRS` includes 13; `KNOWN_GAPS` lost `TLA OrderReconciliation!AtMostOneEntryPerSignal` and gained `TLA ExecutionRouter!AutoNeedsGtt` (see `verify-execution`); `test_ops.py` gained `test_session_backoff_alert` and `test_backoff_delay_is_monotone_and_capped`.

- PR 15c added the `exits.close` part to `JobScheduler` as an independent sub-model (the clock in four spans, one trade date; bounded by `MaxCalls` starts) rather than a model of its own: it shares the scheduler's start gate. It checks the window and the 15:30 sending cut-off, not the minute-level retry interval (`retry_after` is one minute, inside the five-minute window). `MERGED_PRS` now includes 15 and `KNOWN_GAPS` lost `TLA StopPlacement!StopOnlyTightens` (the model is in `verify-stops`). `ops.close_gap_recorded` drives virtual positions (the evaluation record is the same for real ones) with at most three positions per example.

- PR 7 split the nightly-pipeline promises (probe window, fallback at 22:00, no data no scan, gate, exits independent) out of `JobScheduler` into the `NightlyPipeline` model, because the product of the two state spaces was too large to check; `JobScheduler` keeps the job-run promises. The README and spec property names were renamed to match; no check was weakened. PR 7 also added an `on_finish` hook to `run_job` (called inside the final-status transaction; `ops.job_run_recording` still passes) and `MERGED_PRS` now includes 7, `KNOWN_GAPS` unchanged.
- PR 15b updated the harness without narrowing any check: `KNOWN_GAPS` lost `TLA ExecutionRouter!AutoNeedsGtt`, `TLA ExecutionRouter!NoAddToOpenPosition` and `TLA ExecutionRouter!ModeChangeNextSession` (the `ExecutionRouter` model now exists), `MERGED_PRS` is unchanged (15c adds 15), and two models joined `verification/tla` (`ExecutionRouter`, `ApprovalToken`; their checks and breaks are in `verify-execution`). The new property file `test_router.py` is placed by `ci_property_shards.py`'s partition like any other.
- PR 16 added the Telegram jobs to `JobScheduler` (`TgTick`, `LoginFlip`, `Prompt`, `Reprompt`, `Report`; `LoginPromptOrder`, `ReportsAfterClose`), whose breaks are in `verify-telegram`, and `16` to `MERGED_PRS`; `KNOWN_GAPS` is unchanged.
- PR 17 put `AllocateAfterRegime` and `IntradayOrder` (named `JobScheduler!...` until then and never built) in a new model `DailyCycle` rather than in `JobScheduler`, whose state space is already about a million states; the README rows were renamed to match. `DailyCycle` checks the order of first starts on one trade date with the scheduler running all day: a job's retry after a failure is modelled only for the gate refresh, `exits.close`, `regime` and `allocate` (the jobs whose outcome something waits on), a restart late in the day (which starts every due job at once) is outside it, and each job run is one step (an `exits.close` run still running at 15:25 is handled by `gtt.swap` leaving a position whose exit sale is working). No entry before 09:20 is guarded twice (the trigger window and the 09:20 gate refresh), so removing either guard alone cannot show it; the breaks remove the gate refresh and the 15:20 end of the window instead. `NightlyPipeline`'s `LaterStep` now points at `scheduler.py:due` (ordering by `after`) instead of `run_job`.
- PR 17 added the read-only dashboard routes. `ops.status_matches_db` compares against an oracle that takes the latest row of each kill key by time; the generated rows have distinct times, so the tie rule of `kill._latest` (a tripped row wins) is covered by the risk checks, not here. The status report shows `feed.stale` as the latest such event of the day: recovery writes no event, so the feed's current health shows through the health kill. The Telegram CONF checks `gov__lever_catalogue` and `no_override_command` (in `verify-telegram`) now judge only writing (non-GET) routes, because reads are held to `hat_routes_read_only`; they still fail on a `POST /api/hat/regime`. Their route list reads `route.path`, which already carries the router prefix (it was prefixed twice).
- PR 19 fixed a flaky `ops.stop_names_abandoned_jobs` (CI: `OSError(39, 'Directory not empty')`): its held body returned `release.wait`'s `True`, so the released run's `_finish` raised in the scheduler's thread, left the row `running`, and raced the temporary directory's removal. The body now returns None and the property waits until every run row has its finish before the directory goes (`_await_runs_finished`). The property is unchanged otherwise; with the old body the new wait fails `an abandoned job never recorded its finish`.
- PR 38 added `CONF hat_writes_guarded`, `CONF api_loopback_no_auth_wiring` and `CONF frontend_sends_write_header` to `test_conf_api.py` (HAT-DEC-BD5); `hat_routes_read_only` is unchanged, and its `WRITE_ROUTES` comment names the guard.
- PR 33 changed `NightlyPipeline`: step 2 (`ca.adjust`) waits only for the ingested day and step 3 (`features`) for the gate and the adjustment (`Ready(k)`); `LaterStep(k)` became `\E r : Step(k, r)` so that the adjustment's success alone can carry weak fairness (its lookups are retried until they succeed); a `Snapshot` action and variable `snapDone` model `snapshot.bots`, which waits on no step, also under weak fairness. `NightlyPipelineOrder` was restated for the new waits, not weakened: steps 4 to 7 still need the one before, and features both. The fairness on the adjustment and the snapshot is an assumption that each eventually runs to success, not a check that it does. The traces above that showed `<LaterStep ...>` now show `<Step ...>`. The DB acceptance run `daily_cycle` still runs `NIGHTLY_CHAIN`, which no longer lists `ca.adjust` (it runs on its own `after`). 
- PR 33 (review) added `ScheduledJob.carry`: a job with no `after` and no window marked `carry` (`snapshot.bots`) carries past midnight as a chained one does. `ops.chain_carries_past_midnight` draws the kind (chained, carry, plain) and checks that a plain unchained job does not carry. This makes the `WF_vars(Snapshot)` of `NightlyPipeline` hold for a restart before 08:30; a process down until after 08:30 still misses the night.
- PR 47: `trade_jobs` registers `gtt.ex_date` only with `exdate_check` (`HatSettings.exdate_gtt_check`, passed through `daily_jobs` and `account_jobs`); the job table holds the default registration. Nothing was narrowed.

- PR 36 (HAT-EXEC-042): `daily/nightly.py` gains the `journal.check` job (23:15 trading days, after the 23:00 GTT verify). Its body (`Nightly.journal_check`) is checked by `verify-execution`'s `journal.check_alerts`. Nothing was narrowed.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model (HAT-VER-072), then the break was removed and the check returned to passing.

### TLA JobScheduler!AtMostOnceSuccess

Break: in `JobScheduler.tla`, `Claimable` accepts a `succeeded` row in the real protocol (`\/ r = "succeeded"`).

```
Error: Invariant AtMostOnceSuccess is violated.
State 2: <StartScheduler ...>
State 3: <Claim ...>
State 4: <FinishOk ...>
State 5: <Claim ...>
State 6: <FinishOk ...>
```

### TLA JobScheduler!FailedJobRetryable

Break: `Claimable` never accepts a `failed` row (`\/ FALSE`).

```
Error: Invariant FailedJobRetryable is violated.
State 3: <Claim ...>
State 4: <Restart ...>
State 5: <Claim ...>
```

### TLA JobScheduler!NoJobWhenDisabled

Break: `StartScheduler` ignores `enabled` (`/\ TRUE`).

```
Error: Invariant NoJobWhenDisabled is violated.
State 2: <StartScheduler ...>
```

### TLA JobScheduler!NoStuckRunning

Break: `Restart` leaves a `running` row alone (`row' = row`).

```
Error: Temporal properties were violated.
State 2: <Restart ...>
State 3: <StartScheduler ...>
State 4: <Claim ...>
```

### TLA JobScheduler!ExitsCloseInWindow

Break: the variant `no_window`: the scheduler starts `exits.close` whenever it is 15:20 or later (no `until`) and the body does not refuse outside the window.

```
Error: Invariant ExitsCloseInWindow is violated.
State 1: <Initial predicate>
State 2: <StartScheduler ...>
State 3: <ClockTick ...>
State 4: <ClockTick ...>
State 5: <CloseRun ...>
```

Break: the variant `late_send`: a sale is checked against the time the job started instead of the clock at sending.

```
Error: Invariant ExitsCloseInWindow is violated.
State 1: <Initial predicate>
State 2: <StartScheduler ...>
State 3: <ClockTick ...>
State 4: <CloseRun ...>
State 5: <ClockTick ...>
State 6: <ClockTick ...>
State 7: <CloseSell ...>
```

### PBT ops.close_gap_recorded

Break: in `backend/app/hat/engine/live_trades.py`, `record_close_gaps` writes the gap as live price minus close.

```
    assert Decimal(event.payload["gap"]) == official - live
E   AssertionError: assert Decimal('-0.01') == (Decimal('50.01') - Decimal('50.00'))
```

Break: `record_close_gaps` no longer skips a position whose gap it already recorded.

```
    assert again == 0
E   assert 1 == 0
```

### PBT ops.job_run_recording

Break: in `_claim`, the retry update also flips `succeeded` rows to running (`HatJobRun.status.in_(("failed", "succeeded"))`).

```
tests/hat/properties/test_ops.py:189: in call_run_job
E   assert not [True]
E   Failing test case: <state machine>
E   state.call_run_job(calls=0, ..., job='ingest.nightly', kind='ok', rows=0)
E   state.call_run_job(calls=0, ..., job='ingest.nightly', kind='ok', rows=0)
```

### PBT ops.disabled_starts_nothing

Break: `HatRuntime.start` stops checking `settings.enabled` (`if False:`), PR 17 (the PR 2 break removed the same check from the deleted `start_scheduler`).

```
FAILED tests/hat/properties/test_ops.py::test_disabled_starts_nothing - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

### PBT ops.refuses_non_postgres

Break: `HatRuntime.start` builds its parts before `require_postgres(self._dialect_name)`.

```
FAILED tests/hat/properties/test_ops.py::test_refuses_non_postgres - AssertionError: assert ['parts'] == []
```

### CONF hat_runtime_requires_postgres

Break: the same one: `HatRuntime.start` builds its parts before `require_postgres(self._dialect_name)`.

```
FAILED tests/hat/conformance/test_conf_boundaries.py::test_hat_runtime_requires_postgres[sqlite] - AssertionError: assert ['parts'] == []
FAILED tests/hat/conformance/test_conf_boundaries.py::test_hat_runtime_requires_postgres[mysql] - AssertionError: assert ['parts'] == []
```

### PBT ops.calendar_skip

Break: `runs_on` returns True whatever the calendar says (`return True` in place of `return job.days is Days.EVERY or trading(day)`).

```
FAILED tests/hat/properties/test_ops.py::test_calendar_skip - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

### E2E ops.simulated_day

Break: `ShadowExits._positions` skips the virtual positions of accounts with a desk in the live phase too (`accounts = self._managed()` in all phases), so a crossed stop waits for the 15:20 close exits.

```
tests/hat/db/test_db_daily_cycle.py:385: in _check_trades
    assert _local(stopped.closed_at) < (TRADE, STOPPED_BY)
E   assert (datetime.date(2026, 10, 21), datetime.time(15, 20)) < (datetime.date(2026, 10, 21), datetime.time(11, 0))
```

Break: `after=()` for the nightly chain in `daily/nightly.py`.

```
tests/hat/db/test_db_daily_cycle.py:315: in _check_night
E   AssertionError: ('regime', 'regime_no_breadth')
E   assert 'failed' == 'succeeded'
```

Break: `LiveRoute.__call__` calls `router.route(session, request, decide=decide)` without `now` and `check` (the router used as a `Route`), so every trigger raises a `TypeError` that `TriggerService.write` logs and drops.

```
{"level": "ERROR", "msg": "writing a trigger failed", "exc_type": "TypeError"}
E   AssertionError: assert 'Expired' == 'Triggered'
```

Break (the 09:21 restart): `Intraday.prepare` skips the recorded gappers (`if gappers is not None:` to `if False:`), so the EP candidate is gone after the restart.

```
tests/hat/db/test_db_daily_cycle.py:390: in _check_trades
E   assert None is not None
```

Break (the 09:21 restart): `HatRuntime._recover` skips `self.live.restore(today)`, so the breakout bot has no opening range after the restart.

```
tests/hat/db/test_db_daily_cycle.py:375: in _check_watchlist
E   AssertionError: assert 'Expired' == 'Triggered'
```

### PBT ops.entry_inputs_fail_closed

Break: `_quality_failed` returns `False` (passed) when the night's run is missing or unfinished.

```
E   assert False is None
E   Failing test case: test_entry_inputs_fail_closed(
E       trading=[True, False],
E       statuses=[None, None, None, None, None, None, None, None],
```

Break: `_allocation` returns the latest allocation before d (an older night's row when that night's is missing).

```
E   assert datetime.date(2026, 1, 1) == None
E   Failing test case: test_entry_inputs_fail_closed(
E       trading=[False, True, False],
```

### PBT ops.window_not_starved

Break: `_run` hands the body to the loop's default pool (`run_in_executor(None, ...)`, as `asyncio.to_thread` did).

```
E   assert False
E   Failing test case: test_window_not_starved(
E       pending=8,
E   )
```

### CONF ops.route_matches_protocol

Break: `LiveRoute.__call__` takes a keyword-only `now: datetime` like `ExecutionRouter.route`.

```
FAILED tests/hat/conformance/test_conf_runtime.py::test_route_matches_protocol
E   assert [('session', ...), ('request', ...), ('now', <_ParameterKind.KEYWORD_ONLY: 3>, ...), ('decide', ...)] == [('session', ...), ('request', ...), ('decide', ...)]
```

### CONF existing_suite_green

Break: `continue-on-error: true` on the `frontend` job of `.github/workflows/ci.yml`.

```
E   AssertionError: frontend
E   assert not True
```

### PBT ops.log_records_structured

Break: `JsonFormatter.format` pretty-prints with `indent=1`.

```
tests/hat/properties/test_ops.py:127: in test_log_records_structured
E   assert '\n' not in '{\n "ts": "..."msg": ""\n}'
```

### PBT ops.backup_manifest

Break: in `scripts/hat_backup.py`, `compare_entries` drops its checksum branch (only missing files and sizes are compared).

```
FAILED tests/hat/properties/test_ops.py::test_backup_manifest
E   assert 0 == 1
E    +  where 0 = len([])
E   Failing test case: test_backup_manifest(files={'0.parquet': b'\x00'}, target=0, how='flip', pick=0)
```

### PBT ops.deploy_window_guard

Break: in `scripts/hat_deploy_guard.py`, `WINDOW_END = time(15, 30)` (the window ends five minutes early).

```
FAILED tests/hat/properties/test_ops.py::test_deploy_window_guard - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
    |     assert allowed == (force or not (trading and inside))
    | AssertionError: assert True == ((False or not (True and True)))
```

Another break: `main` no longer calls `log_forced` for a forced run inside the window.

```
E   assert 0 == 1
E    +  where 0 = len([])
E    +  and   1 = int((True and True and True))
```

### CONF ops.compose_services

Break: the backend port in `deploy/hat/docker-compose.yml` published as `"8000:8000"` (all interfaces).

```
E   AssertionError: ('backend', '8000:8000')
E   assert '' == '127.0.0.1'
```

Another break: `POSTGRES_PASSWORD: hunter2` written inline instead of `${DB_PASSWORD:?...}`.

```
E   AssertionError: ('postgres', 'POSTGRES_PASSWORD')
E   assert None
```

### CONF ops.runbook_complete

Break: in `docs/runbook.md`, the failover drill's heading lost its check name (shortened to `### Failover drill`).

```
E   AssertionError: assert ['ops.failover_drill'] == []
E     Left contains one more item: 'ops.failover_drill'
```

Another break: `### Failure: Machine down` renamed `### Failure: host outage` (a failure case without its procedure).

```
E   AssertionError: assert ['Machine down'] == []
E     Left contains one more item: 'Machine down'
```

### DB ops.job_run_unique

Break: `UniqueConstraint("job", "trade_date")` removed from `HatJobRun`.

```
tests/hat/db/test_db_portfolio_ops.py:22: in test_ops__job_run_unique
tests/hat/db/hat_db_rules.py:468: in check_ops__job_run_unique
E   AssertionError: the database accepted: a second run of one (job, trade_date)
```

### CONF verification harness

Break: nine separate breaks, each reverted after its check failed: `run.sh` stops refusing a jar with the wrong hash; a constant dropped from the model header's `Bounds:`; the code comment removed from `FinishOk`; the `EXPECT:` line of `JobScheduler__no_retry.cfg` pointed at a property the cfg does not check; `max_examples=500` changed to 100 in the `ci` profile; the `verification` job's `timeout-minutes` raised to 30; a database check function renamed; a covered file touched without refreshing the skill's digest; a required section renamed in a skill.

```
test_tla_toolchain_pinned: assert 'refusing to run' in 'Java 17 or later is required (set JAVA_HOME or put java on PATH)'
test_tla_bounds_declared: AssertionError: ('JobScheduler.cfg', 'MaxRestarts')
test_tla_actions_mapped: AssertionError: JobScheduler.tla: action FinishOk has no code comment
test_tla_broken_variants_fail: AssertionError: JobScheduler__no_retry.cfg   assert 'AtMostOnceSuccess' in ['FailedJobRetryable']
test_hypothesis_profile_registered: assert 100 == 500
test_ci_verification_job: assert 30 <= 15
test_db_checks_match_governance_table: AssertionError: governance.exposure_checks   assert 0 == 1
test_verify_skill_sync_current: AssertionError: verify-governance is stale: run python backend/tests/hat/conformance/hat_conf_skills.py verify-governance after re-confirming
test_verify_skills_complete: AssertionError: verify-scheduler: Updating   assert ''
```

### TLA NightlyPipeline!FallbackAt2200

Break: the broken variant `NightlyPipeline__probe_twice.cfg` (mode `probe_twice` in the model's `BrokenMode` switch).

```
Error: Invariant FallbackAt2200 is violated.
State 1: <Initial ...>
State 2: <Probe ...>
State 3: <Probe ...>
```

Another break: the broken variant `NightlyPipeline__probe_at_cutoff.cfg` (mode `probe_at_cutoff` in the model's `BrokenMode` switch).

```
Error: Invariant FallbackAt2200 is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Probe ...>
```

Another break: the broken variant `NightlyPipeline__fetch_without_match.cfg` (mode `fetch_without_match` in the model's `BrokenMode` switch).

```
Error: Invariant FallbackAt2200 is violated.
State 1: <Initial ...>
State 2: <Fetch ...>
```

Another break: the broken variant `NightlyPipeline__fallback_always.cfg` (mode `fallback_always` in the model's `BrokenMode` switch).

```
Error: Invariant FallbackAt2200 is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <McpBecomesReady ...>
State 5: <Probe ...>
State 6: <Tick ...>
```

### TLA NightlyPipeline!NoDataNoScan

Break: the broken variant `NightlyPipeline__nodata_no_kill.cfg` (mode `nodata_no_kill` in the model's `BrokenMode` switch).

```
Error: Invariant NoDataNoScan is violated.
State 1: <Initial ...>
State 2: <McpBecomesReady ...>
State 3: <Probe ...>
State 4: <Fetch ...>
```

Another break: the broken variant `NightlyPipeline__nodata_no_alert.cfg` (mode `nodata_no_alert` in the model's `BrokenMode` switch).

```
Error: Invariant NoDataNoScan is violated.
State 1: <Initial ...>
State 2: <McpBecomesReady ...>
State 3: <Probe ...>
State 4: <Fetch ...>
```

### TLA NightlyPipeline!NoScanAfterGateFail

Break: the broken variant `NightlyPipeline__scan_ignores_gate.cfg` (mode `scan_ignores_gate` in the model's `BrokenMode` switch).

```
Error: Invariant NoScanAfterGateFail is violated.
State 1: <Initial ...>
State 2: <McpBecomesReady ...>
State 3: <Probe ...>
State 4: <Fetch ...>
State 5: <Step ...>
```

Another break: the broken variant `NightlyPipeline__gate_fail_no_kill.cfg` (mode `gate_fail_no_kill` in the model's `BrokenMode` switch).

```
Error: Invariant NoScanAfterGateFail is violated.
State 1: <Initial ...>
State 2: <McpBecomesReady ...>
State 3: <Probe ...>
State 4: <Fetch ...>
State 5: <Gate ...>
```

### TLA NightlyPipeline!ExitsIndependentOfPipeline

Break: the broken variant `NightlyPipeline__exit_needs_pipeline.cfg` (mode `exit_needs_pipeline` in the model's `BrokenMode` switch).

```
Error: Temporal properties were violated.
State 1: <Initial ...>
State 2: <ExitBecomesDue ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Probe ...>
State 6: <McpBecomesReady ...>
```

### TLA NightlyPipeline!NightlyPipelineOrder

Break: the broken variant `NightlyPipeline__skip_order.cfg` (mode `skip_order` in the model's `BrokenMode` switch).

```
Error: Invariant NightlyPipelineOrder is violated.
State 1: <Initial ...>
State 2: <McpBecomesReady ...>
State 3: <Probe ...>
State 4: <Fetch ...>
State 5: <Step ...>
```

### TLA NightlyPipeline!AdjustIndependentOfGate

Break: (PR 33) the broken variant `NightlyPipeline__adjust_needs_gate.cfg` (mode `adjust_needs_gate`: the adjustment waits for the gate, as before PR 33).

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 2: <Snapshot ...>
State 6: <Probe ...>
State 7: <Fetch ...>
State 8: <Gate ...>
State 12: Stuttering
```

### TLA NightlyPipeline!SnapshotEveryNight

Break: (PR 33) the broken variant `NightlyPipeline__snapshot_needs_allocate.cfg` (mode `snapshot_needs_allocate`: the snapshot waits for `allocate`, as before PR 33).

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 8: <Fallback ...>
State 9: <Step ...>
State 10: <Gate ...>
```

### TLA NightlyPipeline!IngestDecides

Break: the broken variant `NightlyPipeline__no_fallback.cfg` (mode `no_fallback` in the model's `BrokenMode` switch).

```
Error: Temporal properties were violated.
State 1: <Initial ...>
State 2: <Probe ...>
State 3: <Tick ...>
State 4: <ExitBecomesDue ...>
State 5: <Tick ...>
State 6: <ExitSend ...>
```

### TLA DailyCycle!IntradayOrder

Break: the broken variant `DailyCycle__gapscan_before_preopen.cfg` (mode `gapscan_before_preopen` in the model's `BrokenMode` switch).

```
Error: Invariant IntradayOrder is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <RunIntraday ...>
State 6: <RunIntraday ...>
```

Another break: the broken variant `DailyCycle__no_gate_refresh.cfg` (mode `no_gate_refresh` in the model's `BrokenMode` switch).

```
Error: Invariant IntradayOrder is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Tick ...>
State 6: <Tick ...>
State 7: <SessionFlip ...>
State 8: <Entry ...>
```

Another break: the broken variant `DailyCycle__no_session_check.cfg` (mode `no_session_check` in the model's `BrokenMode` switch).

```
Error: Invariant IntradayOrder is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Tick ...>
State 6: <Tick ...>
State 7: <RunIntraday ...>
State 8: <Entry ...>
```

Another break: the broken variant `DailyCycle__swap_before_exits.cfg` (mode `swap_before_exits` in the model's `BrokenMode` switch).

```
Error: Invariant IntradayOrder is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Tick ...>
State 6: <Tick ...>
State 7: <Tick ...>
State 8: <RunIntraday ...>
State 9: <RunIntraday ...>
```

Another break: the broken variant `DailyCycle__trigger_after_1520.cfg` (mode `trigger_after_1520` in the model's `BrokenMode` switch).

```
Error: Invariant IntradayOrder is violated.
State 1: <Initial ...>
State 2: <Tick ...>
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Tick ...>
State 6: <Tick ...>
State 7: <SessionFlip ...>
State 8: <RunIntraday ...>
State 9: <Tick ...>
State 10: <Entry ...>
```

### TLA DailyCycle!AllocateAfterRegime

Break: the broken variant `DailyCycle__allocate_ignores_regime.cfg` (mode `allocate_ignores_regime` in the model's `BrokenMode` switch).

```
Error: Invariant AllocateAfterRegime is violated.
State 1: <Initial ...>
State 2..11: <Tick ...>
State 12: <Allocate ...>
```

Another break: the broken variant `DailyCycle__allocate_resizes.cfg` (mode `allocate_resizes` in the model's `BrokenMode` switch).

```
Error: Invariant AllocateAfterRegime is violated.
State 1: <Initial ...>
State 2..11: <Tick ...>
State 12: <Regime ...>
State 13: <Allocate ...>
```

### CONF ci_runs_models_for_merged_targets

Break: two separate breaks, each reverted after the check failed: the `verification-tla` step `verification/tla/run.sh --broken` filtered to `verification/tla/run.sh --broken JobScheduler` in `.github/workflows/ci.yml`; the two `DailyCycle__allocate_*.cfg` broken variants moved out of `verification/tla/broken/`.

```
test_ci_runs_models_for_merged_targets: AssertionError: verification/tla/run.sh --broken   assert None is not None
test_ci_runs_models_for_merged_targets: AssertionError: ('DailyCycle', ['AllocateAfterRegime'])   assert not {'AllocateAfterRegime'}
```

### PBT ops.status_matches_db

Break: in `backend/app/hat/api/dashboard.py`, `_status` reads the account kill from the `risk` channel only.

```
E   assert False is True
E   Failing test case: test_status_matches_db(world={'bots': [], 'kills': [(None, 'data_health', 'tripped', 0)], ...})
```

Break: `_kill_switches` reads each key across both channels (`kill.current_state(rows, scope, bot_id)`).

```
E   AssertionError: assert {('account', ...'): 'tripped'} == {('account', ...th'): 'armed'}
E     {('account', None, 'health'): 'tripped'} != {('account', None, 'health'): 'armed'}
```

### PBT dashboard_routes.positions_are_the_open_rows_as_decimal_strings

Break: `_positions` drops the `qty_open > 0` condition.

```
E   AssertionError: assert [('00000000-0...al('0.0100'))] == []
E     Left contains one more item: ('00000000-0000-0000-0000-000000002000', 'SYM0', 0, Decimal('0.0500'), Decimal('0.0100'))
```

### PBT dashboard_routes.allocations_are_the_latest_day_exactly

Break: `_allocations` no longer filters to the latest trade date.

```
E   AssertionError: assert [('00000000-0...al('0.0500'))] == [('00000000-0...imal('0.05'))]
E     Left contains one more item: ('00000000-0000-0000-0000-0000000000c0', Decimal('0E-10'), Decimal('0.0500'))
```

### PBT dashboard_routes.watchlist_is_the_latest_day_on_or_before_today

Break: `_watchlist` drops the `trade_date <= today` bound.

```
E   AssertionError: assert '2026-10-22' == None
E   Failing test case: test_watchlist_is_the_latest_day_on_or_before_today(rows=[(-1, 0, Decimal('0'))])
```

### PBT dashboard_routes.lists_hold_the_newest_rows_up_to_the_limit

Break: `_signals` orders oldest first.

```
E   assert [datetime.dat...timezone.utc)] == [datetime.dat...timezone.utc)]
E     At index 0 diff: datetime.datetime(2026, 10, 21, 5, 58, tzinfo=datetime.timezone.utc) != datetime.datetime(2026, 10, 21, 5, 59, tzinfo=datetime.timezone.utc)
```

### PBT dashboard_routes.a_limit_out_of_bounds_is_refused

Break: `Limit` loses its upper bound (`Query(ge=1)`).

```
E   assert 503 == 422
E   Failing test case: test_a_limit_out_of_bounds_is_refused(route='signals', limit=201)
```

### PBT dashboard_routes.a_failed_read_is_a_fixed_message

Break: `_read` appends the driver's error text to the 503 detail.

```
E   AssertionError: assert {'detail': 'T...e/e/21/e3q8)'} == {'detail': 'T... not be read'}
```

### CONF hat_writes_guarded

Break: (PR 38) `POST /disconnect` without `Depends(frontend_write)`.

```
E   AssertionError: ('app.hat.api.fyers_auth', '/api/hat/auth/fyers/disconnect')
E   assert <function frontend_write at 0x7fb00277f950> in {<function get_fyers_auth at 0x7faffff2ceb0>}
```

### CONF api_loopback_no_auth_wiring

Break: (PR 38) `app/main.py` without the `app.state.frontend_origin = is_frontend_origin` line.

```
E   AssertionError: assert [] == ['is_frontend_origin']
```

Break: (PR 38) `start_server.py` defaults `BACKEND_HOST` to `0.0.0.0`.

```
E   assert ["'0.0.0.0'"] == ["'127.0.0.1'"]
```

### CONF frontend_sends_write_header

Break: (PR 38 review) `client.ts` renames the header to `X-Hat-Write`.

```
E   assert 'export const WRITE_HEADER = "X-HAT-Request";' in 'import createClient from "openapi-fetch";...'
```

### CONF hat_routes_read_only

Break: a `POST /api/hat/events/purge` route that commits, added to `dashboard.py`.

```
E   AssertionError: assert {('POST', '/a...vents/purge')} == {('POST', '/a...fyers/token')}
E     Extra items in the left set:
E     ('POST', '/api/hat/events/purge')
```

Break: `_all_bots` (reached from `GET /api/hat/status`) calls `session.flush()`.

```
E   AssertionError: ('app.hat.api.dashboard', '/api/hat/status')
E   assert ['app.hat.api...ts: .flush()'] == []
E     Left contains one more item: 'app.hat.api.dashboard._all_bots: .flush()'
```

### PBT ops.retry_after_failed_run

Break: the retry is stamped from the start of the failed run (`ended = started + ONE_DAY if succeeded else started`).

```
E   assert (1 == 1) is (9 >= 10)
```

### PBT ops.chain_carries_past_midnight

Break: no carry-over (`elif not carries_over(job, day, now):` to `elif True:`).

```
E   AssertionError: assert [] == [('next', datetime.date(2026, 10, 20))]
```

Break: (PR 33 review) `carry` ignored (`(bool(job.after) or job.carry)` to `bool(job.after)` in `carries_over`).

```
E   AssertionError: assert [] == [('next', dat...026, 10, 20))]
E     Right contains one more item: ('next', datetime.date(2026, 10, 20))
E   Failing test case: test_chain_carries_past_midnight(
```

### PBT ops.stop_names_abandoned_jobs

Break: `stop` names nothing (`abandoned: list[str] = []` kept empty).

```
E   AssertionError: assert [] == ['job0']
```

### PBT ops.stop_after_failed_start

Break: `_stop_feed` calls each step without the getattr guard or the try (`getattr(getattr(self, name), step)()`).

```
E   assert 0 == 1
E    +  where 0 = <test_ops._StopFeed object at 0x...>.stopped
E   Failing test case: test_stop_after_failed_start(
E       built=1,
```

### PBT ops.daily_tick_matches_reference

Break: the month-start close reads one session too many (the close's `FILTER (WHERE date < ?)` to `<= ?`).

```
E   AssertionError: assert Decimal('0.01') == Decimal('0.05')
E    +  where Decimal('0.01') = tick(UUID('00000000-0000-0000-0000-000000000001'))
```

Break: each date's close from its oldest ingest run (`ORDER BY ingest_run_id DESC` to `ASC`).

```
E   AssertionError: assert Decimal('0.01') == Decimal('0.05')
E    +  where Decimal('0.01') = tick(UUID('00000000-0000-0000-0000-000000000001'))
```

### PBT ops.ex_date_job_behind_flag

Break: (PR 47) in `backend/app/hat/settings.py`, `exdate_gtt_check` ignores `HAT_EXDATE_GTT_CHECK`.

```
E   AssertionError: assert False == (('1' is not None and '1' in frozenset({'1', 'on', 'true', 'yes'})))
```

Break: (PR 47) in `backend/app/hat/ops/trade_jobs.py`, `gtt.ex_date` is registered whatever `exdate_check` says.

```
======================= 1 failed, 20 deselected in 2.19s =======================
```

### PBT ops.subscription_limit_alerts

Break: (PR 73) the alert dates itself by the UTC date (`trade_date_of(now())` became `now().date()`).

```
E   AssertionError: assert [('subscripti...6, 1, 1), {})] == [('subscripti...6, 1, 2), {})]
```

Break: (PR 73, review) the alert is sent on the syncing thread (`ALERT_POOL.submit(send, code, ...)` became `send(code, ...)`), which holds the subscriptions' lock while the transport blocks.

```
E   assert <_MainThread(MainThread, started ...)> not in [<_MainThread(MainThread, started ...)>]
```

### CONF ops.subscription_alert_wired

Break: (PR 73) the runtime builds `LiveSubscriptions(registry, subscribe, unsubscribe)` without its alert (as before PR 73).

```
E   AssertionError: assert {'hat/daily/r...me.py': [332]} == {}
```

Break: (PR 73, CodeAnt) the runtime passes `None` as the alert (the first version accepted any fourth argument).

```
E   AssertionError: assert {'hat/daily/r...me.py': [346]} == {}
```
