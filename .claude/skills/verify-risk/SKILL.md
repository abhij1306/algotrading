---
name: verify-risk
description: How to run, read and update the checks behind Hire a Trader's risk authority (position sizing, caps and invariants, exposure ladder, kill switches and manage-only, policy-change rules, mode changes, the single `evaluate_signal` decision). Use before and after any change to backend/app/hat/risk, verification/tla/KillSwitch.tla or the risk properties and conformance checks.
---

# verify-risk

Checks that `evaluate_signal` (`backend/app/hat/risk/service.py`) is the one place a HAT order is sized and gated: the policy numbers come from the effective policy version, the size is exact, every cap and invariant rejects what it must, exits are never blocked, kill switches and pauses act as the spec says, and risk can only be raised through the cooling-off and evidence gate. Specs: `docs/specs/hat/06-risk-and-governance.md` (HAT-RISK-010 to 104, HAT-GOV-080 to 094), `03-schema.md` (HAT-SCH-033/034/073/086), `01-decisions.md` (B13 to B26), `11-verification.md` section 3.

## Properties

- `TLA KillSwitch!TripOnDrawdown` An account-level drawdown of 15% (or a bot's 10%) is seen and no kill is set, so the bots keep opening positions through a crash. Harm: the account loses more than the owner's stop-loss for the whole book.
- `TLA KillSwitch!TripImpliesManageOnly` While the account kill or the bot's own kill is tripped, no entry is placed for that bot. Harm: a kill that does not stop new positions is not a kill.
- `TLA KillSwitch!KillCancelsApprovals` A drawdown or manual kill ends every approval still waiting for a decision, so a stale Telegram button cannot open a position after the kill. Harm: a confirm tap minutes after a kill places an order.
- `TLA KillSwitch!DataHealthManageOnly` While the data or broker health check is failing, no entry is placed, even before the stored switch has been updated. Harm: entries on stale or unconfirmed data.
- `TLA KillSwitch!NoAutoRearm` A drawdown or manual kill is lifted only by the confirmed second step of a user, never by recovery, a resume or time. Harm: the bots resume trading after a crash with nobody having decided to.
- `TLA KillSwitch!PauseBlocksEntriesOnly` A paused bot places no new entry, and its exits, stop moves and reconciliation continue. Harm: a pause that still opens positions, or one that strands an open position.
- `TLA KillSwitch!HealthAutoRearm` A health kill clears by itself as soon as the health inputs pass, without a user. Harm: a brief data glitch freezes the bot until someone notices.
- `TLA KillSwitch!ExitsContinue` Whatever is tripped, paused or failing, a due exit is eventually sent. Harm: a kill or a health failure traps an open position.
- `PBT sizing.*` On the real `size_position` and the real `evaluate_signal`: the size is exactly the floor of the smallest of the four terms (risk, position share, 20-day volume, cash), in Decimal with no float; the risk taken never exceeds `equity x risk_pct x ladder multiplier`; a wider stop never grows the size and more equity never shrinks it; a zero size is a skip with a named reason; a stop at or above the entry is refused; the regime and health multipliers are applied once (they are already inside the allocation's `risk_pct`); every number comes from the policy version in force at the signal time, so a pending increase never sizes a trade. Harm: a position larger than the owner's risk budget, or one sized with a limit nobody approved.
- `PBT caps.*` On the real decision: the 3% global open risk, positions per bot and level, positions per industry, entries and stop-outs per day, the regime gate, no add to a loser and no pyramiding, a stop strictly below the entry, no entry on a stale feed or failed data check, no entry outside the calendar's session (DEV_MODE never opens it; an unknown date fails closed; a special session such as Muhurat is open from its own open to its own close), the max-chase rule, the price band (an entry more than 20% from the last price is `price_out_of_band`; a band width that is not a finite number of at least zero is `missing_input`), HAT's own kill switches as the only halt (no legacy halt flag is read); exits and stop moves accepted in every state; the decision is total (any input gives ACCEPT or a REJECT with a reason from the enumerated set, never an exception) and fails closed on a missing, float, NaN or infinite number; a policy change is classified increase or decrease from every field (a higher multiplier ceiling or base weight is an increase), an increase needs 50 live (non-shadow) trades inside tolerance, a change is requested outside market hours, and each request and each effect writes its event. Harm: any one cap missing or off by one lets the book take more risk than the policy allows, or an exit is trapped.
- `PBT exposure.*` The L1 to L3 ladder: a level moves up after the policy's winners in a row and down after its losers, one level at a time, never past L1 or L3; a level move resets both counters; the level's position limit and risk multiplier are the ones the decision applies; shadow (virtual) trades never move it; a trade closed at 0 R counts as a loss. Harm: paper wins unlock real exposure, or a losing streak does not cut it.
- `PBT kill.*` Kill state from rows: a (scope, bot) is tripped exactly when the latest row of its risk channel (drawdown, manual) or of its health channel (data, broker) is a trip, so an automatic health re-arm never hides a tripped drawdown kill; another bot's rows never count. On the real service on SQLite, random trips, health changes, re-arms, pauses and resumes keep a bot manage-only exactly when the model says, a re-arm needs a user and a second confirmation, a resume never lifts a kill, a drawdown or manual trip ends the undecided approvals it covers, and the nightly snapshot trips the account kill at 15% and a bot at 10% from the summed and per-bot equity. A planned reallocation is not a drawdown (HAT-RISK-066): with no trade results, any sequence of allocation capitals (cuts, rises, nights at zero) trips nothing and leaves no stored peak above its equity, and a cut together with a realised loss trips exactly when the loss reaches the drawdown share of the restated capital. Harm: a tripped kill that reads as armed, or one that a pause or a glitch quietly clears.
- `PBT calendar.*` The next session open is the earliest open strictly after the instant, holidays and weekends skipped; a mode change is queued only outside market hours and takes effect at that next open, never before; the bot's mode is untouched until then. Harm: a mode flips to auto in the middle of a session.
- `CONF risk.*` The policy document's fields are exactly the 06 section 1 table, the seed equals the table's seed column field by field, no bot, strategy or version column carries a risk number, `size_position` is called from one place only, no risk function takes an override or bypass switch, HAT's pinned safety properties (exits, regime gate, stop, staleness, session, kill, caps, tag and fail-closed broker rules) still exist as Hypothesis properties, match their recorded digests and pass, the legacy manual-trading modules are gone, and no HAT module names the legacy VCP halt flag. Harm: a second sizing path, a hidden override, a safety property weakened to fit a change, or a manual-trading path around HAT's risk decision.
- `DB risk.*` On Postgres, the real policy, kill and snapshot services write rows the database constraints accept (cooling-off span, kill scope, one snapshot per bot and day), and a service that forgets its own rule is refused. Harm: the code and the database disagree about what a legal row is.

## Run

```bash
# Java 17+ is needed (Temurin 21 is what CI uses): export JAVA_HOME=/path/to/jdk
verification/tla/run.sh KillSwitch              # the model must pass
verification/tla/run.sh --broken KillSwitch     # every broken variant must fail with its EXPECT line
cd backend
CI=1 python -m pytest tests/hat/properties -q --no-cov          # sizing, caps, exposure, kill, calendar, mode
python -m pytest tests/hat/conformance -q --no-cov              # CONF risk.* and the harness
# database checks need Postgres (a throwaway database whose name ends in _test):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  python -m pytest -m postgres tests/hat/db -q --no-cov
```

## Bug or model error

- A failing property prints the falsifying example (for sizing, the nine inputs; for caps, the context fields that differ from the valid one; for the kill service, the operation list). Reproduce it with the real function. If the real code breaks the property, that is a finding: report the counterexample in the PR and fix the code. Never weaken the property or narrow its strategy to get past it.
- A TLC counterexample names each action (`SnapshotAccount`, `PlaceEntry`, `HealthFlip`, `Pause`, `Resume`); each action has a `\* code: backend/app/hat/risk/...py:function` comment naming the function it models. If the real function can take those steps in that order, it is a bug in the code. If it cannot (a guard or transaction the model leaves out), fix the model and say so under Scope changes.
- A failing `CONF risk.initial_seed_equals_spec` names the field that differs from the 06 section 1 table: the spec is the source of truth, fix `SEED_POLICY` (or change the spec in a separate, called-out change).
- `CONF risk.hat_safety_properties_pinned` fails when a pinned safety property was edited (digest), is no longer a `@given` property, or fails. Do not edit the property: stop and explain which safety behaviour changed; re-pin (update `SAFETY_DIGESTS`) only for a deliberate, reviewed change, and say so in the PR.
- Finding kept visible: HAT's own `MarketHoursPolicy` (`app/hat/data/sessions.py`) has no `DEV_MODE` bypass, and `evaluate_signal` requires an OPEN session from the trading calendar, so a dev flag never opens a HAT entry.
- Finding kept visible: the price band is a constant (`MAX_PRICE_DEVIATION_PCT`, 20%), not a policy field, because 06 section 1 fixes the policy document's fields (HAT-RISK-010); making it a field is a spec change.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new cap or invariant: add a predicate in `risk/caps.py`, call it from `risk/decision.py` in the ordered entry checks, add a `Reason` code, a `test_<name>` in `tests/hat/properties/test_caps.py` that drives `decide`, and a broken variant of the new check under "Scratch breaks".
- A new policy field: change `SEED_POLICY`, `POLICY_FIELDS`, `parse_policy`, `_permissive_fields` (its direction), the 06 section 1 table and the CONF tests together; they read the table, so they fail until all agree.
- A kill or pause rule: edit `verification/tla/KillSwitch.tla` and `risk/kill.py` / `risk/controls.py` together; keep a `\* code:` comment on every action, add a broken variant `KillSwitch__<mode>.cfg` (with `EXPECT:` and the mode in the model's `ASSUME`), run both `run.sh` commands, then the properties.
- New HAT code that needs a size: call `evaluate_signal` from `app/hat/risk/service.py`; `CONF risk.single_sizing_path` fails if anything else calls `size_position`.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-risk` prints the digest.

## Coverage

TLA+ model and broken variants:
- `verification/tla/KillSwitch.tla` and `verification/tla/KillSwitch.cfg`
- `verification/tla/broken/KillSwitch__no_trip_on_drawdown.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_kill.cfg`, `verification/tla/broken/KillSwitch__kill_keeps_approvals.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_live_health.cfg`, `verification/tla/broken/KillSwitch__auto_rearm_on_recovery.cfg`, `verification/tla/broken/KillSwitch__resume_clears_kill.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_pause.cfg`, `verification/tla/broken/KillSwitch__health_manual_rearm.cfg`, `verification/tla/broken/KillSwitch__exit_blocked_when_killed.cfg`

Code under check:
- `backend/app/hat/risk/sizing.py`: `size_position`, `exact`
- `backend/app/hat/risk/decision.py`: `decide`, `Reason`, `SignalContext`
- `backend/app/hat/risk/caps.py`: the cap predicates, `effective_max_chase_pct`
- `backend/app/hat/risk/exposure.py`: `apply_outcome`, `apply_trade`, `max_positions`
- `backend/app/hat/risk/kill.py`: `current_state`, `kill_state_of`, `drawdown_breached`, `tripped_since`, `account_series`, `rebase_peak`, `adjusted_series`
- `backend/app/hat/risk/controls.py`: `trip_kill`, `rearm_kill`, `sync_health`, `evaluate_drawdown_kills`, `pause_bot`, `resume_bot`, `queue_mode_change`, `record_closed_trade`, `snapshot_bots`
- `backend/app/hat/risk/policy_service.py`: `seed_initial_policy`, `request_policy_change`, `apply_due_versions`
- `backend/app/hat/risk/sessions.py`: `SessionCalendar`, `next_session_open`
- `backend/app/hat/risk/modes.py`: `plan_mode_change`, `effective_mode`
- `backend/app/hat/risk/policy.py`: `parse_policy`, `SEED_POLICY`, `effective_version`, `classify_direction`, `build_gate_evidence`, `increase_gate_ok`
- `backend/app/hat/risk/service.py`: `evaluate_signal`, `price_band_ok`, `market_open_at`

Checks:
- `backend/tests/hat/hat_risk_support.py`
- `backend/tests/hat/properties/test_sizing.py`
- `backend/tests/hat/properties/test_caps.py`
- `backend/tests/hat/properties/test_exposure.py`
- `backend/tests/hat/properties/test_kill.py`
- `backend/tests/hat/properties/test_calendar.py`
- `backend/tests/hat/properties/test_mode_change.py`
- `backend/tests/hat/conformance/test_conf_risk.py`
- `backend/tests/hat/db/test_db_risk.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `0cf2eae6ab9307dffb7950ebf7e1462733cc7567`
Covered digest: `20e07afbe0e69fba882129c3fb87b2ad8545cc8871cd7d4d0b7908d3ec3d4a72`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `KillSwitch` has no `FlattenNeedsConfirm` or `FlattenExitOnly`: the flatten command is PR 16 code and the model's actions must map to existing functions. The kill, pause, health and approval rules of HAT-RISK-062 to 065 and HAT-GOV-081 are modelled.
- `KillCancelsApprovals` covers approvals that are still undecided (`pending`). An approval already taken has left the approval state and still passes the entry gate at placement, which the kill and manage-only checks stop (`TripImpliesManageOnly`). The spec text says "pending".
- The ExecutionRouter TLA names of HAT-DEC-B20, HAT-GOV-084 and HAT-SCH-086 (`NoAddToOpenPosition`, `ModeChangeNextSession`) belong to the router model of PR 15 and are listed in `KNOWN_GAPS`; the same promises are checked on the real code by `caps.no_pyramiding`, `caps.no_add_to_loser`, `mode.mode_change_next_session` and `calendar.next_session_open`.
- Kill state is read per (scope, bot, channel) rather than from one latest row per scope: with one latest row an automatic health re-arm would hide a tripped drawdown or manual kill. A bot is manage-only while any channel is tripped.
- `kill_state` treats an unknown or non-active bot as paused (fail closed).
- The ladder counts a trade closed at exactly 0 R as a loss (the conservative reading of "consecutive losers").
- `kill.tripped_since` and the equity series read snapshot equity as allocation capital plus cumulative realised P&L (06 section 2). PR 9 closed the follow-up this entry used to carry (HAT-RISK-066): `kill.rebase_peak` restates the previous peak by the ratio of the new to the old equity when the allocation capital changes, `snapshot_bots` stores that restated peak, `evaluate_drawdown_kills` rebuilds the account series from the allocation history (`kill.adjusted_series`) and does not measure a snapshot with zero allocated capital. `kill.tripped_since` itself is unchanged, so the pinned `kill_thresholds` property still holds.
- The property `caps.kill_thresholds` is checked against an independently written series oracle and on the service through `kill.drawdown_service`.
- The service-level kill properties use SQLite in memory (foreign keys not enforced), and the Postgres checks in `test_db_risk.py` cover the constraints.
- This PR refreshed the digest of `verify-governance` (it covers `risk/policy.py`, which gained the policy record, with the cooling-off functions untouched) and of `verify-scheduler` (it covers `test_conf_verification.py`, where `MERGED_PRS` gained 10 and `KNOWN_GAPS` two ExecutionRouter names).

- K4 deleted the legacy manual-trading code and the digest pin of its risk tests (`test_risk__existing_suite_green`, HAT-RISK-103). `risk.hat_safety_properties_pinned` pins HAT's own safety properties in its place, `risk.manager_reads_no_hat_state` goes with the legacy risk manager, and `risk.no_manual_trading_path` checks that the deleted modules stay deleted. The spec row of HAT-RISK-103 was rewritten to match.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model (HAT-VER-072), then the break was removed and the check returned to passing.

### TLA KillSwitch!TripOnDrawdown

Break: `verification/tla/broken/KillSwitch__no_trip_on_drawdown.cfg` selects the model variant where a drawdown breach is read but the kill is not tripped.

```
Error: Invariant TripOnDrawdown is violated.
State 1: <Initial>
State 2: <SnapshotAccount>
```

### TLA KillSwitch!TripImpliesManageOnly

Break: `verification/tla/broken/KillSwitch__entry_ignores_kill.cfg` selects the model variant where the entry gate ignores the account and bot kills.

```
Error: Invariant TripImpliesManageOnly is violated.
State 1: <Initial>
State 2: <SnapshotAccount>
State 3: <PlaceEntry>
```

### TLA KillSwitch!KillCancelsApprovals

Break: `verification/tla/broken/KillSwitch__kill_keeps_approvals.cfg` selects the model variant where a trip leaves undecided approvals pending.

```
Error: Invariant KillCancelsApprovals is violated.
State 1: <Initial>
State 2: <CreateApproval>
State 3: <SnapshotAccount>
```

### TLA KillSwitch!DataHealthManageOnly

Break: `verification/tla/broken/KillSwitch__entry_ignores_live_health.cfg` selects the model variant where the entry gate reads only the stored health kill, not the live health inputs.

```
Error: Invariant DataHealthManageOnly is violated.
State 1: <Initial>
State 2: <HealthFlip>
State 3: <PlaceEntry>
```

### TLA KillSwitch!NoAutoRearm

Break: `verification/tla/broken/KillSwitch__auto_rearm_on_recovery.cfg` selects the model variant where the snapshot re-arms the account kill when equity recovers, with no second step.

```
Error: Invariant NoAutoRearm is violated.
State 1: <Initial>
State 2: <SnapshotAccount>
State 3: <SnapshotAccount>
```

Break: `verification/tla/broken/KillSwitch__resume_clears_kill.cfg` selects the model variant where resume also re-arms a bot kill.

```
Error: Invariant NoAutoRearm is violated.
State 1: <Initial>
State 2: <SnapshotBot>
State 3: <Pause>
State 4: <Resume>
```

### TLA KillSwitch!PauseBlocksEntriesOnly

Break: `verification/tla/broken/KillSwitch__entry_ignores_pause.cfg` selects the model variant where the entry gate ignores a pause.

```
Error: Invariant PauseBlocksEntriesOnly is violated.
State 1: <Initial>
State 2: <Pause>
State 3: <PlaceEntry>
```

### TLA KillSwitch!HealthAutoRearm

Break: `verification/tla/broken/KillSwitch__health_manual_rearm.cfg` selects the model variant where the health kill never clears once tripped.

```
Error: Temporal properties were violated.
State 1: <Initial>
State 2: <PlaceEntry>
State 3: <PlaceEntry>
State 4: <SnapshotBot>
State 5: <SnapshotBot>
State 6: <HealthFlip>
State 7: <HealthEval>
State 8: <HealthFlip>
```

### TLA KillSwitch!ExitsContinue

Break: `verification/tla/broken/KillSwitch__exit_blocked_when_killed.cfg` selects the model variant where an exit is gated like an entry.

```
Error: Temporal properties were violated.
State 1: <Initial>
State 2: <PlaceEntry>
State 3: <ExitDue>
State 4: <CreateApproval>
State 5: <SnapshotAccount>
State 6: <SnapshotBot>
State 7: <HealthFlip>
State 8: <SnapshotBot>
```

### PBT sizing.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/sizing.py` (sizing.formula_exact): liquidity term rounded to nearest instead of floored.

```
E assert False
E + where False = <function test_formula_exact.<locals>.fits at 0x7f92253c33d0>(1)
E Failing test case: test_formula_exact(
E inp=SizingInputs(equity=Decimal('1000.00'),
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.floats_rejected): a float input is accepted.

```
E Failed: DID NOT RAISE MissingInputError
E Failing test case: test_floats_rejected(
E # The test always failed when commented parts were varied together.
E inp=SizingInputs(equity=Decimal('1000.00'),
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.risk_bounded): the risk term doubled.

```
E AssertionError: assert (1 * (Decimal('2.00') - Decimal('0.01'))) <= ((Decimal('1000.00') * Decimal('0.0010')) * Decimal('1.0'))
E + where Decimal('2.00') = SignalContext(signal_id=UUID('3497e468-f33e-4b39-80cc-7318ab5f8dcb'), bot_id=UUID('00000000-0000-7000-8000-0000000000b...neInfo(key='Asia/Kolk
E + and Decimal('0.01') = SignalContext(signal_id=UUID('3497e468-f33e-4b39-80cc-7318ab5f8dcb'), bot_id=UUID('00000000-0000-7000-8000-0000000000b...neInfo(key='Asia/Kolkat
E + and Decimal('1000.00') = SignalContext(signal_id=UUID('3497e468-f33e-4b39-80cc-7318ab5f8dcb'), bot_id=UUID('00000000-0000-7000-8000-0000000000b...neInfo(key='Asia/Kol
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.monotone): the risk term wraps at 1000 shares.

```
E AssertionError: assert 1 <= 0
E + where 1 = SizingResult(quantity=1, binding='liquidity', risk_pct_used=Decimal('0.0000515'), terms={'risk': 10, 'position': 11, 'liquidity': 1, 'cash': None}, reason=N
E + where SizingResult(quantity=1, binding='liquidity', risk_pct_used=Decimal('0.0000515'), terms={'risk': 10, 'position': 11, 'liquidity': 1, 'cash': None}, reason=None)
E + and 0 = SizingResult(quantity=0, binding='risk', risk_pct_used=Decimal('0.00515'), terms={'risk': 0, 'position': 11, 'liquidity': 1, 'cash': None}, reason='size_zero_
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.wider_stop_never_grows): risk divided by the stop price, not the stop distance.

```
E AssertionError: assert 1 <= 0
E + where 1 = SizingResult(quantity=1, binding='risk', risk_pct_used=Decimal('0.00005'), terms={'risk': 1, 'position': 1, 'liquidity': 1, 'cash': None}, reason=None).quan
E + where SizingResult(quantity=1, binding='risk', risk_pct_used=Decimal('0.00005'), terms={'risk': 1, 'position': 1, 'liquidity': 1, 'cash': None}, reason=None) = size_p
E + where SizingInputs(equity=Decimal('5000.00'), allocation_risk_pct=Decimal('0.0001'), level_risk_mult=Decimal('0.5'), entry_p...mal('0.25'), p_max=Decimal('0.0004'), a
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.zero_skips_with_reason): a zero size carries no reason.

```
E AssertionError: assert not True
E + where True = RiskDecision(accepted=True, reason=None, quantity=0, stop=Decimal('0.01'), risk_amount=Decimal('0.00'), warnings=(), p...f-49338a2559e3'), binding='risk'
E Failing test case: test_zero_skips_with_reason(
E parts={'strategy_key': 'stage2-contraction-breakout',
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.stop_below_entry_required): a stop equal to the entry is allowed.

```
E decimal.DivisionByZero: [<class 'decimal.DivisionByZero'>]
E Failing test case: test_stop_below_entry_required(
E parts={'strategy_key': 'stage2-contraction-breakout',
E 'equity': Decimal('1000.00'),
```

Break: in `backend/app/hat/risk/decision.py` (sizing.adv_cap_from_policy): the liquidity cap is a constant, not the policy's.

```
E AssertionError: assert 1 == 2
E + where 1 = RiskDecision(accepted=True, reason=None, quantity=1, stop=Decimal('95'), risk_amount=Decimal('5'), warnings=('size_bou...85148e48e'), binding='liquidity', r
E Failing test case: test_adv_cap_from_policy(
E adv_cap=Decimal('0.02'),
```

Break: in `backend/app/hat/risk/decision.py` (sizing.multipliers_applied_once): the regime multiplier applied a second time.

```
FAILED tests/hat/properties/test_sizing.py::test_multipliers_applied_once - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/decision.py` (sizing.allocation_above_policy_rejected): the allocation ceiling check removed.

```
FAILED tests/hat/properties/test_sizing.py::test_allocation_above_policy_rejected - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/decision.py` (sizing.uses_effective_policy): the newest version used instead of the one in force.

```
FAILED tests/hat/properties/test_sizing.py::test_uses_effective_policy - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/policy.py` (sizing.pending_increase_never_applies): a version still in its cooling-off counts as in force.

```
E AssertionError: assert (False, 'size...9154fad3bbf')) == (False, 'size...c49b58f0a13'))
E
E At index 3 diff: UUID('672c0517-7c97-41f0-b932-09154fad3bbf') != UUID('cadb3203-fb4f-4749-853d-ac49b58f0a13')
E
```

Break: in `backend/app/hat/risk/sizing.py` (sizing.domain_errors_are_not_silent): an out-of-range fraction is accepted.

```
E Failed: DID NOT RAISE SizingInputError
E Failing test case: test_domain_errors_are_not_silent(
E inp=SizingInputs(equity=Decimal('1000.00'),
E allocation_risk_pct=int_to_decimal(1),
```

### PBT caps.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/caps.py` (caps.global_open_risk): the candidate's own risk left out of the sum.

```
E AssertionError: assert None == <Reason.GLOBAL_OPEN_RISK: 'global_open_risk'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...4-ab15316d56e0'), binding='risk'
E + and <Reason.GLOBAL_OPEN_RISK: 'global_open_risk'> = Reason.GLOBAL_OPEN_RISK
E Failing test case: test_global_open_risk(
```

Break: in `backend/app/hat/risk/caps.py` (caps.max_positions): limit off by one.

```
E AssertionError: assert None == <Reason.MAX_POSITIONS: 'max_positions'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=250, stop=Decimal('95'), risk_amount=Decimal('1250'), warnings=(), p...4-53a366a4c8d1'), binding='risk'
E + and <Reason.MAX_POSITIONS: 'max_positions'> = Reason.MAX_POSITIONS
E Failing test case: test_max_positions(
```

Break: in `backend/app/hat/risk/caps.py` (caps.industry): limit off by one.

```
E AssertionError: assert None == <Reason.INDUSTRY_CAP: 'industry_cap'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...5-6f3383e5d63e'), binding='risk'
E + and <Reason.INDUSTRY_CAP: 'industry_cap'> = Reason.INDUSTRY_CAP
E Failing test case: test_industry(
```

Break: in `backend/app/hat/risk/caps.py` (caps.daily_entries): limit off by one.

```
E AssertionError: assert 4 <= 3
E + where 3 = RiskPolicy(risk_pct=Decimal('0.0025'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), health_zero_d
E Failing test case: test_daily_entries(
E triggers=4,
```

Break: in `backend/app/hat/risk/caps.py` (caps.daily_stopouts): limit off by one.

```
E AssertionError: assert None == <Reason.STOPOUT_CAP: 'stopout_cap'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...0-d2bc10b12d49'), binding='risk'
E + and <Reason.STOPOUT_CAP: 'stopout_cap'> = Reason.STOPOUT_CAP
E Failing test case: test_daily_stopouts(
```

Break: in `backend/app/hat/risk/decision.py` (caps.exits_never_blocked): an exit is refused under an account kill.

```
E AssertionError: assert False
E + where False = RiskDecision(accepted=False, reason='missing_input', quantity=None, stop=None, risk_amount=None, warnings=(), policy_version_id=None, binding=None, risk
E Failing test case: test_exits_never_blocked(
E # The test always failed when commented parts were varied together.
```

Break: in `backend/app/hat/risk/caps.py` (caps.regime_gate_blocks_entries): gate always open.

```
E AssertionError: assert None == <Reason.REGIME_GATE: 'regime_gate'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...b-786b84abe68b'), binding='risk'
E + and <Reason.REGIME_GATE: 'regime_gate'> = Reason.REGIME_GATE
E Failing test case: test_regime_gate_blocks_entries(
```

Break: in `backend/app/hat/risk/kill.py` (caps.kill_thresholds): drawdown compared with > instead of >=.

```
E AssertionError: assert False
E + where False = <function drawdown_breached at 0x7f7b8638e6c0>(Decimal('0.8500'), Decimal('1.00'), Decimal('0.15'))
E + where <function drawdown_breached at 0x7f7b8638e6c0> = kill.drawdown_breached
E + and Decimal('0.15') = RiskPolicy(risk_pct=Decimal('0.0025'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), h
```

Break: in `backend/app/hat/risk/kill.py` (caps.kill_thresholds): snapshots before the re-arm still count.

```
E AssertionError: assert True is False
E + where True = <function tripped_since at 0x7f2128f86cf0>([EquityPoint(at=datetime.datetime(2026, 10, 21, 10, 30, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')), equity=
E + where <function tripped_since at 0x7f2128f86cf0> = kill.tripped_since
E Failing test case: test_kill_thresholds(
```

Break: in `backend/app/hat/risk/kill.py` (caps.kill_isolation): any bot's kill stops every bot.

```
E AssertionError: assert not True
E + where True = KillState(account_tripped=False, bot_tripped=True, health_tripped=False, paused=False).manage_only
E + where KillState(account_tripped=False, bot_tripped=True, health_tripped=False, paused=False) = <function kill_state_of at 0x7f77d0a69e80>([KillRow(scope='bot', bot_id
E + where <function kill_state_of at 0x7f77d0a69e80> = kill.kill_state_of
```

Break: in `backend/app/hat/risk/decision.py` (caps.no_add_to_loser): the loser check removed.

```
E AssertionError: assert 'position_exists' == <Reason.ADD_T...add_to_loser'>
E
E - add_to_loser
E + position_exists
```

Break: in `backend/app/hat/risk/decision.py` (caps.no_pyramiding): the open-position check removed.

```
FAILED tests/hat/properties/test_caps.py::test_no_pyramiding - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/caps.py and `app/hat/risk/sizing.py`` (caps.entry_requires_stop): a stop of zero or less allowed (the invariant and the sizing guard both relaxed).

```
E AssertionError: assert 0 < Decimal('0.00')
E + where Decimal('0.00') = RiskDecision(accepted=True, reason=None, quantity=1250, stop=Decimal('0.00'), risk_amount=Decimal('2500.00'), warnings...d-04ca5ea1e704'), bin
E Failing test case: test_entry_requires_stop(
E pair=(Decimal('2.00'), Decimal('0.01')),
```

Break: in `backend/app/hat/risk/decision.py` (caps.stale_blocks_entry): the stale-feed check removed.

```
E AssertionError: assert None == <Reason.STALE_FEED: 'stale_feed'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...e-6028cf470144'), binding='risk'
E + and <Reason.STALE_FEED: 'stale_feed'> = Reason.STALE_FEED
E Failing test case: test_stale_blocks_entry(
```

Break: in `backend/app/hat/risk/decision.py` (caps.market_hours_only): the calendar session ignored (only the legacy clock check left).

```
E AssertionError: assert None == <Reason.MARKET_CLOSED: 'market_closed'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...b-423f7f943da9'), binding='risk'
E + and <Reason.MARKET_CLOSED: 'market_closed'> = Reason.MARKET_CLOSED
E Failing test case: test_market_hours_only(
```

Break: in `backend/app/hat/risk/decision.py` (caps.market_hours_only): DEV_MODE bypasses the session.

```
E File "/data/workspace/wt/hat/pr10-risk/backend/app/hat/risk/decision.py", line 16
E from __future__ import annotations
E ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E SyntaxError: from __future__ imports must occur at the beginning of the file
```

Break: in `backend/app/hat/risk/caps.py` (caps.max_chase_always): the locked-upper-circuit rule removed.

```
E AssertionError: assert None == <Reason.MAX_CHASE: 'max_chase'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=277, stop=Decimal('81.00'), risk_amount=Decimal('2493.00'), warnings...7-7a448c0411df'), binding='risk'
E + and <Reason.MAX_CHASE: 'max_chase'> = Reason.MAX_CHASE
E Failing test case: test_max_chase_always(
```

Break: in `backend/app/hat/risk/decision.py` (caps.hat_kill_is_authority): the account-kill check disabled.

```
tests/hat/properties/test_caps.py F
tests/hat/properties/test_caps.py:462: in test_hat_kill_is_authority
tests/hat/properties/test_caps.py:481: in test_hat_kill_is_authority
E   AssertionError: assert None == <Reason.KILL_ACCOUNT: 'kill_account'>
E    +  where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...4-4f5bcc206489'), binding='risk', risk_pct_used=Decimal('0
E   Failing test case: test_hat_kill_is_authority(
```

Break: in `backend/app/hat/risk/service.py` (caps.price_band): the band check always passes.

```
tests/hat/properties/test_caps.py F
tests/hat/properties/test_caps.py:491: in test_price_band
tests/hat/properties/test_caps.py:505: in test_price_band
E   AssertionError: assert None == <Reason.PRICE_OUT_OF_BAND: 'price_out_of_band'>
E    +  where None = RiskDecision(accepted=True, reason=None, quantity=10000, stop=Decimal('1.1979'), risk_amount=Decimal('121.0000'), warn...097cd915f'), binding='liquidity', risk_pct_used=Decimal('0
E    +  and   <Reason.PRICE_OUT_OF_BAND: 'price_out_of_band'> = Reason.PRICE_OUT_OF_BAND
```

Break: in `backend/app/hat/risk/service.py` (caps.price_band): an infinite or NaN band width is accepted.

```
tests/hat/properties/test_caps.py F
    | AssertionError: assert True is None
```

Break: in `backend/app/hat/risk/service.py` (caps.market_hours_only): the default policy ignores the calendar's special session.

```
tests/hat/properties/test_caps.py F
tests/hat/properties/test_caps.py:417: in test_special_session_follows_calendar
tests/hat/properties/test_caps.py:432: in test_special_session_follows_calendar
E   AssertionError: market_closed
```

Break: in `backend/app/hat/risk/decision.py` (caps.decision_total): only missing-input errors are caught.

```
FAILED tests/hat/properties/test_caps.py::test_decision_total - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/sizing.py` (caps.fail_closed): a missing number defaults to zero.

```
E AssertionError: assert 'stop_not_below_entry' == <Reason.MISSI...issing_input'>
E
E - missing_input
E + stop_not_below_entry
```

Break: in `backend/app/hat/risk/policy.py` (caps.policy_direction_classification): raising the multiplier ceiling not counted as an increase.

```
E AssertionError: assert 'decrease' == 'increase'
E
E - increase
E ? ^^
```

Break: in `backend/app/hat/risk/policy.py` (caps.increase_gate): shadow trades counted as live evidence.

```
E assert 1 == 0
E + where 0 = len([])
E Failing test case: test_increase_gate(
E # The test always failed when commented parts were varied together.
```

Break: in `backend/app/hat/risk/policy.py` (caps.increase_gate): the 50-trade minimum lowered to 1.

```
E AssertionError: assert 2 >= 50
E + where 2 = len([TradeOutcome(r_multiple=Decimal('0.00'), is_virtual=False), TradeOutcome(r_multiple=Decimal('0.00'), is_virtual=False)])
E Failing test case: test_increase_gate(
E raw=[(Decimal('0.00'), False), (Decimal('0.00'), False)],
```

Break: in `backend/app/hat/risk/policy_service.py` (caps.policy_change_emits_event): a change allowed inside market hours.

```
FAILED tests/hat/properties/test_caps.py::test_policy_change_inside_session_or_without_evidence_is_refused - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-
```

Break: in `backend/app/hat/risk/policy_service.py` (caps.policy_change_emits_event): an increase takes effect at the next open, no cooling-off.

```
E app.hat.risk.policy.CoolingOffError: a risk increase must take effect at least 7 days after request
E Failing test case: test_policy_change_emits_event(
E steps=[(
E 'raise',
```

Break: in `backend/app/hat/risk/policy_service.py` (caps.policy_change_emits_event): the request writes no event.

```
E assert 1 == 2
E + where 1 = len([<app.hat.models.events.HatEvent object at 0x7f9993b46750>])
E + and 2 = len([<app.hat.models.strategy.HatRiskPolicyVersion object at 0x7f99938ff250>, <app.hat.models.strategy.HatRiskPolicyVersion object at 0x7f99938fefd0>])
E Failing test case: test_policy_change_emits_event(
```

### PBT exposure.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/exposure.py` (exposure.ladder_transitions): moves up after one extra win.

```
E assert (0, 1, 0) == (1, 0, 0)
E
E At index 0 diff: 0 != 1
E
```

Break: in `backend/app/hat/risk/exposure.py` (exposure.ladder_transitions): a level move keeps the win counter.

```
E assert (2, 0) == (0, 0)
E
E At index 0 diff: 2 != 0
E
```

Break: in `backend/app/hat/risk/exposure.py` (exposure.level_applies): the level's position maximum ignored.

```
E AssertionError: assert 6 == 2
E + where 2 = min(6, 2)
E + where 6 = RiskPolicy(risk_pct=Decimal('0.0025'), p_max={'stage2-contraction-breakout': Decimal('0.20'), 'momentum-breakout-ep': ...f_dd=Decimal('0.08'), health_zero_d
E + and 2 = LadderLevel(max_positions=2, risk_mult=Decimal('0.5')).max_positions
```

Break: in `backend/app/hat/risk/exposure.py` (exposure.virtual_ignored): shadow trades move the ladder.

```
E AssertionError: assert LadderState(l...tive_losses=0) == LadderState(l...tive_losses=0)
E
E Omitting 2 identical items, use -vv to show
E Differing attributes:
```

Break: in `backend/app/hat/risk/exposure.py` (exposure.ladder_transitions): no cap at L3.

```
E IndexError: tuple index out of range
E Failing test case: test_level_never_exceeds_l3(
E wins=[ClosedTrade(r_multiple=Decimal('1'), is_virtual=False),
E ClosedTrade(r_multiple=Decimal('1'), is_virtual=False),
```

Break: in `backend/app/hat/risk/exposure.py` (exposure.ladder_transitions): a breakeven trade counts as a win.

```
FAILED tests/hat/properties/test_exposure.py::test_breakeven_counts_as_a_loss - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

### PBT kill.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/kill.py` (kill.current_state): one latest row across health and risk channels.

```
E AssertionError: assert 'tripped' == 'armed'
E
E - armed
E + tripped
```

Break: in `backend/app/hat/risk/kill.py` (kill.current_state): rows of other bots read.

```
E AssertionError: assert 'tripped' == 'armed'
E
E - armed
E + tripped
```

Break: in `backend/app/hat/risk/controls.py` (kill.current_state): a passing health check re-arms a drawdown kill.

```
FAILED tests/hat/properties/test_kill.py::test_kill_service_matches_model - ExceptionGroup: Hypothesis found 3 distinct failures. (3 sub-exceptions)
```

Break: in `backend/app/hat/risk/controls.py` (kill.current_state): a re-arm needs no second confirmation.

```
E Failed: DID NOT RAISE ControlError
E Failing test case: test_kill_service_matches_model(
E ops=[(
E 'rearm_unconfirmed',
```

Break: in `backend/app/hat/risk/controls.py` (kill.current_state): resume lifts the bot's kill.

```
E assert False == True
E + where False = KillState(account_tripped=False, bot_tripped=False, health_tripped=False, paused=False).bot_tripped
E Failing test case: test_kill_service_matches_model(
E ops=[('trip_bot', 0), ('pause', 0), ('resume', 0)],
```

Break: in `backend/app/hat/risk/controls.py` (kill.trip_cancels_approvals): a trip leaves pending approvals.

```
E AssertionError: assert None == 'expired'
E Failing test case: test_trip_cancels_pending_approvals(
E # The test always failed when commented parts were varied together.
E scope='account', # or any other generated value
```

Break: in `backend/app/hat/risk/controls.py` (kill.drawdown_service): the bot threshold read from the account setting.

```
E AssertionError: assert set() == {'0153f27d-d1...25050218e6c3'}
E
E Extra items in the right set:
E '0153f27d-d1d5-4c41-be06-25050218e6c3'
```

Break: in `backend/app/hat/risk/controls.py`: the old peak survives a reallocation. Check run: `test_kill.py::test_reallocation_is_not_drawdown`.

```
E   AssertionError: assert (Decimal('0.0200') <= (Decimal('0.0100') + Decimal('0.0001')) or Decimal('0.0100') == 0)
E    +  where Decimal('0.0200') = <app.hat.models.ops.HatDailyBotSnapshot object at 0x7ffb4bdaa490>.equity_peak
E    +  and   Decimal('0.0100') = <app.hat.models.ops.HatDailyBotSnapshot object at 0x7ffb4bdaa490>.equity
E    +  and   Decimal('0.0001') = D('0.0001')
E    +  and   Decimal('0.0100') = <app.hat.models.ops.HatDailyBotSnapshot object at 0x7ffb4bdaa490>.equity
E   Failing test case: test_reallocation_is_not_drawdown(
E       days=[(
E            D(2) / 100,
E            False,  # or any other generated value
FAILED tests/hat/properties/test_kill.py::test_reallocation_is_not_drawdown
```

Break: in `backend/app/hat/risk/controls.py`: the peak is reset to the equity at every snapshot. Check run: `test_kill.py::test_reallocation_does_not_hide_a_loss`.

```
E   AssertionError: assert Decimal('0.0300') >= Decimal('0.04')
E    +  where Decimal('0.0300') = <app.hat.models.ops.HatDailyBotSnapshot object at 0x7fcede310eb0>.equity_peak
E   Failing test case: test_reallocation_does_not_hide_a_loss(
E       capital=D(14) / 100,
E       keep=D(25) / 100,  # or any other generated value
E       loss_share=D(13) / 100,
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEOQRlBDQ==') as a decorator on your test function
FAILED tests/hat/properties/test_kill.py::test_reallocation_does_not_hide_a_loss
```

### PBT calendar.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/sessions.py` (calendar.next_session_open): the latest open returned instead of the next.

```
E AssertionError: assert datetime.datetime(2026, 10, 30, 3, 45, tzinfo=datetime.timezone.utc) == datetime.datetime(2026, 10, 12, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia
E Failing test case: test_next_session_open(
E at=datetime.datetime(2026, 10, 10, 0, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')),
E )
```

Break: in `backend/app/hat/risk/sessions.py` (calendar.next_session_open): a holiday is treated as a session.

```
E AssertionError: assert datetime.datetime(2026, 10, 22, 3, 45, tzinfo=datetime.timezone.utc) == datetime.datetime(2026, 10, 23, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia
E + where datetime.datetime(2026, 10, 22, 3, 45, tzinfo=datetime.timezone.utc) = next_session_open(SessionCalendar(days={datetime.date(2026, 10, 11): SessionDay(is_tradin
E + and datetime.datetime(2026, 10, 23, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) = <built-in method combine of type object at 0x1606018>((datetime.date(2026,
E + where <built-in method combine of type object at 0x1606018> = datetime.combine
```

Break: in `backend/app/hat/risk/modes.py` (calendar.next_session_open): a mode change allowed inside market hours.

```
E Failed: DID NOT RAISE ModeChangeError
E Failing test case: test_mode_change_next_session(
E to_mode='shadow', # or any other generated value
E at=datetime.datetime(2026, 10, 12, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')),
```

Break: in `backend/app/hat/risk/modes.py` (calendar.next_session_open): a queued change counts before its effective time.

```
FAILED tests/hat/properties/test_mode_change.py::test_effective_mode - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Break: in `backend/app/hat/risk/controls.py` (calendar.next_session_open): queueing a change also switches the bot at once.

```
E AssertionError: assert 'confirm' == 'shadow'
E
E - shadow
E + confirm
```

### CONF risk.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/policy.py` (risk.policy_schema_fields): a field dropped from the policy schema.

```
E AssertionError: assert {'adv_cap_pct...h_bands', ...} == {'adv_cap_pct..., 'kill', ...}
E
E Extra items in the left set:
E 'health_bands'
```

Break: in `backend/app/hat/models/strategy.py` (risk.no_risk_keys_in_strategy_schema): a risk_pct column on hat_bot.

```
E AssertionError: hat_bot.risk_pct
E assert 'risk_pct' not in {'S1', 'S2', 'account_drawdown_pct', 'adv_cap_pct', 'base_weight', 'bot_drawdown_pct', ...}
E + where 'risk_pct' = Column('risk_pct', Text(), table=<hat_bot>).name
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__no_risk_keys_in_strategy_schema - AssertionError: hat_bot.risk_pct
```

Break: in `backend/app/hat/risk/policy.py` (risk.initial_seed_equals_spec): seed risk_pct 0.0030.

```
E AssertionError: assert Decimal('0.0030') == Decimal('0.0025')
E + where Decimal('0.0030') = Decimal('0.0030')
E + and Decimal('0.0025') = _number('0.0025 (0.25%)')
E + where '0.0025 (0.25%)' = <function test_risk__initial_seed_equals_spec.<locals>.row at 0x7f32eb941220>('risk_pct')
```

Break: in `backend/app/hat/risk/policy_service.py` (initial_risk_policy_seed): seed row filed as an increase.

```
E AssertionError: assert (1, 'decrease') == (1, 'initial')
E
E At index 1 diff: 'decrease' != 'initial'
E
```

Break: in `backend/app/hat/risk/policy.py` (risk_policy_seed_bot_limits): S2 p_max 0.30.

```
E AssertionError: assert Decimal('0.30') == Decimal('0.25')
E + where Decimal('0.30') = Decimal('0.30')
E + and Decimal('0.25') = Decimal('0.25')
FAILED tests/hat/conformance/test_conf_risk.py::test_risk_policy_seed_bot_limits - AssertionError: assert Decimal('0.30') == Decimal('0.25')
```

Break: in `backend/app/services/risk_manager.py` (risk.single_sizing_path): a legacy risk manager file exists again.

```
tests/hat/conformance/test_conf_risk.py F                                [100%]
tests/hat/conformance/test_conf_risk.py:217: in test_risk__single_sizing_path
E   AssertionError: assert not True
E    +  where True = exists()
E    +    where exists = ((PosixPath('backend/app') / 'services') / 'risk_manager.py').exists
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__single_sizing_path
```

Break: in `backend/app/hat/risk/decision.py` (risk.no_gate_override_path): decide takes an override flag.

```
E AssertionError: ('decision.py', 'decide')
E assert not True
E + where True = any(<generator object test_risk__no_gate_override_path.<locals>.<genexpr> at 0x7fbf11a0d9a0>)
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__no_gate_override_path - AssertionError: ('decision.py', 'decide')
```

Break: in `backend/tests/hat/properties/test_caps.py` (risk.hat_safety_properties_pinned): a pinned property edited (`test_exits_never_blocked` ignores the pause input).

```
tests/hat/conformance/test_conf_risk.py F                                [100%]
tests/hat/conformance/test_conf_risk.py:361: in test_risk__hat_safety_properties_pinned
E   AssertionError: properties/test_caps.py::test_exits_never_blocked was edited: stop and explain, never weaken the property to fit a change
E   assert '5b37974e0337...cedff4fa0d43f' == '8274671f6c65...93aa0f4609e7b'
E     
E     - 8274671f6c65ec6b48cb77db76a0aa5282381752bbfaf64b6be93aa0f4609e7b
E     + 5b37974e03373c949c5eda12b2677613684462e25cd5d07b8cacedff4fa0d43f
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__hat_safety_properties_pinned
```

Break: in `backend/app/hat/risk/decision.py` (risk.hat_safety_properties_pinned): `EXIT_ACTIONS` emptied, so an exit is treated as an entry and can be refused; the pinned properties run and fail.

```
tests/hat/conformance/test_conf_risk.py F                                [100%]
tests/hat/conformance/test_conf_risk.py:373: in test_risk__hat_safety_properties_pinned
E   AssertionError: ime_gate_blocks_entries(strategy: str) -> None:
E                        ^^^
E     tests/hat/properties/test_caps.py:237: in test_regime_gate_blocks_entries
E         assert decide(valid_ctx(strategy_key=strategy, gate_on=False, action="exit")).accepted
E     E   AssertionError: assert False
E     E    +  where False = RiskDecision(accepted=False, reason='regime_gate', quantity=None, stop=None, risk_amount=None, warnings=(), policy_ver..._id=UUID('523a797b-dbfa-4bcd-bf6d-5bc0ed183e69'), b
```

Break: in `backend/app/services/order_execution_service.py` (risk.no_manual_trading_path): an order-execution module exists again.

```
tests/hat/conformance/test_conf_risk.py F                                [100%]
tests/hat/conformance/test_conf_risk.py:399: in test_risk__no_manual_trading_path
E   AssertionError: assert ['services/or...n_service.py'] == []
E     
E     Left contains one more item: 'services/order_execution_service.py'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__no_manual_trading_path
```

Break: in `backend/app/utils/helpers.py` (risk.no_manual_trading_path): a class named `ManualRiskManager` is defined.

```
tests/hat/conformance/test_conf_risk.py F                                [100%]
tests/hat/conformance/test_conf_risk.py:406: in test_risk__no_manual_trading_path
E   AssertionError: assert ['utils/helpers.py'] == []
E     
E     Left contains one more item: 'utils/helpers.py'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_risk.py::test_risk__no_manual_trading_path
```

Break: in `backend/app/hat/risk/decision.py` (risk.no_legacy_halt_read): a comment names the legacy halt flag.

```
tests/hat/conformance/test_conf_risk.py F
tests/hat/conformance/test_conf_risk.py:319: in test_risk__no_legacy_halt_read
E   AssertionError: assert [('hat/risk/d...DING_HALTED')] == []
E     Left contains one more item: ('hat/risk/decision.py', 'TRADING_HALTED')
```

### DB risk.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/policy_service.py` (risk.policy_service_rows): an increase effective at the next open, not after 7 days.

```
E app.hat.risk.policy.CoolingOffError: a risk increase must take effect at least 7 days after request
FAILED tests/hat/db/test_db_risk.py::test_risk__policy_service_rows_pass_the_database - app.hat.risk.policy.CoolingOffError: a risk increase must take effect at least 7 d
```

Break: in `backend/app/hat/risk/controls.py` (risk.snapshot_job): a rerun inserts a second snapshot row.

```
E psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "hat_daily_bot_snapshot_pkey"
E DETAIL: Key (bot_id, trade_date)=(01a15ccd-577a-713b-8b32-15dbc2966983, 2026-10-19) already exists.
E sqlalchemy.exc.IntegrityError: (raised as a result of Query-invoked autoflush; consider using a session.no_autoflush block if this flush is occurring prematurely)
E (psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "hat_daily_bot_snapshot_pkey"
```

Break: in `backend/app/hat/risk/controls.py` (risk.kill_rows): an account kill written with a bot id.

```
E psycopg2.errors.CheckViolation: new row for relation "hat_kill_switch" violates check constraint "ck_hat_kill_switch_scope_bot"
E DETAIL: Failing row contains (01a15ccd-64d8-7018-8f57-4176c9ac77c1, account, 2f9e1eba-3061-4ad8-99af-0532002ae618, tripped, 2026-10-20 09:00:00+00, manual, null).
E sqlalchemy.exc.IntegrityError: (psycopg2.errors.CheckViolation) new row for relation "hat_kill_switch" violates check constraint "ck_hat_kill_switch_scope_bot"
E DETAIL: Failing row contains (01a15ccd-64d8-7018-8f57-4176c9ac77c1, account, 2f9e1eba-3061-4ad8-99af-0532002ae618, tripped, 2026-10-20 09:00:00+00, manual, null).
```

