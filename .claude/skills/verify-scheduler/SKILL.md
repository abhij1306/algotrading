---
name: verify-scheduler
description: How to run, read and update the checks behind Hire a Trader's job scheduler, job-run recording and the verification harness itself (TLA+ JobScheduler, ops properties, job_run uniqueness, CONF checks of the TLA toolchain, Hypothesis profile, CI job and verify skills). Use before and after any change to backend/app/hat/ops, hat_job_run, verification/tla, the CI verification job or a verify skill.
---

# verify-scheduler

Checks that a scheduled job runs to success at most once per (job, trade_date), that a failed or interrupted run can be retried, that nothing starts while HAT is disabled, and that the verification machinery (TLC runner, Hypothesis profile, CI job, skills) is itself intact. Specs: `docs/specs/hat/10-ops.md` §2, `03-schema.md` §7 (HAT-SCH-072), `11-verification.md` (HAT-VER-030..034, 040, 050, 060, 070..072).

## Properties

- `TLA JobScheduler!AtMostOnceSuccess` One body is running at a time and at most one success is ever recorded for a key, even when callers race, retry or restart. Harm: a nightly job such as ingest or allocation runs twice and double-counts rows or orders.
- `TLA JobScheduler!FailedJobRetryable` A call that finds a failed or interrupted run is allowed to run it again. Harm: one transient failure leaves the day's job dead until someone edits the database.
- `TLA JobScheduler!NoJobWhenDisabled` With `HAT_ENABLED` false the scheduler never exists and no job is ever started. Harm: a developer machine or a test run starts scheduled HAT jobs by accident.
- `TLA JobScheduler!NoStuckRunning` A run never stays `running` forever: a crash is reaped at the next start. Harm: a crashed run blocks its key and the job never runs again that day.
- `PBT ops.job_run_recording` On the real `run_job`, random calls, body failures, simulated crashes and restarts always leave one row per key, with a stable error code (never raw exception text), correct timestamps, and at most one successful body per key. Harm: an audit row lies about what ran, or an error message leaks a secret.
- `PBT ops.disabled_starts_nothing` With HAT disabled `start_scheduler` creates no task and touches no logging or database, and with it enabled on a non-Postgres dialect it raises before any task exists. Harm: HAT runs without the database triggers that protect it.
- `PBT ops.log_records_structured` Every record under `app.hat` renders as one JSON line with `ts`, `level`, `msg` and each context key given. Harm: logs cannot be parsed during an incident.
- `DB ops.job_run_unique` The database rejects a second `hat_job_run` row for one (job, trade_date), including under concurrent inserts. Harm: idempotency rests on application code alone and a race runs a job twice.
- `CONF verification harness` The TLC runner is pinned by version and sha256 and refuses a mismatching jar; every model has a cfg, stated bounds and assumptions, a code location per action and a broken variant per property; the `ci` Hypothesis profile is registered and loaded; the CI `verification` job runs everything inside 15 minutes; every DB check has a broken variant; every spec requirement is well formed; verify skills are complete and current. Harm: a check silently stops running or stops being able to fail.

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
- `verification/tla/broken/JobScheduler__retry_after_success.cfg`, `verification/tla/broken/JobScheduler__split_claim.cfg`, `verification/tla/broken/JobScheduler__no_retry.cfg`, `verification/tla/broken/JobScheduler__ignore_enabled.cfg`, `verification/tla/broken/JobScheduler__no_reap.cfg`
- `verification/tla/run.sh`

Code under check:
- `backend/app/hat/ops/jobs.py`: `run_job`, `_claim`, `_finish`, `recover_interrupted`, `error_code`
- `backend/app/hat/ops/scheduler.py`: `start_scheduler`, `HatScheduler.tick`
- `backend/app/hat/logs.py`: `JsonFormatter`
- `backend/app/hat/models/ops.py`: `HatJobRun`

Checks:
- `backend/tests/hat/properties/test_ops.py`: `test_job_run_recording`, `test_disabled_starts_nothing`, `test_enabled_refuses_a_non_postgres_database`, `test_log_records_structured`, `test_job_error_codes_are_stable_codes`
- `backend/tests/hat/db/test_db_portfolio_ops.py`: `test_ops__job_run_unique`
- `backend/tests/hat/db/hat_db_rules.py`: `check_ops__job_run_unique`
- `backend/tests/hat/db/test_db_broken_variants.py`
- `backend/tests/hat/conformance/test_conf_verification.py`
- `backend/tests/hat/conformance/hat_conf_skills.py`
- `backend/tests/hat/conformance/hat_conf_spec.py`
- `backend/tests/hat/conftest.py`
- `.github/workflows/ci.yml`: job `verification`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `8d86a8a1ee33f40df26acaf45f509c5d7ef0e347`
Covered digest: `95ee54ae8578a63f7b4f68a8c210e3cbfa795d0060781aba97957b6b09d14f34`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `ops.job_run_recording` runs 60 state-machine examples of 40 steps rather than the profile's 500 examples: each step is a database transaction on a real SQLite engine, and 500 sequences would exceed the 15-minute job budget. The invariants are not weakened.
- `JobScheduler` models one key (job, trade_date) because keys are independent rows under one unique index; two callers and three calls each stand for "any number" of racers (bounds in the model header).
- The model counts recorded successes, not body executions: a crash between the body and its recording is the `Restart` action and re-runs the body (see "Bug or model error", known window).
- `TypeOK` has no broken variant: it is a type-correctness invariant, not a promise.
- PR 3 updated the harness files this skill covers, without narrowing any check: `MERGED_PRS` now includes 3; `KNOWN_GAPS` gained two TLA names that belong to PR 13 and PR 15 (see `verify-events`); the DB-check coverage test accepts `events.partitions_ahead` through its own broken-ensure variants; the catalog comparison orders by name and definition.

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
