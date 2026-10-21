---
name: verify-stops
description: How to run, read and update the checks behind Hire a Trader's initial stop rules, the nine-rule exit stack, the rule that a stop only rises and the stop resting at the broker under every real position (PBT stops and exits, DB governance.stop_only_rises, TLA StopPlacement). Use before and after any change to backend/app/hat/engine/stops.py, backend/app/hat/engine/exits.py, backend/app/hat/execution/protect.py, verification/tla/StopPlacement.tla, the hat_position stop and delete triggers in backend/app/hat/schema.py, or the stop and exit parts of the strategy config.
---

# verify-stops

Checks that every initial stop is priced from its rule exactly, on the tick grid, strictly below the entry and (for the contraction low) never wider than the configured percentage, that the exit stack evaluates top to bottom with a full exit beating partials, firing each rule once, selling fractions of the original quantity and never lowering a stop, that the database refuses to lower a position's stop or delete the position (a delete followed by an insert would restart the stop lower), and that a real position always rests on a broker stop that follows its stop up and never down (the `StopPlacement` model). Specs: `docs/specs/hat/07-execution-and-brokers.md` §5 and §7 (HAT-EXEC-050 to 053, 070 to 073), `06-risk-and-governance.md` (HAT-RISK-070, 073), `03-schema.md` (HAT-SCH-081), `04-criteria-and-bots.md` (HAT-BOT-062), `11-verification.md` (HAT-VER-030..034, 072), `07` §6 (HAT-EXEC-060..064), `01-decisions.md` (HAT-DEC-B17). The code behind the model is `execution/protect.py`, `execution/fills.py` and `engine/live_trades.py`; its property tests on that code (`manage.*`) are in `verify-execution`.

## Properties

