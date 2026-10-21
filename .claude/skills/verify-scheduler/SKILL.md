---
name: verify-scheduler
description: How to run, read and update the checks behind Hire a Trader's job scheduler, job-run recording and the verification harness itself (TLA+ JobScheduler, ops properties, job_run uniqueness, CONF checks of the TLA toolchain, Hypothesis profile, CI job and verify skills). Use before and after any change to backend/app/hat/ops, hat_job_run, verification/tla, the CI verification job or a verify skill.
---

# verify-scheduler

Checks that a scheduled job runs to success at most once per (job, trade_date), that a failed or interrupted run can be retried, that nothing starts while HAT is disabled, that the windowed `exits.close` job runs only inside 15:20-15:25 IST and sends nothing from 15:30 on, that the 15:25 live price of each MA exit is kept next to the official close, and that the verification machinery (TLC runner, Hypothesis profile, CI job, skills) is itself intact. Specs: `docs/specs/hat/10-ops.md` §2, §3 (HAT-OPS-034), `07-execution-and-brokers.md` §7 (HAT-EXEC-074, HAT-DEC-B18), `03-schema.md` §7 (HAT-SCH-072), `11-verification.md` (HAT-VER-030..034, 040, 050, 060, 070..072).

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
- `TLA NightlyPipeline!NightlyPipelineOrder` The steps run in order ingest, gate, then the later steps, each only after the one before succeeded. Harm: a scan reads a day not yet ingested or gated.
- `TLA NightlyPipeline!IngestDecides` By 22:00 the ingest has either its data or a failure, never neither (a lost fallback leaves the pipeline waiting forever). Harm: the nightly job hangs and nobody is told.
- `PBT ops.job_run_recording` On the real `run_job`, random calls, body failures, simulated crashes and restarts always leave one row per key, with a stable error code (never raw exception text), correct timestamps, and at most one successful body per key. Harm: an audit row lies about what ran, or an error message leaks a secret.
- `PBT ops.disabled_starts_nothing` With HAT disabled `start_scheduler` creates no task and touches no logging or database, and with it enabled on a non-Postgres dialect it raises before any task exists. Harm: HAT runs without the database triggers that protect it.
- `PBT ops.close_gap_recorded` Every `close_below_sma` evaluation of the close window records the live price it used, fired or not; once the nightly ingest has the official close, exactly one `exit.close_gap` per position records that close and its gap, against the same position; a close still missing is recorded by a later run and a repeated run changes nothing. Harm: the backtest/live gap of MA exits cannot be measured.
- `PBT ops.log_records_structured` Every record under `app.hat` renders as one JSON line with `ts`, `level`, `msg` and each context key given. Harm: logs cannot be parsed during an incident.
- `DB ops.job_run_unique` The database rejects a second `hat_job_run` row for one (job, trade_date), including under concurrent inserts. Harm: idempotency rests on application code alone and a race runs a job twice.
- `CONF verification harness` The TLC runner is pinned by version and sha256 and refuses a mismatching jar; every model has a cfg, stated bounds and assumptions, a code location per action and a broken variant per property; the `ci` Hypothesis profile is registered and loaded; the CI verification jobs (`verification-tla`, `verification-conformance`, sharded `verification-properties`) run everything, each inside the 5-minute target; every DB check has a broken variant; every spec requirement is well formed; verify skills are complete and current. Harm: a check silently stops running or stops being able to fail.

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
- A new model: needs a cfg, header, code comments and broken variants, and a row in `11-verification.md` §3.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-scheduler` prints the digest.

## Coverage

TLA+ model and runner:
- `verification/tla/JobScheduler.tla` and `verification/tla/JobScheduler.cfg`
- `verification/tla/broken/JobScheduler__retry_after_success.cfg`, `verification/tla/broken/JobScheduler__split_claim.cfg`, `verification/tla/broken/JobScheduler__no_retry.cfg`, `verification/tla/broken/JobScheduler__ignore_enabled.cfg`, `verification/tla/broken/JobScheduler__no_reap.cfg`, `verification/tla/broken/JobScheduler__no_window.cfg`, `verification/tla/broken/JobScheduler__late_send.cfg`
- `verification/tla/NightlyPipeline.tla` and `verification/tla/NightlyPipeline.cfg`
- `verification/tla/broken/NightlyPipeline__probe_twice.cfg`, `verification/tla/broken/NightlyPipeline__probe_at_cutoff.cfg`, `verification/tla/broken/NightlyPipeline__fetch_without_match.cfg`, `verification/tla/broken/NightlyPipeline__fallback_always.cfg`, `verification/tla/broken/NightlyPipeline__nodata_no_kill.cfg`, `verification/tla/broken/NightlyPipeline__nodata_no_alert.cfg`, `verification/tla/broken/NightlyPipeline__scan_ignores_gate.cfg`, `verification/tla/broken/NightlyPipeline__gate_fail_no_kill.cfg`, `verification/tla/broken/NightlyPipeline__exit_needs_pipeline.cfg`, `verification/tla/broken/NightlyPipeline__skip_order.cfg`, `verification/tla/broken/NightlyPipeline__no_fallback.cfg`
- `verification/tla/run.sh`

Code under check:
- `backend/app/hat/ops/jobs.py`: `run_job`, `_claim`, `_finish`, `recover_interrupted`, `error_code`
- `backend/app/hat/ops/scheduler.py`: `start_scheduler`, `HatScheduler.tick`, `HatScheduler.due_jobs` (the `until` window and `retry_after`)
- `backend/app/hat/ops/trade_jobs.py`: `trade_jobs` (`exits.close`, `gtt.swap`, `gtt.verify`)
- `backend/app/hat/execution/manage.py`: `TradeManager.close_exits`, `TradeManager._sell` (the 15:30 check at sending)
- `backend/app/hat/engine/live_trades.py`: `record_close_evaluation`, `record_close_gaps`
- `backend/app/hat/logs.py`: `JsonFormatter`
- `backend/app/hat/models/ops.py`: `HatJobRun`

Checks:
- `backend/tests/hat/properties/test_ops.py`: `test_job_run_recording`, `test_disabled_starts_nothing`, `test_enabled_refuses_a_non_postgres_database`, `test_log_records_structured`, `test_job_error_codes_are_stable_codes`, `test_close_gap_recorded`
- `backend/tests/hat/db/test_db_portfolio_ops.py`: `test_ops__job_run_unique`
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
Covered digest: `5a09b527095155af0ec4167657db50a5076c47446beae523ad6aad752f067ddf`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `ops.job_run_recording` runs 60 state-machine examples of 40 steps rather than the profile's 500 examples: each step is a database transaction on a real SQLite engine, and 500 sequences would exceed the 15-minute job budget. The invariants are not weakened.
- `JobScheduler` models one key (job, trade_date) because keys are independent rows under one unique index; two callers and three calls each stand for "any number" of racers (bounds in the model header).
- The model counts recorded successes, not body executions: a crash between the body and its recording is the `Restart` action and re-runs the body (see "Bug or model error", known window).
- `TypeOK` has no broken variant: it is a type-correctness invariant, not a promise.
- PR 3 updated the harness files this skill covers, without narrowing any check: `MERGED_PRS` now includes 3; `KNOWN_GAPS` gained two TLA names that belong to PR 13 and PR 15 (see `verify-events`); the DB-check coverage test accepts `events.partitions_ahead` through its own broken-ensure variants; the catalog comparison orders by name and definition.
- PR 13 updated the harness files this skill covers, without narrowing any check: `MERGED_PRS` includes 13; `KNOWN_GAPS` lost `TLA OrderReconciliation!AtMostOneEntryPerSignal` and gained `TLA ExecutionRouter!AutoNeedsGtt` (see `verify-execution`); `test_ops.py` gained `test_session_backoff_alert` and `test_backoff_delay_is_monotone_and_capped`.
- PR 6 updated `test_conf_verification.py`: `MERGED_PRS` now includes 6; `KNOWN_GAPS` unchanged.
- PR 10 updated `test_conf_verification.py` without narrowing any check: `MERGED_PRS` now includes 10, and `KNOWN_GAPS` gained two ExecutionRouter TLA names that belong to PR 15 (see `verify-risk`).
- PR 8 updated `test_conf_verification.py`: `MERGED_PRS` now includes 8; `KNOWN_GAPS` unchanged.

- PR 15c added the `exits.close` part to `JobScheduler` as an independent sub-model (the clock in four spans, one trade date; bounded by `MaxCalls` starts) rather than a model of its own: it shares the scheduler's start gate. It checks the window and the 15:30 sending cut-off, not the minute-level retry interval (`retry_after` is one minute, inside the five-minute window). `MERGED_PRS` now includes 15 and `KNOWN_GAPS` lost `TLA StopPlacement!StopOnlyTightens` (the model is in `verify-stops`). `ops.close_gap_recorded` drives virtual positions (the evaluation record is the same for real ones) with at most three positions per example.

- PR 7 split the nightly-pipeline promises (probe window, fallback at 22:00, no data no scan, gate, exits independent) out of `JobScheduler` into the `NightlyPipeline` model, because the product of the two state spaces was too large to check; `JobScheduler` keeps the job-run promises. The README and spec property names were renamed to match; no check was weakened. PR 7 also added an `on_finish` hook to `run_job` (called inside the final-status transaction; `ops.job_run_recording` still passes) and `MERGED_PRS` now includes 7, `KNOWN_GAPS` unchanged.
- PR 15b updated the harness without narrowing any check: `KNOWN_GAPS` lost `TLA ExecutionRouter!AutoNeedsGtt`, `TLA ExecutionRouter!NoAddToOpenPosition` and `TLA ExecutionRouter!ModeChangeNextSession` (the `ExecutionRouter` model now exists), `MERGED_PRS` is unchanged (15c adds 15), and two models joined `verification/tla` (`ExecutionRouter`, `ApprovalToken`; their checks and breaks are in `verify-execution`). The new property file `test_router.py` is placed by `ci_property_shards.py`'s partition like any other.

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

Break: `start_scheduler` stops checking `settings.enabled`.

```
FAILED tests/hat/properties/test_ops.py::test_disabled_starts_nothing - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

### PBT ops.log_records_structured

Break: `JsonFormatter.format` pretty-prints with `indent=1`.

```
tests/hat/properties/test_ops.py:127: in test_log_records_structured
E   assert '\n' not in '{\n "ts": "..."msg": ""\n}'
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
State 5: <LaterStep ...>
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
State 5: <LaterStep ...>
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
