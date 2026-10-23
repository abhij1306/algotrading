---
name: verify-portfolio
description: How to run, read and update the checks behind Hire a Trader's portfolio layer (universe versions and point-in-time membership, the regime engine and its 3-day rule, the health multiplier, the nightly allocator and the sleeve lifecycle with its guards). Use before and after any change to backend/app/hat/engine/portfolio, verification/tla/RegimeConfirmation.tla, verification/tla/SleeveLifecycle.tla or the portfolio properties and conformance checks.
---

# verify-portfolio

Checks that the portfolio layer (`backend/app/hat/engine/portfolio/`) decides what each sleeve may do and with how much capital, and that no step of it can be skipped: a universe is a point-in-time set that never rewrites history, the regime state changes only after three consecutive days and never touches a sleeve's lifecycle, the health multiplier follows the drawdown bands, the allocator keeps every multiplier inside the policy bounds and every cap, and a sleeve reaches Live only through the guarded lifecycle with an event for every change. Requirement IDs (index: `docs/invariants.md`): (to 066), (PL1 to PL5),,.

## Properties

- `TLA RegimeConfirmation!ThreeDayRule` The confirmed regime state changes to a state only on the third consecutive day the raw classification equals it (and `ConfirmsAfterThree`: three equal days always confirm). Harm: one wild day moves capital in or out of the market, or a real change is never acknowledged.
- `TLA SleeveLifecycle!AllowedTransitions` Every lifecycle change is one of the seven allowed edges, its guard held before it (backtest gate, 20 shadow sessions, probation trigger, the owner's promotion with the failure gone, retirement after the period with the failure still true) and it comes with exactly one event of the same (from, to). Harm: a sleeve skips a stage, or a change leaves no audit trail.
- `TLA SleeveLifecycle!NoLiveWithoutEvidence` A Live sleeve has been through Backtested and Shadow since it last left Research. Harm: a retired or unproven sleeve returns to money without the full gate.
- `TLA SleeveLifecycle!RegimeNeverChangesLifecycle` A regime change never changes a sleeve's lifecycle state. Harm: a bear market retires a sound sleeve, or a bull market promotes an unproven one.
- `TLA SleeveLifecycle!ProbationOnHealthZero` After an allocation no Live sleeve has health 0, and every sleeve out of Live has weight 0. Harm: a broken sleeve keeps trading real money.
- `PBT universe.*` On the real membership rules: the members on a date are exactly the instruments with an interval holding it (delisted ones included for the dates they were in); an edit never changes the membership of an earlier date; the universe is (constituents plus includes) minus excludes; intervals stay ordered, non empty and non overlapping; leaving the universe blocks entries and never forces an exit; a seeded version is a draft that changes no membership until it is published. Harm: a backtest sees today's survivors, an edit rewrites history, or a universe change sells positions the strategy did not call for.
- `PBT regime.*` The confirmation matches an independent model for any length; the inputs read nothing dated after the day, the VIX percentile is in [0, 100) and monotone, a missing or unusable input fails with a stable code; the classification is total, follows the precedence Bear, Correction, Bull volatile, Bull quiet and never moves to a safer state when conditions worsen; the multiplier table never rises toward Bear and a replacement table is held within the policy bounds; the `regime` job stores one row per day with the raw state, the confirmed state and one event. Harm: the regime reads the future, overlapping rows give different states by code order, or worse conditions raise exposure.
- `PBT health.*` The multiplier is the full value under the first band, the half value from it to the second band and 0 (Probation) from the second band on, compared exactly at the edges, never rises with a deeper drawdown, and an unreadable equity pair fails instead of reading as healthy. Harm: a sleeve in a deep drawdown keeps full capital.
- `PBT allocator.*` On the real allocator and the nightly service on SQLite: weight = base x regime x health with both multipliers inside the bounds of the policy version in force at the trade date; a sleeve out of Live has weight 0; a weight never exceeds that product and equals it when no cap binds; capital = weight x equity rounded down and risk per trade = policy risk x regime x health; a Shadow or Probation sleeve keeps weight 0 with a nominal `virtual_capital` and `virtual_risk_pct` (risk x regime); `virtual_capital` comes from a pass of the same caps per sleeve, as if it alone were Live (base x regime) in place of its strategy's other sleeves beside the other strategies' real products, the other Shadow and Probation sleeves counting 0: it matches an oracle of the two caps, equals base x regime x equity when no cap binds, is the same with every other virtual sleeve removed, and the real weights are unchanged, and through `run_allocation` and `LiveContexts` its virtual entries are accepted on it while a real one is `no_allocation` (`allocator.virtual_allocation`, `allocator.virtual_entries_sized`); a multiplier moves at most 0.25 a day and a sleeve back from health 0 restarts at its target; every risk cluster stays within the cluster cap and the book within 1, with nothing redistributed; any number of sleeves in any order gives one row each; the allocation fails with no regime row and a rerun keeps the first rows; open positions are untouched. Harm: a sleeve gets more weight or risk than the policy allows, capital swings in one night, or a weight cut sells positions.
- `PBT lifecycle.*` On the real guards and `transition`: the backtest gate needs DSR probability of at least 0.95 and 200 trades, and (`backtest_refusal`) sessions spanning five years, the version's playbooks known, no known gap naming an enabled playbook, ambiguous days at most 10% of held sessions, the base weight in force and regime rule `policy`, each refused with its own code in that order and an absent fact failing; the shadow gate needs 20 trading sessions after the start; the Student-t quantile is checked against a numerical integration of the t density; the probation trigger is health 0 or an expectancy interval below zero from 50 trades; the benchmark is the backtest expectancy times one minus the discount; retirement needs 3 months or 50 shadow trades with the failure still true; every guard answer equals an independently written rule table; a transition changes the state and writes its event together or does neither; the results a decision reads stop at the trade date's close; the allocation moves a health-zero sleeve to Probation. Harm: an unproven sleeve goes live, a sound one is benched on noise, or the state and its audit trail disagree.
- `CONF portfolio.*` Only `service.transition` assigns `hat_bot.lifecycle_state` (by attribute, `setattr` or a bulk update); the regime, health, allocator, lifecycle and universe rules import no database, model, broker or data code; `hat_bot` pins a strategy version and a universe version, carries `lifecycle_state` with exactly the six lifecycle values and `risk_cluster`, and has no risk-number column. Harm: a second writer skips the lifecycle guard, a rule reads the database and the backtest applies a different rule, or a risk number bypasses the risk policy.
- `PBT admin.*` Through the owner's admin command line (`python -m app.hat.admin`) on SQLite with a fake broker and scripted backtest evidence: the first-deploy runbook takes a fresh database to a sleeve in Shadow that has a strategy version and a live universe, refuses `promote --to live` until the version itself is live, promotes the version only after 20 trading sessions of shadow, and records the owner's steps (`confirm-ddpi` among them) as `actor = user` with the allowlisted chat id and `via = cli` (`admin.first_deploy`); every refused command exits 2 with the fixed code of the rule that refused it and leaves every table unchanged, a universe publish naming an unknown sleeve included, as do a universe or draft inside a session, a CSV ISIN that differs from the instrument's, sleeves on an account that is not active, an undecodable file, a file outside `<HAT_DATA_ROOT>/admin/`, and a `confirm-ddpi` inside a session, dated in the future, with a blank note, repeated or naming an unknown account, and a `revoke-ddpi` of an unconfirmed or unknown account or with a blank note, an `add-sleeve` of an unknown strategy or on an account that is not active, a `resolve-action` inside a session or of an unknown position, and an owner's act (`assign-version` among them) without the owner's chat (`admin.refusal_writes_nothing`); with a strategy's sleeve Live, `add-sleeve` gives it a second sleeve (same account or another) that reaches Shadow on a new version while the Live one keeps the live version, every assignment and the promotion's move of the Live sleeve are recorded `sleeve.version_assigned` as the owner's, and the second sleeve is refused Live first on its version and then on the Live sleeve (`strategy_has_live_sleeve`, `admin.shadow_beside_live`); `revoke-ddpi`, inside or outside a session, with no trading calendar readable too, turns the account's DDPI gate off from that instant, keeps the dated reason, writes `broker.ddpi_revoked` as the owner's under the account's user (as `broker.ddpi_confirmed` is) and refuses a second revocation (`admin.ddpi_revoked`; the rig's user is not the settings' default, so an event filed under the default is seen). The postgres suite runs the same runbook through `main` on Postgres (`tests/hat/db/test_db_admin.py`). Harm: a fresh deploy never scans, a sleeve trades real money on an unproven version, or a refused release step is half applied.
- `CONF ops.admin_cli_calls_services` `backend/app/hat/admin.py` imports only the services it wraps (and what they need), calls no session method, builds no query and assigns no attribute; no module imports it, so no route reaches it. Harm: a setup command skips the guard a service enforces, or the owner's commands become an unauthenticated API.
- `PBT regime.strategy_rules` `close_above_sma_and_rising` is true exactly when the close is above its SMA and the SMA is higher than `rising_days` sessions earlier; `sma_fast_above_slow_both_rising` exactly when SMA fast is above SMA slow and both are above their value `rising_days` sessions earlier; a configured fast/slow rule without `rising_days` reads 3 sessions, one with it reads that many, and each gives what the function gives; a too-short series is false. Harm: the gate lets entries through in a falling market or blocks them in a rising one.
- `PBT dsr.evidence_gate_facts` The evidence carries the run's first and last session, held sessions and ambiguous days, the playbooks its known gaps name (every playbook of the version when it recorded no gap list), the version's playbooks (none when its config does not parse), a malformed fact absent rather than raised, its sleeve weight and regime rule, and the base weight of the version's own sleeve in the policy in force. Harm: the gate passes on a fact the run never recorded, or on another sleeve's weight.
- `PBT dsr.evidence_current_trials` The backtest evidence the lifecycle reads is the newest completed run of the sleeve's live version on its family, with the DSR deflated by the family's trial count and Sharpe variance at evaluation time (later failed runs and other versions counted), as `BacktestFacts`; no run is no evidence. Harm: a sleeve passes the Research to Backtested gate on a stale trial count.

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
- Finding kept visible: the planned-reallocation fix lives in `risk/kill.py` and `risk/controls.py`, covered by `verify-risk`; the allocator's capital cuts reach the kill only through the restated peak.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new sleeve: a `hat_bot` row and a `base_weight` entry in the policy; no code (the strategy name maps to its key through `SLEEVE_OF_STRATEGY`, an unknown name is its own key). `allocator.sleeve_count_agnostic` proves any number of rows works.
- A lifecycle rule: edit `lifecycle.py` (the guard and `EDGES`), `service.py` (the one writer) and `verification/tla/SleeveLifecycle.tla` together; keep a `\* code:` comment on every action, add a broken variant `SleeveLifecycle__<mode>.cfg` (with `EXPECT:` and the mode in the model's `ASSUME`), run both `run.sh` commands, then the properties.
- A regime rule or threshold: edit `regime.py`, `RegimeConfirmation.tla` (if the 3-day rule changes), the 05 section 3 text and `rules_v1_config`; the stored model version holds the multiplier table, so a new table is a new `hat_regime_model_version` row.
- Backtest evidence plugs in through `BacktestEvidence` in `service.py`, now returning `lifecycle.BacktestFacts` (DSR, trades, expectancy, trial count). The default `no_backtest_evidence` keeps Research to Backtested closed; the backtest's source is `app.hat.backtest.runs.backtest_evidence`, which `allocate_job` passes to `run_allocation` by default.
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
- `backend/app/hat/engine/portfolio/lifecycle.py`: `guard`, `backtest_gate`, `backtest_refusal`, `span_gate`, `ambiguous_gate`, `shadow_gate`, `t_quantile_95`, `probation_trigger`, `discounted_benchmark`, `retire_gate`
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
- `backend/tests/hat/hat_admin_support.py`
- `backend/tests/hat/db/test_db_admin.py`
- `backend/tests/hat/conformance/test_conf_ops.py`
- `backend/app/hat/engine/regime_gate.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `98d8e01d839c0ea6428cfbe5b573e41caa6a22f619adce6319a4d0b66627716e`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model, then the break was removed and the check returned to passing.

### TLA RegimeConfirmation!ThreeDayRule

Break: `verification/tla/broken/RegimeConfirmation__immediate.cfg` selects the variant where the confirmed state follows the raw state at once.

```
Error: Invariant ThreeDayRule is violated.
State 1: <Initial predicate>
State 2: <Bootstrap line 57>
State 3: <Switch line 76>
```

### TLA SleeveLifecycle!AllowedTransitions

Break: `verification/tla/broken/SleeveLifecycle__no_dsr_guard.cfg` selects the variant where Research moves to Backtested without the backtest gate.

```
Error: Invariant AllowedTransitions is violated.
State 1: <Initial predicate>
State 2: <ToBacktested line 110>
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

### PBT regime.strategy_rules

Break: in `backend/app/hat/engine/regime_gate.py`: close_above_sma_and_rising accepts a close equal to its SMA.

```
E   AssertionError: assert True is False
FAILED tests/hat/properties/test_regime.py::test_strategy_rules - AssertionEr [cut]
============================== 1 failed in 0.57s ===============================
```

### PBT dsr.evidence_gate_facts

Break: in `backend/app/hat/backtest/runs.py`: a run without a gap list reads as naming no playbook.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:963: in test_evidence_gate_facts
    assert facts.gapped_playbooks == (
E   AssertionError: assert frozenset() == frozenset({'default'})
E     Extra items in the right set:
E     'default'
E     Full diff:
E     - frozenset({
E     ?           ^
E     + frozenset()
E     ?           ^
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

### CONF ops.admin_cli_calls_services

Break: in `backend/app/hat/admin.py`: `_publish_universe` sets `named.name` itself.

```
E   AssertionError: assert ['assigns .name'] == []
E     Left contains one more item: 'assigns .name'
== cli writes a column: 1 failed, 1 passed, 2 deselected
```
