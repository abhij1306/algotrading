---
name: verify-portfolio
description: How to run, read and update the checks behind Hire a Trader's portfolio layer (universe versions and point-in-time membership, the regime engine and its 3-day rule, the health multiplier, the nightly allocator and the sleeve lifecycle with its guards). Use before and after any change to backend/app/hat/engine/portfolio, verification/tla/RegimeConfirmation.tla, verification/tla/SleeveLifecycle.tla or the portfolio properties and conformance checks.
---

# verify-portfolio

Checks that the portfolio layer (`backend/app/hat/engine/portfolio/`) decides what each sleeve may do and with how much capital, and that no step of it can be skipped: a universe is a point-in-time set that never rewrites history, the regime state changes only after three consecutive days and never touches a sleeve's lifecycle, the health multiplier follows the drawdown bands, the allocator keeps every multiplier inside the policy bounds and every cap, and a sleeve reaches Live only through the guarded lifecycle with an event for every change. Specs: `docs/specs/hat/05-portfolio-regime-allocation.md` (HAT-PORT-010 to 066), `01-decisions.md` (PL1 to PL5), `03-schema.md` (HAT-SCH-042/083), `06-risk-and-governance.md` (HAT-RISK-066), `11-verification.md` section 3.

## Properties

- `TLA RegimeConfirmation!ThreeDayRule` The confirmed regime state changes to a state only on the third consecutive day the raw classification equals it (and `ConfirmsAfterThree`: three equal days always confirm). Harm: one wild day moves capital in or out of the market, or a real change is never acknowledged.
- `TLA SleeveLifecycle!AllowedTransitions` Every lifecycle change is one of the seven allowed edges, its guard held before it (backtest gate, 20 shadow sessions, probation trigger, the owner's promotion with the failure gone, retirement after the period with the failure still true) and it comes with exactly one event of the same (from, to). Harm: a sleeve skips a stage, or a change leaves no audit trail.
- `TLA SleeveLifecycle!NoLiveWithoutEvidence` A Live sleeve has been through Backtested and Shadow since it last left Research. Harm: a retired or unproven sleeve returns to money without the full gate.
- `TLA SleeveLifecycle!RegimeNeverChangesLifecycle` A regime change never changes a sleeve's lifecycle state. Harm: a bear market retires a sound sleeve, or a bull market promotes an unproven one.
- `TLA SleeveLifecycle!ProbationOnHealthZero` After an allocation no Live sleeve has health 0, and every sleeve out of Live has weight 0. Harm: a broken sleeve keeps trading real money.
- `PBT universe.*` On the real membership rules: the members on a date are exactly the instruments with an interval holding it (delisted ones included for the dates they were in); an edit never changes the membership of an earlier date; the universe is (constituents plus includes) minus excludes; intervals stay ordered, non empty and non overlapping; leaving the universe blocks entries and never forces an exit; a seeded version is a draft that changes no membership until it is published. Harm: a backtest sees today's survivors, an edit rewrites history, or a universe change sells positions the strategy did not call for.
- `PBT regime.*` The confirmation matches an independent model for any length; the inputs read nothing dated after the day, the VIX percentile is in [0, 100) and monotone, a missing or unusable input fails with a stable code; the classification is total, follows the precedence Bear, Correction, Bull volatile, Bull quiet and never moves to a safer state when conditions worsen; the multiplier table never rises toward Bear and a replacement table is held within the policy bounds; the `regime` job stores one row per day with the raw state, the confirmed state and one event. Harm: the regime reads the future, overlapping rows give different states by code order, or worse conditions raise exposure.
- `PBT health.*` The multiplier is the full value under the first band, the half value from it to the second band and 0 (Probation) from the second band on, compared exactly at the edges, never rises with a deeper drawdown, and an unreadable equity pair fails instead of reading as healthy. Harm: a sleeve in a deep drawdown keeps full capital.
- `PBT allocator.*` On the real allocator and the nightly service on SQLite: weight = base x regime x health with both multipliers inside the bounds of the policy version in force at the trade date; a sleeve out of Live has weight 0; a weight never exceeds that product and equals it when no cap binds; capital = weight x equity rounded down and risk per trade = policy risk x regime x health; a Shadow or Probation sleeve keeps weight 0 with a nominal `virtual_capital` (base x regime x equity) and `virtual_risk_pct` (risk x regime), and through `run_allocation` and `LiveContexts` its virtual entries are accepted on it while a real one is `no_allocation` (`allocator.virtual_allocation`, `allocator.virtual_entries_sized`, PR 34); a multiplier moves at most 0.25 a day and a sleeve back from health 0 restarts at its target; every risk cluster stays within the cluster cap and the book within 1, with nothing redistributed; any number of sleeves in any order gives one row each; the allocation fails with no regime row and a rerun keeps the first rows; open positions are untouched. Harm: a sleeve gets more weight or risk than the policy allows, capital swings in one night, or a weight cut sells positions.
- `PBT lifecycle.*` On the real guards and `transition`: the backtest gate needs DSR probability of at least 0.95 and 200 trades; the shadow gate needs 20 trading sessions after the start; the Student-t quantile is checked against a numerical integration of the t density; the probation trigger is health 0 or an expectancy interval below zero from 50 trades; the benchmark is the backtest expectancy times one minus the discount; retirement needs 3 months or 50 shadow trades with the failure still true; every guard answer equals an independently written rule table; a transition changes the state and writes its event together or does neither; the results a decision reads stop at the trade date's close; the allocation moves a health-zero sleeve to Probation. Harm: an unproven sleeve goes live, a sound one is benched on noise, or the state and its audit trail disagree.
- `CONF portfolio.*` Only `service.transition` assigns `hat_bot.lifecycle_state` (by attribute, `setattr` or a bulk update); the regime, health, allocator, lifecycle and universe rules import no database, model, broker or data code; `hat_bot` pins a strategy version and a universe version, carries `lifecycle_state` with exactly the six lifecycle values and `risk_cluster`, and has no risk-number column. Harm: a second writer skips the lifecycle guard, a rule reads the database and the backtest applies a different rule, or a risk number bypasses the risk policy.
- `PBT admin.*` Through the owner's admin command line (`python -m app.hat.admin`, PR 35) on SQLite with a fake broker and scripted backtest evidence: the first-deploy runbook takes a fresh database to a sleeve in Shadow that has a strategy version and a live universe, refuses `promote --to live` until the version itself is live, promotes the version only after 20 trading sessions of shadow, and records the owner's steps (`confirm-ddpi` among them, PR 45) as `actor = user` with the allowlisted chat id and `via = cli` (`admin.first_deploy`); every refused command exits 2 with the fixed code of the rule that refused it and leaves every table unchanged, a universe publish naming an unknown sleeve included, as do a universe or draft inside a session, a CSV ISIN that differs from the instrument's, sleeves on an account that is not active, an undecodable file, a file outside `<HAT_DATA_ROOT>/admin/`, and a `confirm-ddpi` inside a session, dated in the future, with a blank note, repeated or naming an unknown account (`admin.refusal_writes_nothing`). Harm: a fresh deploy never scans, a sleeve trades real money on an unproven version, or a refused release step is half applied.
- `CONF ops.admin_cli_calls_services` `backend/app/hat/admin.py` imports only the services it wraps (and what they need), calls no session method, builds no query and assigns no attribute; no module imports it, so no route reaches it (HAT-DEC-T3). Harm: a setup command skips the guard a service enforces, or the owner's commands become an unauthenticated API.
- `PBT regime.strategy_rules` `close_above_sma_and_rising` is true exactly when the close is above its SMA and the SMA is higher than `rising_days` sessions earlier; `sma_fast_above_slow_both_rising` exactly when SMA fast is above SMA slow and both are above their value `rising_days` sessions earlier; a configured fast/slow rule without `rising_days` reads 3 sessions (PR 55, owner default), one with it reads that many, and each gives what the function gives; a too-short series is false. Harm: the gate lets entries through in a falling market or blocks them in a rising one.
- `PBT dsr.evidence_current_trials` The backtest evidence the lifecycle reads is the newest completed run of the sleeve's live version on its family, with the DSR deflated by the family's trial count and Sharpe variance at evaluation time (later failed runs and other versions counted), as `BacktestFacts`; no run is no evidence (HAT-BT-060). Harm: a sleeve passes the Research to Backtested gate on a stale trial count.

## Run

```bash
# Java 17+ is needed (Temurin 21 is what CI uses): export JAVA_HOME=/path/to/jdk
verification/tla/run.sh RegimeConfirmation              # the model must pass
verification/tla/run.sh --broken RegimeConfirmation     # every broken variant must fail with its EXPECT line
verification/tla/run.sh SleeveLifecycle
verification/tla/run.sh --broken SleeveLifecycle
cd backend
CI=1 python -m pytest tests/hat/properties/test_universe.py tests/hat/properties/test_regime.py \
  tests/hat/properties/test_health.py tests/hat/properties/test_allocator.py \
  tests/hat/properties/test_lifecycle.py tests/hat/properties/test_admin.py -q --no-cov
python -m pytest tests/hat/conformance -q --no-cov              # CONF portfolio.*, ops.admin_cli_calls_services and the harness
```

## Bug or model error

- A failing property prints the falsifying example (for the allocator, the sleeve inputs and the policy; for the regime, the raw state sequence or the series; for the lifecycle, the two states and the evidence). Reproduce it with the real function. If the real code breaks the property, that is a finding: report the counterexample in the PR and fix the code. Never weaken the property or narrow its strategy to get past it.
- A TLC counterexample names each action (`ToLive`, `Allocate`, `RegimeChange`, `Promote`, `Retire`, the environment steps); each action has a `\* code: backend/app/hat/engine/portfolio/...py:function` comment naming the function it models. If the real function can take those steps in that order, it is a bug in the code. If it cannot (a guard or transaction the model leaves out), fix the model and say so under Scope changes.
- The model checks the structure of the rules with scaled thresholds (2 shadow sessions, 2 probation periods); the real thresholds (20 sessions, 3 months or 50 trades) are proved by `lifecycle.shadow_days` and `lifecycle.retire_gate`. A thresholds change needs both.
- `CONF portfolio.lifecycle_single_writer` names the file and line of a second assignment of `lifecycle_state`: move it into `service.transition` (with its guard and event) rather than adding an exception.
- Finding kept visible: the planned-reallocation fix (HAT-RISK-066) lives in `risk/kill.py` and `risk/controls.py`, covered by `verify-risk`; the allocator's capital cuts reach the kill only through the restated peak.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new sleeve: a `hat_bot` row and a `base_weight` entry in the policy; no code (the strategy name maps to its key through `SLEEVE_OF_STRATEGY`, an unknown name is its own key). `allocator.sleeve_count_agnostic` proves any number of rows works.
- A lifecycle rule: edit `lifecycle.py` (the guard and `EDGES`), `service.py` (the one writer) and `verification/tla/SleeveLifecycle.tla` together; keep a `\* code:` comment on every action, add a broken variant `SleeveLifecycle__<mode>.cfg` (with `EXPECT:` and the mode in the model's `ASSUME`), run both `run.sh` commands, then the properties.
- A regime rule or threshold: edit `regime.py`, `RegimeConfirmation.tla` (if the 3-day rule changes), the 05 section 3 text and `rules_v1_config`; the stored model version holds the multiplier table, so a new table is a new `hat_regime_model_version` row.
- Backtest evidence plugs in through `BacktestEvidence` in `service.py`, now returning `lifecycle.BacktestFacts` (DSR, trades, expectancy, trial count). The default `no_backtest_evidence` keeps Research to Backtested closed; the backtest's source is `app.hat.backtest.runs.backtest_evidence` (PR 12, `verify-backtest`), which `allocate_job` passes to `run_allocation` by default.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-portfolio` prints the digest.

## Coverage

TLA+ models and broken variants:
- `verification/tla/RegimeConfirmation.tla` and `verification/tla/RegimeConfirmation.cfg`
- `verification/tla/broken/RegimeConfirmation__immediate.cfg`, `verification/tla/broken/RegimeConfirmation__two_days.cfg`, `verification/tla/broken/RegimeConfirmation__run_not_reset.cfg`, `verification/tla/broken/RegimeConfirmation__never_confirm.cfg`
- `verification/tla/SleeveLifecycle.tla` and `verification/tla/SleeveLifecycle.cfg`
- `verification/tla/broken/SleeveLifecycle__regime_retires.cfg`, `verification/tla/broken/SleeveLifecycle__skip_shadow.cfg`, `verification/tla/broken/SleeveLifecycle__no_dsr_guard.cfg`, `verification/tla/broken/SleeveLifecycle__short_shadow.cfg`, `verification/tla/broken/SleeveLifecycle__early_retire.cfg`, `verification/tla/broken/SleeveLifecycle__retired_to_live.cfg`, `verification/tla/broken/SleeveLifecycle__promote_while_failing.cfg`, `verification/tla/broken/SleeveLifecycle__auto_promote.cfg`, `verification/tla/broken/SleeveLifecycle__no_event.cfg`, `verification/tla/broken/SleeveLifecycle__no_probation_move.cfg`

Code under check:
- `backend/app/hat/engine/portfolio/regime.py`: `compute_inputs`, `classify`, `confirm_step`, `multiplier_for`, `derive_multipliers`
- `backend/app/hat/engine/portfolio/health.py`: `health_of`
- `backend/app/hat/engine/portfolio/allocator.py`: `allocate`, `smooth`
- `backend/app/hat/engine/portfolio/lifecycle.py`: `guard`, `backtest_gate`, `shadow_gate`, `t_quantile_95`, `probation_trigger`, `discounted_benchmark`, `retire_gate`
- `backend/app/hat/engine/portfolio/universe.py`: `members_on`, `add_member`, `remove_member`, `effective_members`, `membership_change`
- `backend/app/hat/engine/portfolio/service.py`: `transition`, `owner_transition`, `advance_automatic`, `run_allocation`, `record_regime`, `policy_at`, `universe_named`, `resolve_symbols`, `assign_universe`
- `backend/app/hat/admin.py`: `main` and every command
- `backend/app/hat/engine/portfolio/jobs.py`: `regime_job`, `allocate_job`

- `backend/app/hat/backtest/runs.py`: `backtest_evidence`

Checks:
- `backend/tests/hat/hat_portfolio_support.py`
- `backend/tests/hat/properties/test_universe.py`
- `backend/tests/hat/properties/test_regime.py`
- `backend/tests/hat/properties/test_health.py`
- `backend/tests/hat/properties/test_allocator.py`
- `backend/tests/hat/properties/test_lifecycle.py`
- `backend/tests/hat/properties/test_dsr.py`
- `backend/tests/hat/conformance/test_conf_portfolio.py`
- `backend/tests/hat/properties/test_admin.py`
- `backend/tests/hat/conformance/test_conf_ops.py`
- `backend/app/hat/engine/regime_gate.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `9a6ef4da99c3a8201c6bb498cb4a50fb9370c546daf165a97f4d01bbd83def41`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 64: `backtest/runs.py:backtest_bot` takes its entry fill rule and slot order, and the new `ambiguous` (default `optimistic`) and `suspension_sessions` (default 5, HAT-BT-021), as one `RunConventions`, and passes them to `check_conventions`, which refuses an unknown rule (`backtest_bad_ambiguous_rule`) or a count outside 0..250 (`backtest_bad_suspension`) before the run is recorded, and a promotion run with a count above 5 (`backtest_promotion_late_suspension`). The lifecycle's evidence (`backtest_evidence`, `gate_evidence`) reads only promotion runs whose metrics carry the path rules (`lower_circuit`), so a sleeve whose only promotion runs predate PR 64 has no evidence until a new `--for-promotion` run; the ambiguous-day rule is not required (PR 69 caps the share). Properties `backtest.path_rules_refused` and `dsr.evidence_promoted_only` in `verify-backtest`.
- PR 63: `backtest/runs.py:backtest_bot` takes `slot_order` (default `time`, HAT-BT-026) and refuses an unknown one (`backtest_bad_slot_order`) before the run is recorded and a promotion run off `time` (`backtest_promotion_off_time_order`). The lifecycle's evidence (`backtest_evidence`, `gate_evidence`) reads only promotion runs whose `slot_order` is `time`, so a sleeve whose only promotion runs predate PR 63 has no evidence until a new `--for-promotion` run. Properties `backtest.slot_order_refused` and `dsr.evidence_promoted_only` in `verify-backtest`. `test_admin.add_run` records `slot_order: time`, which `publish-version` now requires.
- PR 62: `backtest/runs.py:backtest_bot` takes `entry_fill_rule` (default `penetration_1_tick`) and `for_promotion`, defaults the slippage to 20 bps (`costs.DEFAULT_SLIPPAGE_BPS`), and refuses an unknown rule (`backtest_bad_fill_rule`) or a promotion run at 0 bps or under `touch` (`backtest_promotion_without_slippage`, `backtest_promotion_on_touch`) before the run is recorded. The lifecycle's evidence (`backtest_evidence`, and `gate_evidence`) now reads only runs flagged for promotion, so a sleeve whose only runs predate PR 62 has no evidence (the Research -> Backtested guard stays closed) until a `--for-promotion` run; property `dsr.evidence_promoted_only` in `verify-backtest`. The admin runbook property (`test_admin.add_run`) records its scripted run with `for_promotion` set, since `publish-version` now refuses an unflagged run (`run_not_for_promotion`).
- PR 58: `backtest/runs.py:backtest_bot` takes `cooldown_sessions` (the cooldown sweep, HAT-RISK-036) and refuses a bad one with `backtest_bad_cooldown` before the run is recorded; the lifecycle's evidence (`backtest_evidence`) skips any run not made under the cooldown in force (PBT `dsr.evidence_cooldown_in_force`, verify-backtest).
- PR 55 (plan audit N7, §6 item 10): the fast/slow regime rule's `rising_days` defaults to 3 (was an `[OWNER-PENDING]` 1) and `sma_fast_above_slow_both_rising` takes it without a default. A stored version keeps the value it was saved with (the config stores every parameter), so an S2 version saved before PR 55 keeps 1 until a new version is published. The S2 seed and its spec YAML state `rising_days: 3`, and the config generator (`hat_config_support.REGIMES`) draws it or leaves it out. Nothing was narrowed; `regime.strategy_rules` now draws the lookback for both rules.

- `SleeveLifecycle` models one sleeve and scales the thresholds (2 shadow sessions, 2 probation periods): the real thresholds are properties of the pure guards (`lifecycle.shadow_days`, `lifecycle.retire_gate`), not of the state machine. The three evidence flags (backtest gate, health 0 and expectancy trigger, probation failure) are free environment inputs.
- `AllocateAfterRegime` (HAT-PORT-053) and the job-graph order belong to PR 17 (`JobScheduler`); until then `allocate` fails with `allocate_no_regime` when the regime row of the date is absent.
- The allocator's first-night rule: a sleeve with no earlier allocation row starts at its target multiplier; the 0.25 daily step applies from the second night.
- The `regime` job reads the NIFTY 500 close, India VIX and the stored breadth percentage. The advance/decline ratio is recorded as `null` until the ingest provides it (PR 7) and is not an input of the v1 classification.
- Backtest evidence is a pluggable source (`BacktestEvidence`, default none). PR 12 supplies `runs.backtest_evidence`, whose DSR is recomputed at the family's current trial count (`dsr.evidence_current_trials`, below and in `verify-backtest`); the gate itself (`lifecycle.backtest_gate`) is fully checked.
- The property `lifecycle.transition_records_its_event` and the nightly `run_allocation` properties run on SQLite in memory (foreign keys not enforced); `portfolio.unique_keys` covers the constraints on Postgres.
- PR 15b: `service.trade_results` leaves out veto trades (virtual positions of confirm or auto signals), so a sleeve's shadow evidence is its shadow trades only (HAT-EXEC-032). The lifecycle, health and allocator properties pass unchanged; none was narrowed.
- PR 34 (design review 2026-10-22 item 8a, HAT-PORT-050): `hat_allocation_daily` gains `virtual_capital` and `virtual_risk_pct` (nullable; `ensure_hat_columns` adds them to an existing Postgres database), filled for Shadow and Probation sleeves only. `LiveContexts.entry_context` sizes a virtual entry of such a sleeve from them (health 1), and the virtual global open-risk cap reads the book as the sum of `coalesce(virtual_capital, capital)`. `allocator.virtual_entries_sized` builds the engine world (`seed_engine_world`, strategy renamed to a weighted sleeve), allocates on the previous trading day and decides with `valid_ctx` for the trade's own fields. The Probation deadlock (`still_failing` with no shadow trades) eases now that Probation books virtual trades; the forward lifecycle callers are PR 35. Nothing was narrowed.
- PR 35 (design review 2026-10-22 item 8b, HAT-OPS-091): the forward edges and the setup steps now have a caller, the owner's admin command line. `service.owner_transition` takes an edge under the same guard and evidence as the engine, outside market hours only, records it as the owner's (`actor = user`, the allowlisted chat id, `via = cli`) and refuses Live unless the sleeve's strategy version is itself live (HAT-BOT-084, `config.release_gate` in `verify-criteria`). `admin.*` runs the command line on SQLite: the backtest evidence is scripted (`backtest_evidence` is `dsr.evidence_current_trials`) and the onboarding uses `FyersHatBroker` over `FakeBroker`. `onboard-account` commits its own result (a failed check is recorded as `failed`), so `admin.refusal_writes_nothing` leaves it out. The clock hands out UTC because SQLite keeps no offset. Nothing was narrowed.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model (HAT-VER-072), then the break was removed and the check returned to passing.

### TLA RegimeConfirmation!ThreeDayRule

Break: `verification/tla/broken/RegimeConfirmation__immediate.cfg` selects the variant where the confirmed state follows the raw state at once.

```
Error: Invariant ThreeDayRule is violated.
State 1: <Initial predicate>
State 2: <Bootstrap line 57>
State 3: <Switch line 76>
```

Break: `verification/tla/broken/RegimeConfirmation__two_days.cfg` selects the variant where two days in a row are enough.

```
Error: Invariant ThreeDayRule is violated.
State 1: <Initial predicate>
State 2: <Bootstrap line 57>
State 3: <Switch line 76>
State 4: <Repeat line 66>
```

Break: `verification/tla/broken/RegimeConfirmation__run_not_reset.cfg` selects the variant where a different state in between does not restart the count.

```
Error: Invariant ThreeDayRule is violated.
State 1: <Initial predicate>
State 2: <Bootstrap line 57>
State 3: <Repeat line 66>
State 4: <Switch line 76>
```

Break: `verification/tla/broken/RegimeConfirmation__never_confirm.cfg` selects the variant where a state is never confirmed (the liveness half, `ConfirmsAfterThree`).

```
Error: Invariant ConfirmsAfterThree is violated.
State 1: <Initial predicate>
State 2: <Bootstrap line 57>
State 3: <Switch line 76>
State 4: <Repeat line 66>
State 5: <Repeat line 66>
```

### TLA SleeveLifecycle!AllowedTransitions

Break: `verification/tla/broken/SleeveLifecycle__no_dsr_guard.cfg` selects the variant where Research moves to Backtested without the backtest gate.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <ToBacktested line 110>
```

Break: `verification/tla/broken/SleeveLifecycle__short_shadow.cfg` selects the variant where Shadow moves to Live before enough sessions.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <ToShadow line 116>
State 5: <ToLive line 121>
```

Break: `verification/tla/broken/SleeveLifecycle__early_retire.cfg` selects the variant where Probation retires before its period.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <ToShadow line 116>
State 5: <TickShadow line 208>
State 6: <TickShadow line 208>
State 7: <ToLive line 121>
State 8: <EnvStep line 224>
State 9: <Allocate line 163>
State 10: <Retire line 142>
```

Break: `verification/tla/broken/SleeveLifecycle__retired_to_live.cfg` selects the variant where a Retired sleeve returns straight to Live.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <ToShadow line 116>
State 5: <TickShadow line 208>
State 6: <TickShadow line 208>
State 7: <ToLive line 121>
State 8: <EnvStep line 224>
State 9: <Allocate line 163>
State 10: <TickProbation line 216>
State 11: <TickProbation line 216>
State 12: <Retire line 142>
State 13: <RetiredToLive line 155>
```

Break: `verification/tla/broken/SleeveLifecycle__promote_while_failing.cfg` selects the variant where Probation returns to Live while the failure condition still holds.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <EnvStep line 230>
State 4: <ToBacktested line 110>
State 5: <ToShadow line 116>
State 6: <TickShadow line 208>
State 7: <TickShadow line 208>
State 8: <ToLive line 121>
State 9: <Allocate line 163>
State 10: <Promote line 135>
```

Break: `verification/tla/broken/SleeveLifecycle__auto_promote.cfg` selects the variant where the engine promotes a Probation sleeve without the owner.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 224>
State 3: <EnvStep line 202>
State 4: <EnvStep line 236>
State 5: <ToBacktested line 110>
State 6: <ToShadow line 116>
State 7: <TickShadow line 208>
State 8: <TickShadow line 208>
State 9: <ToLive line 121>
State 10: <Allocate line 163>
State 11: <Promote line 135>
```

Break: `verification/tla/broken/SleeveLifecycle__no_event.cfg` selects the variant where a transition writes no `sleeve.lifecycle_changed` event.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
```

### TLA SleeveLifecycle!NoLiveWithoutEvidence

Break: `verification/tla/broken/SleeveLifecycle__skip_shadow.cfg` selects the variant where a Backtested sleeve goes live without Shadow.

```
Error: Invariant NoLiveWithoutEvidence is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <SkipShadow line 128>
```

### TLA SleeveLifecycle!RegimeNeverChangesLifecycle

Break: `verification/tla/broken/SleeveLifecycle__regime_retires.cfg` selects the variant where a regime change moves a live sleeve out of Live.

```
Error: Invariant RegimeNeverChangesLifecycle is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <ToShadow line 116>
State 5: <TickShadow line 208>
State 6: <TickShadow line 208>
State 7: <ToLive line 121>
State 8: <RegimeChange line 183>
```

### TLA SleeveLifecycle!ProbationOnHealthZero

Break: `verification/tla/broken/SleeveLifecycle__no_probation_move.cfg` selects the variant where the allocation leaves a live sleeve with health 0 live.

```
Error: Invariant ProbationOnHealthZero is violated.
State 1: <Initial predicate>
State 2: <EnvStep line 202>
State 3: <ToBacktested line 110>
State 4: <ToShadow line 116>
State 5: <TickShadow line 208>
State 6: <TickShadow line 208>
State 7: <EnvStep line 224>
State 8: <ToLive line 121>
State 9: <Allocate line 163>
```

### PBT universe.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/universe.py`: interval end is inclusive. Check run: `test_universe.py::test_point_in_time_members`.

```
E   AssertionError: assert frozenset({UU...0000000003')}) == set()
E     
E     Extra items in the left set:
E     UUID('00000000-0000-0000-0000-000000000003')
E     Use -v to get more diff
E   Failing test case: test_point_in_time_members(
E       rows=[Interval(instrument_id=UUID('00000000-0000-0000-0000-000000000003'),
E         effective_from=datetime.date(2026, 1, 1),
E         effective_to=datetime.date(2026, 1, 2))],
FAILED tests/hat/properties/test_universe.py::test_point_in_time_members - As [cut]
```

Break: in `backend/app/hat/engine/portfolio/universe.py`: an include beats an exclude. Check run: `test_universe.py::test_include_exclude`.

```
E   AssertionError: assert frozenset({UU...0000000001')}) == set()
E     
E     Extra items in the left set:
E     UUID('00000000-0000-0000-0000-000000000001')
E     Use -v to get more diff
E   Failing test case: test_include_exclude(
E       constituents=set(),  # or any other generated value
E       include={UUID('00000000-0000-0000-0000-000000000001')},
E       exclude={UUID('00000000-0000-0000-0000-000000000001')},
FAILED tests/hat/properties/test_universe.py::test_include_exclude - Assertio [cut]
```

Break: in `backend/app/hat/engine/portfolio/universe.py`: overlapping intervals pass. Check run: `test_universe.py::test_membership_intervals_detects_bad_rows`.

```
E   AssertionError: assert True == (1 == 0)
E    +  where True = <function intervals_valid at 0x7ff6ee5566c0>([Interval(instrument_id=UUID('00000000-0000-0000-0000-000000000001'), effective_from=datetime.date(2026, 1, 1), effect...strument_id=U
E    +    where <function intervals_valid at 0x7ff6ee5566c0> = universe.intervals_valid
E   Failing test case: test_membership_intervals_detects_bad_rows(
E       # The test always failed when commented parts were varied together.
E       instrument=UUID('00000000-0000-0000-0000-000000000001'),  # or any other generated value
E       day=START + timedelta(days=0),  # or any other generated value
E       length=0,  # or any other generated value
E       gap=1,
FAILED tests/hat/properties/test_universe.py::test_membership_intervals_detects_bad_rows
```

Break: in `backend/app/hat/engine/portfolio/universe.py`: leaving the universe forces an exit. Check run: `test_universe.py::test_leave_no_force_sell`.

```
E   AssertionError: assert frozenset({UU...0000000001')}) == frozenset()
E     
E     Extra items in the left set:
E     UUID('00000000-0000-0000-0000-000000000001')
E     Use -v to get more diff
E   Failing test case: test_leave_no_force_sell(
E       before={UUID('00000000-0000-0000-0000-000000000001')},
E       after=set(),
E       open_positions={UUID('00000000-0000-0000-0000-000000000001')},
FAILED tests/hat/properties/test_universe.py::test_leave_no_force_sell - Asse [cut]
```

Break: in `backend/app/hat/engine/portfolio/universe.py`: an edit may reach into recorded history. Check run: `test_universe.py::test_membership_intervals`.

```
E   AssertionError: assert False
E    +  where False = <function intervals_valid at 0x7fa0b1b426c0>([Interval(instrument_id=UUID('00000000-0000-0000-0000-000000000001'), effective_from=datetime.date(2026, 1, 2), effect...strument_id=
E    +    where <function intervals_valid at 0x7fa0b1b426c0> = universe.intervals_valid
E    +    and   [Interval(instrument_id=UUID('00000000-0000-0000-0000-000000000001'), effective_from=datetime.date(2026, 1, 2), effect...strument_id=UUID('00000000-0000-0000-0000-000000000001'), effec
E   Failing test case: test_membership_intervals(
E       ops=[('add',
E            UUID('00000000-0000-0000-0000-000000000001'),
E            START + timedelta(days=1)),
E        ('add',
FAILED tests/hat/properties/test_universe.py::test_membership_intervals - Ass [cut]
```

Break: in `backend/app/hat/engine/portfolio/service.py`: publishing a version does not apply its members. Check run: `test_universe.py::test_seeded_version_is_seen_only_once_published`.

```
FAILED tests/hat/properties/test_universe.py::test_seeded_version_is_seen_only_once_published
```

### PBT regime.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/regime.py`: confirmation after two days. Check run: `test_regime.py::test_three_day_confirmation`.

```
E   AssertionError: assert ['bull_quiet'...ull_volatile'] == ['bull_quiet'... 'bull_quiet']
E     
E     At index 2 diff: 'bull_volatile' != 'bull_quiet'
E     Use -v to get more diff
E   Failing test case: test_three_day_confirmation(
E       raws=['bull_quiet', 'bull_volatile', 'bull_volatile'],
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAFBAAFBAQFBAQA=') as a decorator on your test function
FAILED tests/hat/properties/test_regime.py::test_three_day_confirmation - Ass [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: the run is not reset by a different state. Check run: `test_regime.py::test_three_day_confirmation`.

```
E   AssertionError: assert ['bull_quiet'...ull_volatile'] == ['bull_quiet'... 'bull_quiet']
E     
E     At index 2 diff: 'bull_volatile' != 'bull_quiet'
E     Use -v to get more diff
E   Failing test case: test_three_day_confirmation(
E       raws=['bull_quiet', 'bull_quiet', 'bull_volatile'],
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAFBAAFBAAFBAQA=') as a decorator on your test function
FAILED tests/hat/properties/test_regime.py::test_three_day_confirmation - Ass [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: percentile counts equal values. Check run: `test_regime.py::test_inputs`.

```
E   AssertionError: assert Decimal('100') == ((Decimal('100') * 0) / 252)
E    +  where Decimal('100') = RegimeInputs(close=Decimal('0.01'), sma50=Decimal('0.01'), sma200=Decimal('0.01'), vix=Decimal('5'), vix_pct=Decimal('100'), breadth_pct=Decimal('0'), ad_ratio=None).vix
E    +  and   Decimal('100') = D(100)
E    +  and   0 = sum(<generator object test_inputs.<locals>.<genexpr> at 0x7fdc0609d000>)
E   Failing test case: test_inputs(
E       pair=({datetime.date(2025, 1, 1): D(1) / 100,
E         datetime.date(2025, 1, 2): D(1) / 100,
E         datetime.date(2025, 1, 3): D(1) / 100,
E         datetime.date(2025, 1, 4): D(1) / 100,
FAILED tests/hat/properties/test_regime.py::test_inputs - AssertionError: ass [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: inputs read later days. Check run: `test_regime.py::test_inputs`.

```
FAILED tests/hat/properties/test_regime.py::test_inputs - ExceptionGroup: Hyp [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: correction checked before bear. Check run: `test_regime.py::test_classification_total`.

```
E   AssertionError: assert 'correction' == 'bear'
E     
E     - bear
E     + correction
E   Failing test case: test_classification_total(
E       # The test always failed when commented parts were varied together.
E       close=D(1) / 100,
E       sma50=D(1) / 100,  # or any other generated value
E       sma200=D(1) / 100,  # or any other generated value
FAILED tests/hat/properties/test_regime.py::test_classification_total - Asser [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: the Bear multiplier above the quiet one. Check run: `test_regime.py::test_multiplier_table`.

```
E   AssertionError: assert [Decimal('1.0...ecimal('1.5')] == [Decimal('1.5...ecimal('0.5')]
E     
E     At index 0 diff: Decimal('1.0') != Decimal('1.5')
E     Use -v to get more diff
E   Failing test case: test_multiplier_table(
E       # The test always failed when commented parts were varied together.
E       state='bull_quiet',  # or any other generated value
E       other='',  # or any other generated value
E   )
FAILED tests/hat/properties/test_regime.py::test_multiplier_table - Assertion [cut]
```

Break: in `backend/app/hat/engine/portfolio/regime.py`: derived multipliers not held to the bounds. Check run: `test_regime.py::test_multiplier_derivation`.

```
FAILED tests/hat/properties/test_regime.py::test_multiplier_derivation - Exce [cut]
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the job stores the raw state as confirmed. Check run: `test_regime.py::test_regime_job`.

```
E   assert [True, True, ...e, True, True] == [True, True, ...e, True, True]
E     
E     At index 3 diff: True != False
E     Use -v to get more diff
E   Failing test case: test_regime_job(
E       closes=[414,
E        515,
E        574,
E        1431,
FAILED tests/hat/properties/test_regime.py::test_regime_job - assert [True, T [cut]
```

Break: in `backend/app/hat/engine/portfolio/service.py`: a rerun writes a second row. Check run: `test_allocator.py::test_allocation_rows_and_rerun`.

```
E   sqlite3.IntegrityError: UNIQUE constraint failed: hat_regime_state.trade_date, hat_regime_state.market_id, hat_regime_state.model_version_id
E   sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: hat_regime_state.trade_date, hat_regime_state.market_id, hat_regime_state.model_version_id
E   [SQL: INSERT INTO hat_regime_state (trade_date, market_id, model_version_id, state, probabilities, inputs, confirmed) VALUES (?, ?, ?, ?, ?, ?, ?)]
E   [parameters: ('2026-10-21', '01a15e59dca770c69226bd3cd24343eb', '01a15e59dcae7700a9fb567272fb9782', 'bull_quiet', 'null', '{"close": "110", "sma50": "105", "sma200": "100", "vix": "15", "vix_pct":
E   (Background on this error at: https://sqlalche.me/e/21/gkpj)
E   Failing test case: test_allocation_rows_and_rerun(
E       # The test always failed when commented parts were varied together.
E       bots=[('stage2-contraction-breakout',
E            'live',
FAILED tests/hat/properties/test_allocator.py::test_allocation_rows_and_rerun
```

### PBT health.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/health.py`: the zero band edge reads as half. Check run: `test_health.py::test_bands_edges`.

```
E   AssertionError: assert False == ('0.12' == '0.12'
E    +  where False = Health(multiplier=Decimal('0.5'), probation=False).probation
E     
E       0.12)
E   Failing test case: test_bands_edges(
E       peak=D(1) / 100,  # or any other generated value
E       edge='0.12',
E   )
E   
FAILED tests/hat/properties/test_health.py::test_bands_edges - AssertionError [cut]
```

Break: in `backend/app/hat/engine/portfolio/health.py`: no half band. Check run: `test_health.py::test_bands_across_the_drawdown_range`.

```
E   AssertionError: assert Decimal('1.0') == Decimal('0.5')
E    +  where Decimal('1.0') = Health(multiplier=Decimal('1.0'), probation=False).multiplier
E   Failing test case: test_bands_across_the_drawdown_range(
E       peak=D(1) / 100,  # or any other generated value
E       basis_points=500,
E       bands=(Decimal('0.05'), Decimal('0.10')),
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQgH0QQE=') as a decorator on your test function
FAILED tests/hat/properties/test_health.py::test_bands_across_the_drawdown_range
```

### PBT allocator.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/allocator.py`: no smoothing. Check run: `test_allocator.py::test_smoothing`.

```
E   AssertionError: assert Decimal('0.02') <= Decimal('0.01')
E    +  where Decimal('0.02') = abs((Decimal('0') - Decimal('0.02')))
E   Failing test case: test_smoothing(
E       previous=D(2) / 100,
E       target=D(0) / 100,
E       step=D(1) / 100,
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQJBAEEB') as a decorator on your test function
FAILED tests/hat/properties/test_allocator.py::test_smoothing - AssertionErro [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: capital rounded half up. Check run: `test_allocator.py::test_capital_and_risk`.

```
E   AssertionError: assert Decimal('0.0001') == Decimal('0.0000')
E    +  where Decimal('0.0001') = Allocation(bot_id=UUID('7228d037-2e94-4ddb-a3a0-e27b654adf84'), base_weight=Decimal('0.17'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.6'), final_weight=Dec
E    +  and   Decimal('0.0000') = <built-in method quantize of decimal.Decimal object at 0x7f0318928740>(Decimal('0.0001'), rounding='ROUND_DOWN')
E    +    where <built-in method quantize of decimal.Decimal object at 0x7f0318928740> = Decimal('0.0000510').quantize
E    +    and   Decimal('0.0001') = D('0.0001')
E   Failing test case: test_capital_and_risk(
E       sleeves=[SleeveInput(bot_id=UUID('7228d037-2e94-4ddb-a3a0-e27b654adf84'),
E         sleeve='S1',
E         cluster='c1',
FAILED tests/hat/properties/test_allocator.py::test_capital_and_risk - Assert [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: no cluster cap. Check run: `test_allocator.py::test_cluster_cap`.

```
E   AssertionError: assert Decimal('0.1008') <= Decimal('0.1')
E    +  where Decimal('0.1008') = sum(<generator object test_cluster_cap.<locals>.<genexpr> at 0x7fd2ffd22cf0>, Decimal('0'))
E    +    where Decimal('0') = D(0)
E    +  and   Decimal('0.1') = RiskPolicy(risk_pct=Decimal('0.0001'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), health_zero_dd=Decimal('0.
E   Failing test case: test_cluster_cap(
E       sleeves=[SleeveInput(bot_id=UUID('c39000f4-2571-4dd0-8d15-ae0881f28137'),
E         sleeve='S1',
E         cluster='c1',
E         lifecycle_state='live',
FAILED tests/hat/properties/test_allocator.py::test_cluster_cap - AssertionEr [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: no whole-book cap. Check run: `test_allocator.py::test_cash_remainder`.

```
E   AssertionError: assert Decimal('1.00104') <= 1
E   Failing test case: test_cash_remainder(
E       sleeves=[SleeveInput(bot_id=UUID('ed923b05-aa9c-47b8-8d05-49d0bdd5be22'),
E         sleeve='S1',
E         cluster='c1',
E         lifecycle_state='live',
E         regime_table_value=D(0) / 100,
E         health=Health(multiplier=Decimal('1.0'), probation=False),
E         previous_regime_mult=None,
FAILED tests/hat/properties/test_allocator.py::test_cash_remainder - Assertio [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: a non-live sleeve gets weight. Check run: `test_allocator.py::test_formula_and_bounds`.

```
E   AssertionError: assert (Decimal('0.6') == 0 or False)
E    +  where Decimal('0.6') = Allocation(bot_id=UUID('5581474a-9e84-49c3-9baa-409338f50aea'), base_weight=Decimal('0'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.6'), final_weight=Decimal('
E   Failing test case: test_formula_and_bounds(
E       # The test always failed when commented parts were varied together.
E       sleeves=[SleeveInput(bot_id=UUID('a476fff6-9542-44b0-91c4-c20bef163257'),
E         sleeve='S1',
E         cluster='c1',
E         lifecycle_state='live',
E         regime_table_value=D(0) / 100,
FAILED tests/hat/properties/test_allocator.py::test_formula_and_bounds - Asse [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: regime multiplier outside the policy bounds. Check run: `test_allocator.py::test_bounds_from_policy`.

```
E   AssertionError: assert Decimal('0.05') <= Decimal('0')
E    +  where Decimal('0') = Allocation(bot_id=UUID('f33fc8d9-6816-424a-9bf9-f0e6dd0ddc0f'), base_weight=Decimal('0'), regime_mult=Decimal('0'), health_mult=Decimal('0.6'), final_weight=Decimal('0.0')
E   Failing test case: test_bounds_from_policy(
E       # The test always failed when commented parts were varied together.
E       sleeves=[SleeveInput(bot_id=UUID('f33fc8d9-6816-424a-9bf9-f0e6dd0ddc0f'),
E         sleeve='S1',
E         cluster='c1',
E         lifecycle_state='live',
E         regime_table_value=D(0) / 100,
FAILED tests/hat/properties/test_allocator.py::test_bounds_from_policy - Asse [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: risk per trade ignores health. Check run: `test_allocator.py::test_capital_and_risk`.

```
E   AssertionError: assert Decimal('0.000005') == ((Decimal('0.0001') * Decimal('0.05')) * Decimal('0.6'))
E    +  where Decimal('0.000005') = Allocation(bot_id=UUID('8d9ab1f9-ddf2-4fb2-bde0-67dd99cb83f4'), base_weight=Decimal('0'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.6'), final_weight=Deci
E    +  and   Decimal('0.0001') = RiskPolicy(risk_pct=Decimal('0.0001'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), health_zero_dd=Decimal(
E    +  and   Decimal('0.05') = Allocation(bot_id=UUID('8d9ab1f9-ddf2-4fb2-bde0-67dd99cb83f4'), base_weight=Decimal('0'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.6'), final_weight=Decimal(
E    +  and   Decimal('0.6') = Allocation(bot_id=UUID('8d9ab1f9-ddf2-4fb2-bde0-67dd99cb83f4'), base_weight=Decimal('0'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.6'), final_weight=Decimal('
E   Failing test case: test_capital_and_risk(
E       sleeves=[SleeveInput(bot_id=UUID('8d9ab1f9-ddf2-4fb2-bde0-67dd99cb83f4'),
E         sleeve='S1',
E         cluster='c1',
FAILED tests/hat/properties/test_allocator.py::test_capital_and_risk - Assert [cut]
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: a sleeve back from health 0 keeps smoothing from 0. Check run: `test_allocator.py::test_smoothing_in_the_allocation`.

```
E   AssertionError: assert Decimal('0.25') == Decimal('0.6')
E    +  where Decimal('0.25') = Allocation(bot_id=UUID('db03906b-7888-405e-8b74-0568de969d2a'), base_weight=Decimal('0'), regime_mult=Decimal('0.05'), health_mult=Decimal('0.25'), final_weight=Decimal
E    +  and   Decimal('0.6') = <function clamp at 0x7f212c4a31c0>(Decimal('1.0'), Decimal('0.05'), Decimal('0.6'))
E    +    where <function clamp at 0x7f212c4a31c0> = allocator.clamp
E    +    and   Decimal('1.0') = Health(multiplier=Decimal('1.0'), probation=False).multiplier
E    +      where Health(multiplier=Decimal('1.0'), probation=False) = SleeveInput(bot_id=UUID('db03906b-7888-405e-8b74-0568de969d2a'), sleeve='S1', cluster='c1', lifecycle_state='live', re...ealth=He
E   Failing test case: test_smoothing_in_the_allocation(
E       # The test always failed when commented parts were varied together.
E       sleeves=[SleeveInput(bot_id=UUID('f8a3cf3c-88f0-4499-8992-f50d682e5224'),
FAILED tests/hat/properties/test_allocator.py::test_smoothing_in_the_allocation
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: only two sleeves allocated. Check run: `test_allocator.py::test_sleeve_count_agnostic`.

```
E   AssertionError: assert (2 == 3)
E    +  where 2 = len({UUID('eec337c0-573c-4732-aa3f-616e0165657c'): Allocation(bot_id=UUID('eec337c0-573c-4732-aa3f-616e0165657c'), base_we..., health_mult=Decimal('0.6'), final_weight=Decimal('0.000
E    +  and   3 = len([SleeveInput(bot_id=UUID('eec337c0-573c-4732-aa3f-616e0165657c'), sleeve='S1', cluster='c1', lifecycle_state='live', r...'0'), health=Health(multiplier=Decimal('1.0'), probation=
E   Failing test case: test_sleeve_count_agnostic(
E       # The test always failed when commented parts were varied together.
E       sleeves=[SleeveInput(bot_id=UUID('eec337c0-573c-4732-aa3f-616e0165657c'),
E         sleeve='S1',
E         cluster='c1',
E         lifecycle_state='live',
FAILED tests/hat/properties/test_allocator.py::test_sleeve_count_agnostic - A [cut]
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the policy of a far-future date. Check run: `test_allocator.py::test_bounds_from_the_policy_in_force`.

```
E   AssertionError: assert Decimal('0.05') == Decimal('0.25')
E    +  where Decimal('0.05') = RiskPolicy(risk_pct=Decimal('0.0025'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), health_zero_dd=Decimal('0
E    +  and   Decimal('0.25') = D('0.25')
E   Failing test case: test_bounds_from_the_policy_in_force(
E       # The test always failed when commented parts were varied together.
E       lo=D(5) / 100,
E       hi=D(50) / 100,  # or any other generated value
E       days_before=1,  # or any other generated value
E   )
FAILED tests/hat/properties/test_allocator.py::test_bounds_from_the_policy_in_force
```

Break: in `backend/app/hat/engine/portfolio/service.py`: no regime row means a default regime. Check run: `test_allocator.py::test_allocation_needs_a_regime_row`.

```
E   Failed: DID NOT RAISE PortfolioError
E   Failing test case: test_allocation_needs_a_regime_row(
E       bots=[('stage2-contraction-breakout',
E            'live',
E            'c1')],  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAFBAEEAQQAA') as a decorator on your test function
FAILED tests/hat/properties/test_allocator.py::test_allocation_needs_a_regime_row
```

Break: in `backend/app/hat/engine/portfolio/service.py`: a rerun overwrites. Check run: `test_allocator.py::test_allocation_rows_and_rerun`.

```
E   sqlite3.IntegrityError: UNIQUE constraint failed: hat_allocation_daily.trade_date, hat_allocation_daily.bot_id
E   sqlalchemy.exc.IntegrityError: (raised as a result of Query-invoked autoflush; consider using a session.no_autoflush block if this flush is occurring prematurely)
E   (sqlite3.IntegrityError) UNIQUE constraint failed: hat_allocation_daily.trade_date, hat_allocation_daily.bot_id
E   [SQL: INSERT INTO hat_allocation_daily (trade_date, bot_id, base_weight, regime_mult, health_mult, final_weight, capital, risk_pct, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)]
E   [parameters: ('2026-10-21', 'cb4b3a9accac487eb3257c1317837b55', 0.5, 1.0, 1.0, 0.5, 500.0, 0.0025, 'INR')]
E   (Background on this error at: https://sqlalche.me/e/21/gkpj)
E   Failing test case: test_allocation_rows_and_rerun(
E       # The test always failed when commented parts were varied together.
E       bots=[('stage2-contraction-breakout',
FAILED tests/hat/properties/test_allocator.py::test_allocation_rows_and_rerun
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the allocation closes open positions. Check run: `test_allocator.py::test_no_effect_on_open_positions`.

```
E   AssertionError: assert [('aa00f9d8-0...'), 'closed')] == [('aa00f9d8-0...96'), 'open')]
E     
E     At index 0 diff: ('aa00f9d8-001a-4b29-8af4-c80edccffad2', 1, Decimal('100'), Decimal('95'), Decimal('96'), 'closed') != ('aa00f9d8-001a-4b29-8af4-c80edccffad2', 1, Decimal('100'), Decimal('95'),
E     Use -v to get more diff
E   Failing test case: test_no_effect_on_open_positions(
E       # The test always failed when commented parts were varied together.
E       bots=[('stage2-contraction-breakout',
E            'live',
E            'c1')],  # or any other generated value
FAILED tests/hat/properties/test_allocator.py::test_no_effect_on_open_positions
```

Break (PR 34): in `backend/app/hat/daily/live.py`: `LiveContexts` ignores the nominal allocation (main before PR 34). Check run: `test_allocator.py::test_virtual_entries_sized`.

```
E   AssertionError: assert Decimal('0.0000') == Decimal('50000.0000')
```

Break (PR 34): the virtual book counts real capital only.

```
E   AssertionError: global_open_risk
```

Break (PR 34): a nominal entry keeps the sleeve's health 0.

```
E   AssertionError: assert Decimal('0E-10') == 1
```

Break (PR 34): in `allocator.py`: every sleeve gets a nominal allocation. Check run: `test_virtual_allocation`.

```
E   AssertionError: assert Decimal('0.0000') is None
```

Break (PR 34): the nominal risk is multiplied by health.

```
E   AssertionError: assert Decimal('0.000000') == (Decimal('0.0001') * Decimal('0.05'))
```

### PBT lifecycle.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: 200 trades is not enough. Check run: `test_lifecycle.py::test_backtest_gate`.

```
E   AssertionError: assert False == ((Decimal('0.95') is not None and Decimal('0.95') >= Decimal('0.95') and 200 >= 200))
E    +  where False = <function backtest_gate at 0x7f716327fab0>(Decimal('0.95'), 200)
E    +    where <function backtest_gate at 0x7f716327fab0> = lifecycle.backtest_gate
E    +  and   Decimal('0.95') = D('0.95')
E   Failing explicit example: test_backtest_gate(
E       dsr=Decimal('0.95'),
E       trades=200,
E   )
FAILED tests/hat/properties/test_lifecycle.py::test_backtest_gate - Assertion [cut]
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: the start day counts as a shadow session. Check run: `test_lifecycle.py::test_shadow_days`.

```
E   assert 1 == 0
E    +  where 1 = <function sessions_after at 0x7f4d89fc3ab0>({datetime.date(2026, 3, 2)}, datetime.date(2026, 3, 2), datetime.date(2026, 3, 2))
E    +    where <function sessions_after at 0x7f4d89fc3ab0> = lifecycle.sessions_after
E   Failing test case: test_shadow_days(
E       offsets={0},
E       start_offset=0,
E       length=0,  # or any other generated value
E   )
E   
FAILED tests/hat/properties/test_lifecycle.py::test_shadow_days - assert 1 == 0
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: wrong t quantile. Check run: `test_lifecycle.py::test_t_quantile`.

```
E   assert 0.0006152078222397273 < 1e-05
E    +  where 0.0006152078222397273 = abs((0.4493847921777603 - 0.45))
E    +    where 0.4493847921777603 = t_cdf_upper_tail_check(49, 1.6703314260057984)
E   Failing test case: test_t_quantile(
E       df=49,  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEx') as a decorator on your test function
FAILED tests/hat/properties/test_lifecycle.py::test_t_quantile - assert 0.000 [cut]
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: expectancy trigger from 10 trades. Check run: `test_lifecycle.py::test_probation_trigger_by_length`.

```
FAILED tests/hat/properties/test_lifecycle.py::test_probation_trigger_by_length
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: health 0 does not trigger probation. Check run: `test_lifecycle.py::test_probation_trigger`.

```
E   assert False == True
E    +  where False = <function probation_trigger at 0x7f949331c250>(True, [])
E    +    where <function probation_trigger at 0x7f949331c250> = lifecycle.probation_trigger
E   Failing test case: test_probation_trigger(
E       health_zero=True,
E       results=[],  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAEA') as a decorator on your test function
FAILED tests/hat/properties/test_lifecycle.py::test_probation_trigger - asser [cut]
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: benchmark is the discount, not the remainder. Check run: `test_lifecycle.py::test_discounted_benchmark`.

```
FAILED tests/hat/properties/test_lifecycle.py::test_discounted_benchmark - Ex [cut]
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: anyone can promote from probation. Check run: `test_lifecycle.py::test_probation_promotion_is_owner_only`.

```
E   AssertionError: assert True is False
E    +  where True = Verdict(allowed=True, reason='ok').allowed
E   Failing test case: test_probation_promotion_is_owner_only(
E       # The test always failed when commented parts were varied together.
E       expectancy=D(1) / 100,  # or any other generated value
E       extra=[D(0) / 100],  # or any other generated value
E       owner=False,
E   )
E   Explanation:
FAILED tests/hat/properties/test_lifecycle.py::test_probation_promotion_is_owner_only
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: an edge from backtested to live. Check run: `test_lifecycle.py::test_guard_matches_the_rules`.

```
E   AssertionError: assert not True
E    +  where True = Verdict(allowed=True, reason='ok').allowed
E   Failing test case: test_guard_matches_the_rules(
E       # The test always failed when commented parts were varied together.
E       current='backtested',
E       target='live',
E       ev=Evidence(today=START + timedelta(days=0),
E        dsr_prob=None,
E        backtest_trades=0,
FAILED tests/hat/properties/test_lifecycle.py::test_guard_matches_the_rules
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: retire on months or trades both required. Check run: `test_lifecycle.py::test_retire_gate`.

```
E   assert False == ((True and True))
E    +  where False = <function retire_gate at 0x7f494fe987d0>(datetime.date(2026, 3, 2), datetime.date(2026, 3, 2), 50, True)
E    +    where <function retire_gate at 0x7f494fe987d0> = lifecycle.retire_gate
E   Failing test case: test_retire_gate(
E       entered=START + timedelta(days=0),  # or any other generated value
E       after_days=0,
E       trades=50,
E       failing=True,
E   )
FAILED tests/hat/properties/test_lifecycle.py::test_retire_gate - assert Fals [cut]
```

Break: in `backend/app/hat/engine/portfolio/lifecycle.py`: probation signals route as real. Check run: `test_lifecycle.py::test_only_live_routes_real`.

```
E   AssertionError: assert False == ('probation' != 'live')
E    +  where False = <function routes_as_shadow at 0x7f070bc996f0>('probation')
E    +    where <function routes_as_shadow at 0x7f070bc996f0> = lifecycle.routes_as_shadow
E   Failing test case: test_only_live_routes_real(
E       state='probation',
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEE') as a decorator on your test function
FAILED tests/hat/properties/test_lifecycle.py::test_only_live_routes_real - A [cut]
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the transition ignores its guard. Check run: `test_lifecycle.py::test_transition_records_its_event`.

```
E   assert False
E   Failing test case: test_transition_records_its_event(
E       # The test always failed when commented parts were varied together.
E       current='research',
E       target='research',  # or any other generated value
E       ev=Evidence(today=START + timedelta(days=0),
E        dsr_prob=None,
E        backtest_trades=0,
E        sessions=frozenset(),
FAILED tests/hat/properties/test_lifecycle.py::test_transition_records_its_event
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the event names the wrong target. Check run: `test_lifecycle.py::test_transition_records_its_event`.

```
E   AssertionError: assert ('live' == 'live'
E     
E       live and 'live' == 'probation'
E     
E     - probation
E     + live)
E   Failing test case: test_transition_records_its_event(
E       current='live',
E       target='probation',
FAILED tests/hat/properties/test_lifecycle.py::test_transition_records_its_event
```

Break: in `backend/app/hat/engine/portfolio/service.py`: the allocation does not move a health 0 sleeve. Check run: `test_lifecycle.py::test_allocation_moves_a_health_zero_sleeve_to_probation`.

```
E   AssertionError: assert 'live' == 'probation'
E     
E     - probation
E     + live
E   Failing test case: test_allocation_moves_a_health_zero_sleeve_to_probation(
E       peak_cents=1,  # or any other generated value
E       drop_pct=12,
E   )
E   Explanation:
FAILED tests/hat/properties/test_lifecycle.py::test_allocation_moves_a_health_zero_sleeve_to_probation
```

Break: in `backend/app/hat/engine/portfolio/service.py`: a replay reads trades closed later. Check run: `test_lifecycle.py::test_trade_results_ignore_later_trades`.

```
E   AssertionError: assert [Decimal('0E-10')] == []
E     
E     Left contains one more item: Decimal('0E-10')
E     Use -v to get more diff
E   Failing test case: test_trade_results_ignore_later_trades(
E       trades=[(
E            1,
E            D(0) / 100,  # or any other generated value
E        )],
FAILED tests/hat/properties/test_lifecycle.py::test_trade_results_ignore_later_trades
```

Break: (PR 46) the seed's Probation band (`health_bands.zero_dd`) back to 0.12: a sleeve 10% under its peak stays live (`lifecycle.allocation_moves_a_health_zero_sleeve_to_probation`, whose 9% and 10% edges are now `@example`s).

```
E   AssertionError: assert 'live' == 'probation'
```

### CONF portfolio.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/engine/portfolio/jobs.py`: a second writer of lifecycle_state. Check run: `test_conf_portfolio.py::test_lifecycle_single_writer`.

```
E   AssertionError: assert [('hat/engine...'service.py')] == [('hat/engine...'service.py')]
E     
E     At index 0 diff: ('hat/engine/portfolio/jobs.py', 'jobs.py') != ('hat/engine/portfolio/service.py', 'service.py')
E     Left contains one more item: ('hat/engine/portfolio/service.py', 'service.py')
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_portfolio.py::test_lifecycle_single_writer
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: a rule module imports the database. Check run: `test_conf_portfolio.py::test_portfolio_rules_are_pure`.

```
E   AssertionError: allocator
E   assert not [('from', 'sqlalchemy')]
E    +  where [('from', 'sqlalchemy')] = <built-in method findall of re.Pattern object at 0x1aecc2f0>('"""The nightly allocator (05 §5, HAT-PORT-050..056) as a pure function.\n\n    w_s = b_s x m_regi
E    +    where <built-in method findall of re.Pattern object at 0x1aecc2f0> = re.compile('^\\s*(from|import)\\s+(sqlalchemy|app\\.brokers|app\\.database|app\\.hat\\.models|app\\.hat\\.execution|app\\
FAILED tests/hat/conformance/test_conf_portfolio.py::test_portfolio_rules_are_pure
```

Break: in `backend/app/hat/models/strategy.py`: the bot does not pin a universe version. Check run: `test_conf_portfolio.py::test_bot_has_sleeve_columns`.

```
E   AssertionError: assert {'live_versio...on_id': set()} == {'live_versio...rse_version'}}
E     
E     Omitting 1 identical items, use -vv to show
E     Differing items:
E     {'universe_version_id': set()} != {'universe_version_id': {'hat_universe_version'}}
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_portfolio.py::test_bot_has_sleeve_columns
```

Break: in `backend/app/hat/models/strategy.py`: a risk number on the bot row. Check run: `test_conf_portfolio.py::test_bot_sleeve_columns`.

```
E   AssertionError: assert ['capital_pct'] == []
E     
E     Left contains one more item: 'capital_pct'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_portfolio.py::test_bot_sleeve_columns
```

Break: in `backend/app/hat/engine/portfolio/jobs.py`: a setattr writer of lifecycle_state. Check run: `test_conf_portfolio.py::test_lifecycle_single_writer`.

```
E   AssertionError: assert [('hat/engine...'service.py')] == [('hat/engine...'service.py')]
E     
E     At index 0 diff: ('hat/engine/portfolio/jobs.py', 'jobs.py') != ('hat/engine/portfolio/service.py', 'service.py')
E     Left contains one more item: ('hat/engine/portfolio/service.py', 'service.py')
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_portfolio.py::test_lifecycle_single_writer
```

Break: in `backend/app/hat/engine/portfolio/allocator.py`: a rule module imports the service by absolute path. Check run: `test_conf_portfolio.py::test_portfolio_rules_are_pure`.

```
E   AssertionError: allocator
E   assert not [('from', 'app.hat.engine.portfolio.service', 'service')]
E    +  where [('from', 'app.hat.engine.portfolio.service', 'service')] = <built-in method findall of re.Pattern object at 0x186c2d10>('"""The nightly allocator (05 §5, HAT-PORT-050..056) as a pure fu
E    +    where <built-in method findall of re.Pattern object at 0x186c2d10> = re.compile('^\\s*(from|import)\\s+(sqlalchemy|app\\.brokers|app\\.database|app\\.hat\\.models|app\\.hat\\.execution|app\\
FAILED tests/hat/conformance/test_conf_portfolio.py::test_portfolio_rules_are_pure
```

### PBT regime.strategy_rules

Break: in `backend/app/hat/engine/regime_gate.py`: close_above_sma_and_rising accepts a close equal to its SMA.

```
E   AssertionError: assert True is False
FAILED tests/hat/properties/test_regime.py::test_strategy_rules - AssertionEr [cut]
============================== 1 failed in 0.57s ===============================
```

Break: (PR 55) the fast/slow rule's config default is 1 session again; a regime without `rising_days` then reads one session.

```
E   AssertionError: assert True is False
E    +  where True = <function regime_allows_longs>(Regime(..., params={'fast': 2, 'slow': 7, 'rising_days': 1}), [cut]
E    +    where <function regime_allows_longs> = regime_gate.regime_allows_longs
E    +  and   False = <function test_strategy_rules.<locals>.fast_slow>(3)
E   Failing test case: test_strategy_rules(
E       closes=[Decimal(1),
FAILED tests/hat/properties/test_regime.py::test_strategy_rules - AssertionEr [cut]
```

Break: (PR 55) `sma_fast_above_slow_both_rising` compares with one session earlier whatever `rising_days` says.

```
E   AssertionError: assert True is False
E    +  where True = <function regime_allows_longs>(Regime(..., params={'fast': 2, 'slow': 7, 'rising_days': 3}), [cut]
E    +    where <function regime_allows_longs> = regime_gate.regime_allows_longs
E    +  and   False = <function test_strategy_rules.<locals>.fast_slow>(3)
E   Failing test case: test_strategy_rules(
E       closes=[Decimal(1),
FAILED tests/hat/properties/test_regime.py::test_strategy_rules - AssertionEr [cut]
```

### PBT dsr.evidence_current_trials

Break: in `backend/app/hat/backtest/runs.py`: the evidence reuses the trial count stored when the run completed.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:248: in test_evidence_current_trials
    @settings(max_examples=30, deadline=None)
                   ^^^
tests/hat/properties/test_dsr.py:290: in test_evidence_current_trials
    assert facts.trials == runs.trial_count(session, family) == 1 + len(later)
E   AssertionError: assert 1 == 2
```

### PBT admin.*

Break: (1) in `backend/app/hat/engine/portfolio/service.py`: `owner_transition` no longer refuses Live on a version that is not live (`admin.first_deploy`).

```
E   AssertionError: assert 'refused: shadow_days\n' == 'refused: version_not_live\n'
E     - refused: version_not_live
E     + refused: shadow_days
```

Break: (2) `owner_transition` drops its market-hours check (`admin.refusal_writes_nothing`, case `promote_in_hours`).

```
E   AssertionError: assert 'refused: no_such_edge\n' == 'refused: market_hours\n'
E     - refused: market_hours
E     + refused: no_such_edge
```

Break: (3) `transition` no longer writes `via` into the payload (`admin.first_deploy`).

```
E   KeyError: 'via'
E   Failing explicit example: test_first_deploy(
E       waited=20,
```

Break: (4) in `backend/app/hat/admin.py`: `main` commits the session before re-raising a refusal (`admin.refusal_writes_nothing`, case `universe_unknown_bot`).

```
E   assert {'hat_broker_...cli'})"], ...} == {'hat_broker_...cli'})"], ...}
E     Omitting 40 identical items, use -vv to show
E     Differing items:
```

Break: (5) `publish-universe` publishes without refusing unknown symbols (`admin.refusal_writes_nothing`, case `unknown_symbol`).

```
E   AssertionError: assert 'refused: uni...ty_interval\n' == 'refused: unk...ymbols: ZZZ\n'
E     - refused: unknown_symbols: ZZZ
E     + refused: universe_empty_interval
```

Break: (6) (CodeAnt review) `assign_universe` drops its market-hours check (case `universe_in_hours`); `resolve_symbols` ignores the CSV ISIN (case `isin_mismatch`); `seed-bots` skips `require_active_account` (case `bots_on_unknown_account`); the file read catches only `OSError` (case `draft_bad_utf8`). Each run alone:

```
== universe in hours: 1 failed      E   assert 0 == 2   case='universe_in_hours',
== isin ignored: 1 failed           E   assert 0 == 2   case='isin_mismatch',
== bots on any account: 1 failed    E   assert 0 == 2   case='bots_on_unknown_account',
== bad utf8 uncaught: 1 failed      E   UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 5
```

Break: (7) (Sonar) `_read` drops the admin-folder and suffix check (cases `file_outside_admin`, `draft_wrong_suffix`).

```
E   AssertionError: assert 'refused: config_unreadable\n' == 'refused: fil...tside_admin\n'
E     - refused: file_outside_admin
E     + refused: config_unreadable
```


Break: (PR 45) `confirm_ddpi` drops its market-hours check (`admin.refusal_writes_nothing`, case `ddpi_in_hours`).

```
E   assert 0 == 2
E   Failing test case: test_refusal_writes_nothing(
E       case='ddpi_in_hours',
```

Break: (PR 45) `confirm_ddpi` records the event but leaves `ddpi_confirmed` false (`admin.first_deploy`).

```
E   AssertionError: assert {'broker_acco...irmed': False} == {'broker_acco...firmed': True}
```

### CONF ops.admin_cli_calls_services

Break: in `backend/app/hat/admin.py`: `_publish_universe` sets `named.name` itself.

```
E   AssertionError: assert ['assigns .name'] == []
E     Left contains one more item: 'assigns .name'
== cli writes a column: 1 failed, 1 passed, 2 deselected
```