- `PBT stops.final_contraction_low` The stop is the final contraction low times (1 minus the buffer percentage) rounded down to the tick, and is skipped when it is not below the entry or more than the maximum percentage below it. Harm: a stop wider than the rule allows risks more than the position was sized for.
- `PBT stops.low_of_day` The stop is the lowest low of the day so far less the buffer ticks, strictly below the entry, else a skip. Harm: a stop above the day's low is hit by ordinary noise, and one at or above the entry is not a stop.
- `PBT stops.custom_rules` `pct_below_entry`, `atr_multiple` and `swing_low` give the formula rounded down to the tick, strictly below the entry, else a skip with a fixed reason; a rule whose input is missing is a skip, never a guess. Harm: a stop above the entry or off the tick grid is refused by the exchange or fires at once.
- `PBT exits.full_beats_partial` When a full exit and partials fire on one evaluation the output is exactly one exit of the whole open quantity and no partial. Harm: a partial sale before a stop-out leaves the position half open after the trade should have ended.
- `PBT exits.fire_once` Over any run of evaluations a rule id is reported fired at most once, a fired rule never sells again and the quantity sold never exceeds the original quantity. Harm: a partial rule that fires on every poll sells the whole position in slices.
- `PBT exits.fraction_arithmetic` A sell fraction applies to the original quantity, rounds down, is at least one share and never more than the open quantity, and partials that together reach the open quantity are one full exit. Harm: a one-share remainder that is never sold, or a sale larger than the position.
- `PBT exits.rule_semantics` Each of the nine rules alone sells or raises the stop exactly when the spec says, in the phase the spec gives it (live, 15:25 close, nightly), and a disabled rule or a missing price never fires one. Harm: a rule firing early or late changes every trade result.
- `PBT exits.never_lowers_stop` Over any run of evaluations the stop never goes down, a new stop is always above the current one and a result carrying a lower stop changes nothing. Harm: a lower stop widens the risk the position was sized for.
- `DB governance.stop_only_rises` The database refuses an UPDATE that lowers `hat_position.current_stop` (alone or with other columns, one row or all) and refuses to DELETE a position; equal or higher stops and other columns move freely. Harm: a lowered stop, or a delete and re-insert, widens the risk behind the owner's back.
- `PBT stops.tick_size_slabs` The tick of a price is its NSE slab (0.01 below ₹250, 0.05 to ₹1,000, 0.10 to ₹5,000, 0.50 to ₹10,000, 1.00 to ₹20,000, 5.00 above, each upper bound inclusive; none for a non-positive price), and (`test_tick_size_reference_close`) a trade date's tick is set from the last close before its month, else the latest earlier close, never the day's own. Harm: a stop or GTT price off the exchange's tick grid is refused, leaving a position with no stop.
- `TLA StopPlacement!StopAfterFill` After a real entry fill, its broker stop is placed (or resized) before any other HAT action on the position. Harm: a raised stop, a sale or a swap runs on a position the broker does not yet protect.
- `TLA StopPlacement!AlwaysProtected` A held real position always has a working broker stop, except while a fill's stop work has not run yet, after a failure that raised an alert, or while a GTT that triggered without a fill is still unseen. Harm: a gap-down or a machine outage hits an unprotected position and nobody is told.
- `TLA StopPlacement!BrokerStopMatchesCurrent` A broker stop (intraday or GTT) is never above the position's current stop. Harm: the broker sells on noise above the price HAT holds as the stop.
- `TLA StopPlacement!NoUnprotectedGap` The 15:25 swap cancels the intraday stop only once the GTT is confirmed, and a failed GTT is alerted. Harm: the day stop is cancelled first and the position spends the night with no stop.
- `TLA StopPlacement!NightlyVerifyCovers` After tonight's verify, a held position has a GTT at its current stop, or the verify alerted it. Harm: a missing or stale GTT goes unnoticed for days.
- `TLA StopPlacement!StopOnlyTightens` The prices a position's broker stops are set to never go down. Harm: a loosened broker stop widens the risk the position was sized for.
- `TLA StopPlacement!GttUnfilledAlerted` A GTT that triggered without a fill under a held position is eventually alerted, by the sync in the session or by the nightly verify. Harm: the owner believes a position is protected after its stop failed to fill.
- `PBT exits.veto_score` The veto score over the R multiples of the closed veto trades is avoided = -sum(R < 0) and missed = sum(R > 0), exactly, both non-negative, with missed - avoided the plain sum and a zero R in neither. Harm: the weekly digest tells the user their skips saved money they in fact cost.
- `PBT exits.veto_verdict_threshold` The veto verdict is absent until 50 decisions, then "skips beat takes" exactly when the mean R of the skipped trades is below the mean R of the taken ones (compared without rounding either mean), else "skips do not beat takes", and insufficient data when either side is empty. Harm: the weekly digest tells the owner to keep vetoing (or to switch to auto) on a rounding artefact (HAT-EXEC-131).

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_stops.py tests/hat/properties/test_exits.py -q --no-cov
# the DB check needs a throwaway Postgres whose database name ends in _test
SMARTTRADER_PG_TESTS=1 DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test \
  python -m pytest tests/hat/db/test_db_runtime.py tests/hat/db/test_db_broken_variants.py -q --no-cov -m postgres -k "stop_only_rises"
