---
name: verify-risk
description: How to run, read and update the checks behind Hire a Trader's risk authority (position sizing, caps and invariants, exposure ladder, kill switches and manage-only, policy-change rules, mode changes, the single `evaluate_signal` decision). Use before and after any change to backend/app/hat/risk, verification/tla/KillSwitch.tla or the risk properties and conformance checks.
---

# verify-risk

Checks that `evaluate_signal` (`backend/app/hat/risk/service.py`) is the one place a HAT order is sized and gated: the policy numbers come from the effective policy version, the size is exact, every cap and invariant rejects what it must, exits are never blocked, kill switches and pauses act as the spec says, and risk can only be raised through the cooling-off and evidence gate. Requirement IDs (index: `docs/invariants.md`): (to 104 to 094),, (B13 to B26).

## Properties

- `TLA KillSwitch!TripOnDrawdown` An account-level drawdown of 10% (or a bot's 10%; D128, the account was 15% before) is seen and no kill is set, so the bots keep opening positions through a crash. Harm: the account loses more than the owner's stop-loss for the whole book.
- `TLA KillSwitch!TripImpliesManageOnly` While the account kill or the bot's own kill is tripped, no entry is placed for that bot. Harm: a kill that does not stop new positions is not a kill.
- `TLA KillSwitch!KillCancelsApprovals` A drawdown or manual kill ends every approval still waiting for a decision, so a stale Telegram button cannot open a position after the kill. Harm: a confirm tap minutes after a kill places an order.
- `TLA KillSwitch!DataHealthManageOnly` While the data or broker health check is failing, no entry is placed, even before the stored switch has been updated. Harm: entries on stale or unconfirmed data.
- `TLA KillSwitch!HealthClearsOwnSource` Each health source (the quality gate, the live feed) clears only its own trip on the account key: the feed recovering never clears a failed gate. Harm: entries run on data the gate rejected once the feed reconnects at 09:15.
- `TLA KillSwitch!NoAutoRearm` A drawdown or manual kill is lifted only by the confirmed second step of a user, never by recovery, a resume or time. Harm: the bots resume trading after a crash with nobody having decided to.
- `TLA KillSwitch!PauseBlocksEntriesOnly` A paused bot places no new entry, and its exits, stop moves and reconciliation continue. Harm: a pause that still opens positions, or one that strands an open position.
- `TLA KillSwitch!HealthAutoRearm` A health kill clears by itself as soon as the health inputs pass, without a user. Harm: a brief data glitch freezes the bot until someone notices.
- `TLA KillSwitch!ExitsContinue` Whatever is tripped, paused or failing, a due exit is eventually sent. Harm: a kill or a health failure traps an open position.
- `PBT sizing.*` On the real `size_position` and the real `evaluate_signal`: the size is exactly the floor of the smallest of the four terms (risk, position share, 20-day volume, cash), in Decimal with no float; the risk taken never exceeds `equity x risk_pct x ladder multiplier`; a wider stop never grows the size and more equity never shrinks it; a zero size is a skip with a named reason; a stop at or above the entry is refused; the regime and health multipliers are applied once (they are already inside the allocation's `risk_pct`); every number comes from the policy version in force at the signal time, so a pending increase never sizes a trade. Harm: a position larger than the owner's risk budget, or one sized with a limit nobody approved.
- `PBT caps.*` On the real decision: the 1.5% global open risk (D128), positions per bot and level, positions per industry, entries and stop-outs per day, the regime gate, no add to a loser and no pyramiding, a stop strictly below the entry, no entry on a stale feed or failed data check, no entry outside the calendar's session (DEV_MODE never opens it; an unknown date fails closed; a special session such as Muhurat is open from its own open to its own close), the per-instrument cooldown (`cooldown_sessions` N > 0 refuses the instrument on the session of the bot's latest `hard_stop` of it and the N trading sessions after, counted on the calendar; a stop after the trade date is ignored; with N > 0 a missing or malformed history is `missing_input`, with N = 0 it is not read; a shorter cooldown is an increase and a value outside [0, 250] is refused; ), the max-chase rule, the price band (an entry more than 20% from the last price is `price_out_of_band`; a band width that is not a finite number of at least zero is `missing_input`), HAT's own kill switches as the only halt (no legacy halt flag is read); exits and stop moves accepted in every state; the decision is total (any input gives ACCEPT or a REJECT with a reason from the enumerated set, never an exception) and fails closed on a missing, float, NaN or infinite number; a policy change is classified increase or decrease from every field (a higher multiplier ceiling or base weight is an increase), an increase needs 50 live (non-shadow) trades inside tolerance, read by the service itself from the backtest runs (none, and so refused, without a completed run), a change is requested outside market hours, the owner's request without its chat id is refused before anything is written, and each request and each effect writes its event. Harm: any one cap missing or off by one lets the book take more risk than the policy allows, or an exit is trapped.
- `PBT exposure.*` The L1 to L3 ladder: a level moves up after the policy's winners in a row and down after its losers, one level at a time, never past L1 or L3; a level move resets both counters; the level's position limit and risk multiplier are the ones the decision applies; shadow (virtual) trades never move it; a trade closed at 0 R counts as a loss. Harm: paper wins unlock real exposure, or a losing streak does not cut it.
- `PBT kill.*` Kill state from rows: a (scope, bot) is tripped exactly when the latest row of its risk stream (drawdown, manual together) or of one of its health reasons (`data_health`, `feed_health`, `broker_health`, each its own stream) is a trip, so an automatic health re-arm never hides a tripped drawdown kill nor another source's health trip; another bot's rows never count. On the real service on SQLite, random trips, health changes (the account's from the gate and from the feed apart), re-arms, pauses and resumes keep a bot manage-only exactly when the model says, a re-arm needs a user and a second confirmation, a resume never lifts a kill, a drawdown or manual trip ends the undecided approvals it covers, and the nightly snapshot trips the account kill at 10% and a bot at 10% (D128; the account was 15% before) from the summed and per-bot equity. A planned reallocation is not a drawdown: with no trade results, any sequence of allocation capitals (cuts, rises, nights at zero) trips nothing and leaves no stored peak above its equity, and a cut together with a realised loss trips exactly when the loss reaches the drawdown share of the restated capital; a snapshot of day d is measured on the bot's latest allocation on or before d, and its capital for restating the peak and the account series is the one it used (equity less realised P&L), so an allocation written after it, or a night without one, reads as no loss. Harm: a tripped kill that reads as armed, or one that a pause or a glitch quietly clears.
- `PBT calendar.*` The next session open is the earliest open strictly after the instant, holidays and weekends skipped; a mode change is queued only outside market hours and takes effect at that next open, never before; the bot's mode is untouched until then. Harm: a mode flips to auto in the middle of a session.
- `PBT policy.*` The startup upkeep (`policy_upkeep.ensure_hat_policies`) moves each user whose policy in force is the withdrawn seed, with nothing pending, to the current seed as an `initial` version effective at once, supersedes the old one at that instant and writes both governance events; any other policy, a pending change, no policy and every later run are left exactly as they were. A constraint failure while moving a user is ignored only when the policy in force is no longer the old seed (another process moved that user first); any other, a change queued meanwhile included, is raised and startup fails. Harm: an account keeps the 15% kill and 3% cap the owner withdrew and its bots stay in shadow, or the owner's own policy is overwritten.
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
- A failing `CONF risk.initial_seed_equals_spec` names the field that differs from `OWNER_SEED` in `test_conf_risk.py`: fix `SEED_POLICY`, or change `OWNER_SEED` and `docs/decisions.md` together in a separate, called-out change.
- `CONF risk.hat_safety_properties_pinned` fails when a pinned safety property was edited (digest), is no longer a `@given` property, or fails. Do not edit the property: stop and explain which safety behaviour changed; re-pin (update `SAFETY_DIGESTS`) only for a deliberate, reviewed change, and say so in the PR.
- Finding kept visible: HAT has no `DEV_MODE` bypass: `evaluate_signal` takes the session only from the trading calendar (`decide` requires `SessionCalendar.state` OPEN), so a dev flag never opens a HAT entry.
- Finding kept visible: the price band is a constant (`MAX_PRICE_DEVIATION_PCT`, 20%), not a policy field, because the policy document's fields are fixed; making it a field is a deliberate change.
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
- `verification/tla/broken/KillSwitch__no_trip_on_drawdown.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_kill.cfg`, `verification/tla/broken/KillSwitch__kill_keeps_approvals.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_live_health.cfg`, `verification/tla/broken/KillSwitch__auto_rearm_on_recovery.cfg`, `verification/tla/broken/KillSwitch__resume_clears_kill.cfg`, `verification/tla/broken/KillSwitch__entry_ignores_pause.cfg`, `verification/tla/broken/KillSwitch__health_manual_rearm.cfg`, `verification/tla/broken/KillSwitch__exit_blocked_when_killed.cfg`, `verification/tla/broken/KillSwitch__shared_health_channel.cfg`

Code under check:
- `backend/app/hat/risk/sizing.py`: `size_position`, `exact`
- `backend/app/hat/risk/decision.py`: `decide`, `Reason`, `SignalContext`
- `backend/app/hat/risk/caps.py`: the cap predicates, `effective_max_chase_pct`
- `backend/app/hat/risk/exposure.py`: `apply_outcome`, `apply_trade`, `max_positions`
- `backend/app/hat/risk/kill.py`: `current_state`, `kill_state_of`, `drawdown_breached`, `tripped_since`, `account_series`, `rebase_peak`, `adjusted_series`
- `backend/app/hat/risk/controls.py`: `trip_kill`, `rearm_kill`, `sync_health`, `evaluate_drawdown_kills`, `pause_bot`, `resume_bot`, `queue_mode_change`, `record_closed_trade`, `snapshot_bots`
- `backend/app/hat/risk/policy_upkeep.py`: `load_versions`, `record_policy_event`, `next_version`, `supersede_old_seed`, `ensure_hat_policies`
- `backend/app/hat/risk/policy_service.py`: `seed_initial_policy`, `request_policy_change` (its evidence from `backtest_evidence`, `backtest.runs.gate_evidence`), `apply_due_versions`
- `backend/app/hat/risk/sessions.py`: `SessionCalendar`, `next_session_open`
- `backend/app/hat/risk/modes.py`: `plan_mode_change`, `effective_mode`
- `backend/app/hat/risk/policy.py`: `parse_policy`, `SEED_POLICY`, `OLD_SEED_POLICY`, `effective_version`, `classify_direction`, `build_gate_evidence`, `increase_gate_ok`
- `backend/app/hat/risk/service.py`: `evaluate_signal`, `price_band_ok`

Checks:
- `backend/tests/hat/hat_risk_support.py`
- `backend/tests/hat/properties/test_sizing.py`
- `backend/tests/hat/properties/test_caps.py`
- `backend/tests/hat/properties/test_exposure.py`
- `backend/tests/hat/properties/test_kill.py`
- `backend/tests/hat/properties/test_policy.py`
- `backend/tests/hat/properties/test_calendar.py`
- `backend/tests/hat/properties/test_mode_change.py`
- `backend/tests/hat/conformance/test_conf_risk.py`
- `backend/tests/hat/db/test_db_risk.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `0cf2eae6ab9307dffb7950ebf7e1462733cc7567`
Covered digest: `b7cace72293dba8f3d46a4ce1ea831944866ae37552e08f386430d669fcb83d4`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model, then the break was removed and the check returned to passing.

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

### TLA KillSwitch!HealthClearsOwnSource

Break: `verification/tla/broken/KillSwitch__shared_health_channel.cfg` selects the model variant where one source turning healthy clears every health trip of the account key.

```
== broken/KillSwitch__shared_health_channel.cfg: must violate HealthClearsOwnSource
Error: Invariant HealthClearsOwnSource is violated.
```

### TLA KillSwitch!NoAutoRearm

Break: `verification/tla/broken/KillSwitch__auto_rearm_on_recovery.cfg` selects the model variant where the snapshot re-arms the account kill when equity recovers, with no second step.

```
Error: Invariant NoAutoRearm is violated.
State 1: <Initial>
State 2: <SnapshotAccount>
State 3: <SnapshotAccount>
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

### PBT caps.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/caps.py` (caps.global_open_risk): the candidate's own risk left out of the sum.

```
E AssertionError: assert None == <Reason.GLOBAL_OPEN_RISK: 'global_open_risk'>
E + where None = RiskDecision(accepted=True, reason=None, quantity=500, stop=Decimal('95'), risk_amount=Decimal('2500'), warnings=(), p...4-ab15316d56e0'), binding='risk'
E + and <Reason.GLOBAL_OPEN_RISK: 'global_open_risk'> = Reason.GLOBAL_OPEN_RISK
E Failing test case: test_global_open_risk(
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

### PBT kill.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/kill.py` (kill.current_state): one latest row across health and risk channels.

```
E AssertionError: assert 'tripped' == 'armed'
E
E - armed
E + tripped
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

### CONF risk.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/policy.py` (risk.policy_schema_fields): a field dropped from the policy schema.

```
E AssertionError: assert {'adv_cap_pct...h_bands', ...} == {'adv_cap_pct..., 'kill', ...}
E
E Extra items in the left set:
E 'health_bands'
```

### DB risk.*

Each break below was made in the real code, the matching check run, and the file restored; every one failed as shown.

Break: in `backend/app/hat/risk/policy_service.py` (risk.policy_service_rows): an increase effective at the next open, not after 7 days.

```
E app.hat.risk.policy.CoolingOffError: a risk increase must take effect at least 7 days after request
FAILED tests/hat/db/test_db_risk.py::test_risk__policy_service_rows_pass_the_database - app.hat.risk.policy.CoolingOffError: a risk increase must take effect at least 7 d
```

### PBT policy.*

Break: `supersede_old_seed` ignores a pending version (the owner's queued change is overtaken).

```
E   assert [1] == [0]
E     At index 0 diff: 1 != 0
```