```

```bash
# the broker-stop model (Java 17+)
verification/tla/run.sh StopPlacement
verification/tla/run.sh --broken StopPlacement
```

No check calls a broker or the exchange; the shadow engine properties in `test_exits.py` run on SQLite with a `FakeBroker` that must record zero calls.

## Bug or model error

- A failing property prints a falsifying example (entry, low, buffer, tick for a stop; the rule stack, the position and the view for an exit). Reproduce it with the real function (`stops.plan_stop`, `exits.evaluate`). If the real code breaks the spec, that is a bug: fix it. Never weaken the property or narrow its strategy; a narrowing needs a reason under "Scope changes".
- The oracles are written apart from the code: stops with `Fraction`, the firing conditions of the exit rules in `expected_sells` and `expected_phase` in `test_exits.py`. If the two disagree, read the spec text (07 §5, §7) before deciding which is wrong.
- Stops are rounded down to the instrument's real tick (`data/ticks.py`, HAT-EXEC-053; `StopInputs.tick` is required, there is no default); a lower stop is never tighter than the formula. The custom rules follow the same rounding.
- `regime_exit` needs no price of its own: it fires on the index at night. `evaluate_position` refuses an exit with no price (`exit_without_price`) before writing anything.
- A `StopPlacement` counterexample names its actions; each has a `\* code:` comment naming the function it stands for (`StopDesk.protect`, `follow`, `swap`, `_cancel`, `verify`, `cancel_all`; `FillSync._apply`, `_unprotected`, `_gtt_unfilled`; `live_trades.evaluate_real_position`). Replay the steps against that code before deciding whether the code or the model is wrong. HAT's view of a GTT that triggered without a fill is still "working" (`Booked`), as it is in the code until the sync or the verify sees the order that ended short.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new stop rule: add its model to `strategy/config.py`, its function and `plan_stop` branch to `stops.py`, and a property in `test_stops.py` with its own `Fraction` formula.
- A new exit rule: add it to `strategy/config.py`, `exits._sells` and its phase in `_RULE_PHASE`, and to `expected_sells` and `expected_phase` and the rule strategies in `test_exits.py`.
- A change to the evaluation rules (full beats partial, fire once, fractions) needs the spec text in 07 §7 changed first.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-stops` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/engine/stops.py`
- `backend/app/hat/data/ticks.py`
- `backend/app/hat/engine/exits.py`
- `backend/app/hat/engine/strategy/config.py`
- `backend/app/hat/schema.py`
- `backend/app/hat/execution/protect.py`
- `verification/tla/StopPlacement.tla`
- `verification/tla/StopPlacement.cfg`

Checks:
- `backend/tests/hat/properties/test_stops.py`
- `backend/tests/hat/properties/test_exits.py`
- `backend/tests/hat/hat_engine_support.py`
- `backend/tests/hat/db/hat_db_runtime.py`
- `backend/tests/hat/db/test_db_runtime.py`
- `backend/tests/hat/db/test_db_broken_variants.py`
- `backend/app/hat/engine/approvals.py`
- `verification/tla/broken/StopPlacement__act_before_stop.cfg`
- `verification/tla/broken/StopPlacement__no_protect.cfg`
- `verification/tla/broken/StopPlacement__place_above.cfg`
- `verification/tla/broken/StopPlacement__cancel_first.cfg`
- `verification/tla/broken/StopPlacement__verify_skips_missing.cfg`
- `verification/tla/broken/StopPlacement__reset_on_follow.cfg`
- `verification/tla/broken/StopPlacement__no_unfilled_alert.cfg`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `2f23b2f377fa3db89906eb01cf61eb230b822b73`
Covered digest: `e7d158df5d8504cd6ebdf8aa1e4c26b69ff8712e212f993b87073eb8bd62cfd0`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 15a replaced `DEFAULT_TICK = 0.05` with the NSE slab tick of the instrument (`data/ticks.py`); the stop properties still run on 0.01, 0.05, 0.1, 1 and 5, and the slabs are checked by `stops.tick_size_slabs`. A new listing's tick is read from its latest close (`[OWNER-PENDING]`: NSE uses the issue price, which HAT does not hold).
- PR 15c built `StopPlacement` and removed `TLA StopPlacement!StopOnlyTightens` from `KNOWN_GAPS`. The model has one real position, at most two entry fills and three stop levels; it does not model the quantity a stop covers (resizing is checked by `manage.real_fill_protected` on the real code) nor the GTT's limit price (`stops.gtt_limit_offset`). `AlwaysProtected` allows the window between a fill and its stop work, a failure that was alerted, and an unseen unfilled GTT (then `GttUnfilledAlerted` holds). `NightlyVerifyCovers` accepts an alert in place of a GTT (the broker could not be reached, or the new GTT failed). `TypeOK` has no broken variant.
- Exits of a real position run through `live_trades.evaluate_real_position` (PR 15c), the same `exits.evaluate`; `evaluate_position` still refuses a position that is not virtual.
- Fees are zero in the shadow engine and fills are at the price of the view; `mfe_r` and `mae_r` come from the extremes the caller passes.
- PR 15b: veto trades (the virtual trades of skipped or expired confirm signals) run through the same exit stack and `evaluate_position` as shadow trades; their events carry `veto: true`. `trades.bot_snapshot` leaves them out of the bot's slots and stop-outs, so the stop and exit properties above, which drive shadow trades, are unchanged. `exits.veto_score` is new; the digest that reports it is PR 16.
- PR 16 added `exits.veto_verdict_threshold` (`approvals.veto_verdict`, `veto_history`). The comparison is exact (`sum(s) * |t| < sum(t) * |s|`) after the first draft compared rounded means; nothing was narrowed.

## Scratch breaks

One recorded break per check (HAT-VER-072): the real code was changed as described, the check was run and failed, and the code was restored.

### PBT stops.final_contraction_low

Break: in `backend/app/hat/engine/stops.py`: the buffer is added to the low instead of taken off it.

```
FAILED tests/hat/properties/test_stops.py::test_final_contraction_low - Excep [cut]
============================== 1 failed in 0.20s ===============================
```

### PBT stops.low_of_day

Break: in `backend/app/hat/engine/stops.py`: the buffer ticks go above the day's low instead of below it.

```
FAILED tests/hat/properties/test_stops.py::test_low_of_day - ExceptionGroup:  [cut]
============================== 1 failed in 0.20s ===============================
```

Break: in `backend/app/hat/engine/stops.py`: `low_of_day` no longer rounds the stop down to the tick.

```
FAILED tests/hat/properties/test_stops.py::test_low_of_day - ExceptionGroup: [cut]
```

### PBT stops.custom_rules

Break: in `backend/app/hat/engine/stops.py`: atr_multiple rounds the stop up a tick.

```
E   AssertionError: assert (None is not None)
FAILED tests/hat/properties/test_stops.py::test_custom_rules - AssertionError [cut]
============================== 1 failed in 0.19s ===============================
```

### PBT exits.full_beats_partial

Break: in `backend/app/hat/engine/exits.py`: a full exit gives way when a partial fires on the same evaluation.

```
FAILED tests/hat/properties/test_exits.py::test_full_beats_partial - Exceptio [cut]
============================== 1 failed in 11.37s ==============================
```

### PBT exits.fire_once

Break: in `backend/app/hat/engine/exits.py`: a rule that already fired is evaluated again.

```
E   AssertionError: assert not ({'1:partial_at_r'} & frozenset({'1:partial_at_r'}))
FAILED tests/hat/properties/test_exits.py::test_fire_once - AssertionError: a [cut]
============================== 1 failed in 5.29s ===============================
```

### PBT exits.fraction_arithmetic

Break: in `backend/app/hat/engine/exits.py`: a fraction below one share rounds to zero shares instead of one.

```
E   assert 0 == 1
FAILED tests/hat/properties/test_exits.py::test_fraction_arithmetic - assert  [cut]
============================== 1 failed in 0.43s ===============================
```

### PBT exits.rule_semantics

Break: in `backend/app/hat/engine/exits.py`: partial_after_days waits one day more than configured.

```
E   assert False == True
FAILED tests/hat/properties/test_exits.py::test_rule_semantics[4] - assert Fa [cut]
========================= 1 failed, 4 passed in 6.99s ==========================
```

### PBT exits.never_lowers_stop

Break: in `backend/app/hat/engine/exits.py`: `apply` takes a lower stop from a result.

```
E   AssertionError: assert Decimal('8.9') == Decimal('9.9')
FAILED tests/hat/properties/test_exits.py::test_never_lowers_stop - Assertion [cut]
============================== 1 failed in 5.87s ===============================
```

### DB governance.stop_only_rises

Break: in `backend/app/hat/schema.py`: the delete guard lets the delete through.

```
E   AssertionError: the database accepted: deleting a position
FAILED tests/hat/db/test_db_runtime.py::test_governance__stop_only_rises - As [cut]
============================== 1 failed in 0.30s ===============================
```

### PBT stops.tick_size_slabs

Break: in `backend/app/hat/data/ticks.py`: `price < 250` became `price <= 250`, so ₹250.00 gets the 0.01 tick.

```
E   AssertionError: assert Decimal('0.01') == Decimal('0.05')
E    +  where Decimal('0.01') = tick_size(Decimal('250.00'))
E    +    where Decimal('250.00') = <built-in method scaleb of decimal.Decimal object at 0x7fbedb0047c0>(-2)
E    +      where <built-in method scaleb of decimal.Decimal object at 0x7fbedb0047c0> = Decimal('25000').scaleb
E    +        where Decimal('25000') = Decimal(25000)
E    +  and   Decimal('0.05') = oracle_tick(25000)
E   Failing explicit example: test_tick_size_slabs(
```

### PBT exits.veto_score

Break: `veto_score` adds the negative R instead of subtracting it (avoided R comes out negative).

```
E   AssertionError: assert Fraction(-1, 10000) == -Fraction(-1, 10000)
E    +  where Fraction(-1, 10000) = Fraction(Decimal('-0.0001'))
E    +    where Decimal('-0.0001') = VetoScore(avoided_r=Decimal('-0.0001'), missed_r=Decimal('0'), trades=1).avoided_r
E    +  and   Fraction(-1, 10000) = sum([Fraction(-1, 10000)], Fraction(0, 1))
E    +    where Fraction(0, 1) = Fraction(0)
E   Failing test case: test_veto_score(
```

### TLA StopPlacement!StopAfterFill

Break: the variant `act_before_stop`: a HAT action on the position (`MayAct`) no longer waits for the stop work of a fill (in the code, `FillSync._apply` raising the stop before `StopDesk.protect`).

```
Error: Invariant StopAfterFill is violated.
State 1: <Initial predicate>
State 2: <Clock ...>
State 3: <EntryFill ...>
State 4: <RaiseStop ...>
```

### TLA StopPlacement!AlwaysProtected

Break: the variant `no_protect`: a fill no longer schedules its stop work (`FillSync._apply` without `StopDesk.protect`).

```
Error: Invariant AlwaysProtected is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
```

### TLA StopPlacement!BrokerStopMatchesCurrent

Break: the variant `place_above`: a new stop is placed one level above the current stop (`broker_trigger` rounding up).

```
Error: Invariant BrokerStopMatchesCurrent is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
```

### TLA StopPlacement!NoUnprotectedGap

Break: the variant `cancel_first`: the swap cancels the intraday stop before the GTT is confirmed.

```
Error: Invariant NoUnprotectedGap is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <EntryFill ...>
State 4: <Protect ...>
State 5: <Clock ...>
State 6: <SwapCancelDay ...>
```

### TLA StopPlacement!NightlyVerifyCovers

Break: the variant `verify_skips_missing`: the verify neither alerts nor re-places a GTT the broker does not confirm.

```
Error: Invariant NightlyVerifyCovers is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <Clock ...>
State 5: <SwapPlaceGtt ...>
State 6: <Clock ...>
State 7: <Clock ...>
State 8: <Verify ...>
```

### TLA StopPlacement!StopOnlyTightens

Break: the variant `reset_on_follow`: `follow` modifies the broker stop back to the initial stop.

```
Error: Invariant StopOnlyTightens is violated.
State 1: <Initial predicate>
State 2: <Clock ...>
State 3: <EntryFill ...>
State 4: <Protect ...>
State 5: <RaiseStop ...>
State 6: <SwapPlaceGtt ...>
State 7: <RaiseStop ...>
State 8: <Follow ...>
```

### TLA StopPlacement!GttUnfilledAlerted

Break: the variant `no_unfilled_alert`: the sync ends a GTT that triggered without a fill but raises no alert.

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <RaiseStop ...>
State 5: <Clock ...>
State 6: <RaiseStop ...>
State 7: <EntryFill ...>
```

### PBT exits.veto_verdict_threshold

Break: in `backend/app/hat/engine/approvals.py`, `veto_verdict` compares the two means rounded to one decimal.

```
E   AssertionError: assert 'skips do not beat takes' == 'skips beat takes'
E     - skips beat takes
E     + skips do not beat takes
```
