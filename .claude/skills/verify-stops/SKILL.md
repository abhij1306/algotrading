---
name: verify-stops
description: How to run, read and update the checks behind Hire a Trader's initial stop rules, the nine-rule exit stack, the rule that a stop only rises and the stop resting at the broker under every real position (PBT stops, exits and rescale, DB governance.stop_only_rises and rescale_moves_stop, TLA StopPlacement), and the split and bonus rescale of open positions. Use before and after any change to backend/app/hat/engine/stops.py, backend/app/hat/engine/exits.py, backend/app/hat/engine/rescale.py, backend/app/hat/execution/protect.py, verification/tla/StopPlacement.tla, the hat_position stop and delete triggers in backend/app/hat/schema.py, or the stop and exit parts of the strategy config.
---

# verify-stops

Checks that every initial stop is priced from its rule exactly, on the tick grid, strictly below the entry and (for the contraction low) never wider than the configured percentage, that the exit stack evaluates top to bottom with a full exit beating partials, firing each rule once, selling fractions of the original quantity and never lowering a stop, that the database refuses to lower a position's stop or delete the position (a delete followed by an insert would restart the stop lower), and that a real position always rests on a broker stop that follows its stop up and never down (the `StopPlacement` model), and that a split or bonus rescales an open position before the ex-date open instead of stopping it out. Requirement IDs (index: `docs/invariants.md`): (to 053, 070 to 073),,,,,,,. The code behind the model is `execution/protect.py`, `execution/fills.py` and `engine/live_trades.py`; its property tests on that code (`manage.*`) are in `verify-execution`.

## Properties

- `PBT stops.final_contraction_low` The stop is the final contraction low times (1 minus the buffer percentage) rounded down to the tick, and is skipped when it is not below the entry, more than the maximum percentage below it, or (with `min_pct` above 0; 0 is off) less than `min_pct` below it (`stop_too_narrow`); every skip reason is one a signal stores as itself (`trades.ENTRY_SKIP_REASONS`), the configured rule gives the same plan through `plan_stop`, and a rule with `min_pct` >= `max_pct` is refused. Harm: a stop wider than the rule allows risks more than the position was sized for; one inside the base's noise is shaken out.
- `PBT stops.contraction_stop_at_the_fill` The contraction stop the scan plans at the pivot (`plan_stop_at_pivot`, which leaves `min_pct` to the entry) and the backtest re-checks at a fill at or above the pivot (`check_width`) is the live plan at that fill, for every `min_pct`. Harm: the backtest skips (as `stop_input_missing`) trades live would take.
- `PBT stops.low_of_day` The stop is the lowest low of the day so far less the buffer ticks, strictly below the entry, else a skip. Harm: a stop above the day's low is hit by ordinary noise, and one at or above the entry is not a stop.
- `PBT stops.custom_rules` `pct_below_entry`, `atr_multiple` and `swing_low` give the formula rounded down to the tick, strictly below the entry, else a skip with a fixed reason; a rule whose input is missing is a skip, never a guess. Harm: a stop above the entry or off the tick grid is refused by the exchange or fires at once.
- `PBT exits.full_beats_partial` When a full exit and partials fire on one evaluation the output is exactly one exit of the whole open quantity and no partial. Harm: a partial sale before a stop-out leaves the position half open after the trade should have ended.
- `PBT exits.fire_once` Over any run of evaluations a rule id is reported fired at most once, a fired rule never sells again and the quantity sold never exceeds the original quantity. Harm: a partial rule that fires on every poll sells the whole position in slices.
- `PBT exits.fraction_arithmetic` A sell fraction applies to the original quantity, rounds down, is at least one share and never more than the open quantity, and partials that together reach the open quantity are one full exit. Harm: a one-share remainder that is never sold, or a sale larger than the position.
- `PBT exits.rule_semantics` Each of the nine rules alone sells or raises the stop exactly when the spec says, in the phase the spec gives it (live, 15:25 close, nightly), and a disabled rule or a missing price never fires one. Harm: a rule firing early or late changes every trade result.
- `PBT exits.never_lowers_stop` Over any run of evaluations the stop never goes down, a new stop is always above the current one and a result carrying a lower stop changes nothing. Harm: a lower stop widens the risk the position was sized for.
- `DB governance.stop_only_rises` The database refuses an UPDATE that lowers `hat_position.current_stop` (alone or with other columns, one row or all) and refuses to DELETE a position; equal or higher stops and other columns move freely. Harm: a lowered stop, or a delete and re-insert, widens the risk behind the owner's back.
- `DB governance.rescale_moves_stop` A lowering UPDATE goes through only when a `position.rescaled` event of that position names exactly its old and new quantity, entry and stops: not with no event, another position's event, another event type, or an event that differs in any of the eight values; and a second lowering after the rescale is refused. Harm: the exception becomes a way to widen any stop.
- `PBT rescale.preserves_position` For every split or bonus ratio, stack and raised stop, `ca.rescale` gives quantity ⌊q / f⌋ and prices × f, keeps quantity × entry within one share, open risk and the stop's R distance within rounding, and on the ex-date open at the old close × f no exit fires (live or close). Harm: a fake `hard_stop` or a fake 50% loss on the ex-date open (F3).
- `PBT rescale.applied_once` For every sequence of runs with actions added between, each (position, action) is applied once, and an action on or before the entry day, an `other` action and a consolidation never. Harm: a split applied twice, or a rights issue adding shares.
- `PBT rescale.factor_exact` A stored 12-place factor of a ratio with denominator ≤ 1000 reads back exactly; a short factor that is no such ratio is refused. Harm: 0.0110 read as 11/1000 instead of 1/91.
- `PBT rescale.pending_is_manage_only` While a position awaits its rescale the exits' view source gives it no view and a trigger of its instrument is stored Rejected `corporate_action_pending`; once rescaled both return. Harm: exits or entries on prices of the wrong scale.
- `PBT rescale.unhandled_is_manage_only` For every action the rescale cannot handle (`other`, or a split or bonus with factor 1 or above) and ex-date around the entry day, with or without a split beside it, over repeated `ca.rescale` runs (`Nightly.rescale`): the action is never applied, a position it spans gets no exit view and is manage-only, its instrument awaits a rescale, and one `corporate_action.unhandled` names the (position, action); the split beside it is applied once. Harm: a consolidation keeps ten times the shares on an old-scale stop, or a rights issue's old stop sells at the open.
- `PBT rescale.resolution_ends_lock` For every unhandled action and virtual position, with or without a second action: the owner's `resolve_action` outside market hours records one `corporate_action.resolved` as the owner's (chat id) and closes the position at the given price through `close`; a real position (`real_position_flatten`), a resolution inside a session, of an action that does not lock it, of an unknown position or without a price is refused and writes nothing. Harm: a locked shadow position stays manage-only for ever, or a real one sells or stops on an old-scale quantity the broker no longer holds.
- `PBT rescale.broker_stop_follows` After a rescale, the next sync moves every live broker stop (GTT or day stop) down to the rescaled trigger first and then to the rescaled quantity, never larger than the old size at the old trigger; while the broker refuses the larger size the stop rests at the new trigger for the old size; with no rescale nothing moves. Harm: the old GTT fires on the ex-date open.
- `PBT rescale.next_session_lookup` `ca.adjust` looks up exactly the held and watchlisted symbols for the next session, stores what comes back and alerts each failed lookup (or one with an unreadable row) once without failing, and with no MCP client still succeeds and alerts once. Harm: the split is first seen after the ex-date close.
- `PBT rescale.price_neutral_changes_nothing` For every set of positions and actions, adding price-neutral rows (`dividend`, `notice`, factor 1) on any ex-date leaves the manage-only set, the pending set, the rescale, the `corporate_action.unhandled` flags and the adjusted view's factors as they were without them. Harm: every dividend of a held stock makes the position manage-only with an URGENT alert, or enters the adjusted view.
- `PBT stops.tick_size_slabs` The tick of a price is its NSE slab (0.01 below ₹250, 0.05 to ₹1,000, 0.10 to ₹5,000, 0.50 to ₹10,000, 1.00 to ₹20,000, 5.00 above, each upper bound inclusive; none for a non-positive price), and (`test_tick_size_reference_close`) a trade date's tick is set from the last close before its month, else the latest earlier close, never the day's own. Harm: a stop or GTT price off the exchange's tick grid is refused, leaving a position with no stop.
- `TLA StopPlacement!StopAfterFill` After a real entry fill, its broker stop is placed (or resized) before any other HAT action on the position. Harm: a raised stop, a sale or a swap runs on a position the broker does not yet protect.
- `TLA StopPlacement!AlwaysProtected` A held real position always has a working broker stop, except while a fill's stop work has not run yet, after a failure that raised an alert, or while a GTT that triggered without a fill is still unseen. Harm: a gap-down or a machine outage hits an unprotected position and nobody is told.
- `TLA StopPlacement!BrokerStopMatchesCurrent` A broker stop (intraday or GTT) is never above the position's current stop. Harm: the broker sells on noise above the price HAT holds as the stop.
- `TLA StopPlacement!NoUnprotectedGap` The 15:25 swap cancels the intraday stop only once the GTT is confirmed, and a failed GTT is alerted. Harm: the day stop is cancelled first and the position spends the night with no stop.
- `TLA StopPlacement!NightlyVerifyCovers` After tonight's verify, a held position has a GTT at its current stop, or the verify alerted it. Harm: a missing or stale GTT goes unnoticed for days.
- `TLA StopPlacement!StopOnlyTightens` The prices a position's broker stops are set to never go down. Harm: a loosened broker stop widens the risk the position was sized for.
- `TLA StopPlacement!GttUnfilledAlerted` A GTT that triggered without a fill under a held position is eventually alerted, by the sync in the session or by the nightly verify. Harm: the owner believes a position is protected after its stop failed to fill.
- `TLA StopPlacement!LostGttSettled` A GTT whose placement answer was lost is eventually settled (adopted when it rests at the broker, ended when it does not) while the position is held. Harm: a lost answer leaves the position with no stop HAT will place, night after night.
- `TLA StopPlacement!LostNeverBlocksSale` A sale due on a held position is never refused because a GTT's answer is still unsure. Harm: a stop-loss exit is frozen while the price falls.
- `TLA StopPlacement!SharesTracked` HAT's open quantity always equals the shares the broker holds for the position, also when part of a DAY limit entry fills after a stop fill, an exit or a flatten emptied the position (the position stays open until the broker shows the entry ended). Harm: shares bought after HAT closed the position sit at the broker untracked and with no stop.
- `TLA StopPlacement!RestCancelledOnExit` Once a sale has emptied the position, the rest of a still-working entry has been asked to be cancelled. Harm: the remainder holds risk budget no cap counts and buys shares HAT is selling.
- `TLA StopPlacement!EmptiedCloses` A position its sales emptied eventually closes (the broker ends the entry: the cancel, or the DAY expiry at the close), unless a late part of the entry is held. Harm: an emptied position stays open for ever and holds its slot.
- `TLA StopPlacement!RestingSold` A GTT that triggered with its limit sale resting below the limit under a held position is eventually sold (the sync has the sale made a market sale), never left at its limit to expire at the close. Harm: the stop triggered and the shares sit unsold all session while the price keeps falling.
- `PBT exits.veto_score` The veto score over the R multiples of the closed veto trades is avoided = -sum(R < 0) and missed = sum(R > 0), exactly, both non-negative, with missed - avoided the plain sum and a zero R in neither. Harm: the weekly digest tells the user their skips saved money they in fact cost.
- `PBT exits.veto_verdict_threshold` The veto verdict is absent until 50 decisions, then "skips beat takes" exactly when the mean R of the skipped trades is below the mean R of the taken ones (compared without rounding either mean), else "skips do not beat takes", and insufficient data when either side is empty. Harm: the weekly digest tells the owner to keep vetoing (or to switch to auto) on a rounding artefact.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_stops.py tests/hat/properties/test_exits.py tests/hat/properties/test_rescale.py -q --no-cov
# the DB check needs a throwaway Postgres whose database name ends in _test
SMARTTRADER_PG_TESTS=1 DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test \
  python -m pytest tests/hat/db/test_db_runtime.py tests/hat/db/test_db_broken_variants.py -q --no-cov -m postgres -k "stop_only_rises or rescale_moves_stop"
```

```bash
# the broker-stop model (Java 17+)
verification/tla/run.sh StopPlacement
verification/tla/run.sh --broken StopPlacement
```

No check calls a broker or the exchange; the shadow engine properties in `test_exits.py` run on SQLite with a `FakeBroker` that must record zero calls.

## Bug or model error

- A failing property prints a falsifying example (entry, low, buffer, tick for a stop; the rule stack, the position and the view for an exit). Reproduce it with the real function (`stops.plan_stop`, `exits.evaluate`). If the real code breaks the spec, that is a bug: fix it. Never weaken the property or narrow its strategy; a narrowing needs a reason under "Scope changes".
- The oracles are written apart from the code: stops with `Fraction`, the firing conditions of the exit rules in `expected_sells` and `expected_phase` in `test_exits.py`. If the two disagree, read the code and `docs/invariants.md` before deciding which is wrong.
- Stops are rounded down to the instrument's real tick (`data/ticks.py`, `StopInputs.tick` is required, there is no default); a lower stop is never tighter than the formula. The custom rules follow the same rounding.
- `regime_exit` needs no price of its own: it fires on the index at night. `evaluate_position` refuses an exit with no price (`exit_without_price`) before writing anything.
- A `StopPlacement` counterexample names its actions; each has a `\* code:` comment naming the function it stands for (`StopDesk.protect`, `follow`, `swap`, `_cancel`, `verify`, `cancel_all`, `cancel_entry`; `FillSync._apply`, `_unprotected`, `_gtt_unfilled`, `run`; `live_trades.evaluate_real_position`, `apply_exit_fill`, `close_if_emptied`). Replay the steps against that code before deciding whether the code or the model is wrong. HAT's view of a GTT that triggered without a fill is still "working" (`Booked`), as it is in the code until the sync or the verify sees the order that ended short.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new stop rule: add its model to `strategy/config.py`, its function and `plan_stop` branch to `stops.py`, and a property in `test_stops.py` with its own `Fraction` formula.
- A new exit rule: add it to `strategy/config.py`, `exits._sells` and its phase in `_RULE_PHASE`, and to `expected_sells` and `expected_phase` and the rule strategies in `test_exits.py`.
- A change to the evaluation rules (full beats partial, fire once, fractions) needs `docs/invariants.md` updated with it.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-stops` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/engine/stops.py`
- `backend/app/hat/data/ticks.py`
- `backend/app/hat/engine/exits.py`
- `backend/app/hat/engine/strategy/config.py`
- `backend/app/hat/schema.py`
- `backend/app/hat/execution/protect.py`
- `backend/app/hat/engine/rescale.py`
- `verification/tla/StopPlacement.tla`
- `verification/tla/StopPlacement.cfg`

Checks:
- `backend/tests/hat/properties/test_stops.py`
- `backend/tests/hat/properties/test_exits.py`
- `backend/tests/hat/properties/test_rescale.py`
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
- `verification/tla/broken/StopPlacement__never_settle.cfg`
- `verification/tla/broken/StopPlacement__lost_blocks_sale.cfg`
- `verification/tla/broken/StopPlacement__close_with_rest.cfg`
- `verification/tla/broken/StopPlacement__keep_rest.cfg`
- `verification/tla/broken/StopPlacement__no_close_step.cfg`
- `verification/tla/broken/StopPlacement__no_resting_market.cfg`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `2f23b2f377fa3db89906eb01cf61eb230b822b73`
Covered digest: `5cb78e85c989c99437cb12ab8c78d796d30ecb82f7294ef525f0f9c455be5fd6`

## Scratch breaks

One recorded break per check: the real code was changed as described, the check was run and failed, and the code was restored.

### PBT stops.final_contraction_low

Break: in `backend/app/hat/engine/stops.py`: the buffer is added to the low instead of taken off it.

```
FAILED tests/hat/properties/test_stops.py::test_final_contraction_low - Excep [cut]
============================== 1 failed in 0.20s ===============================
```

### PBT stops.contraction_stop_at_the_fill

Break: the overnight plan at the pivot applies `min_pct` (the `model_copy` dropping it removed).

```
E   AssertionError: assert Decimal('98.70') is None
E    +  where Decimal('98.70') = StopPlan(stop=Decimal('98.70'), skip=None).stop
E   Failing explicit example: test_contraction_stop_at_the_fill(
E       pivot=Decimal('100'),
E       above=100,
E       low=Decimal('99.2'),
E       max_pct=8,
FAILED tests/hat/properties/test_stops.py::test_contraction_stop_at_the_fill
```

### PBT stops.low_of_day

Break: in `backend/app/hat/engine/stops.py`: the buffer ticks go above the day's low instead of below it.

```
FAILED tests/hat/properties/test_stops.py::test_low_of_day - ExceptionGroup:  [cut]
============================== 1 failed in 0.20s ===============================
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

### TLA StopPlacement!LostGttSettled

Break: the variant `never_settle`: `Settle` never runs, so a GTT whose answer was lost stays unsure.

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <RaiseStop ...>
State 5: <EntryFill ...>
State 6: <Clock ...>
State 7: <Protect ...>
State 8: <RaiseStop ...>
State 9: <GttTriggersUnfilled ...>
State 10: <SeeUnfilled ...>
State 11: <Protect ...>
State 12: <SwapPlaceGtt ...>
State 13: <Clock ...>
State 14: <Clock ...>
State 15: <Verify ...>
State 16: <Clock ...>
```

### TLA StopPlacement!SharesTracked

Break: the variant `close_with_rest`: a sale that empties the position closes it at once and a later entry fill is refused.

```
Error: Invariant SharesTracked is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <DayStopFills ...>
State 5: <EntryFill ...>
```

### TLA StopPlacement!RestCancelledOnExit

Break: the variant `keep_rest`: no sale asks the broker to cancel the rest of the entry.

```
Error: Invariant RestCancelledOnExit is violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <DayStopFills ...>
```

### TLA StopPlacement!EmptiedCloses

Break: the variant `no_close_step`: the sync never closes an emptied position once its entry ended (`FillSync.run` without step 7).

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 2: <EntryFill ...>
State 3: <Protect ...>
State 4: <RaiseStop ...>
State 5: <RaiseStop ...>
State 6: <Follow ...>
State 7: <Follow ...>
State 8: <DayStopFills ...>
State 9: <EntryEnds ...>
State 10: <Clock ...>
State 11: <Clock ...>
State 12: <Clock ...>
Back to state 9: <Clock ...>
```

### TLA StopPlacement!LostNeverBlocksSale

Break: the variant `lost_blocks_sale`: `Sell` is refused while a GTT's answer is unsure.

```
Error: Invariant LostNeverBlocksSale is violated.
State 1: <Initial predicate>
State 2: <Clock ...>
State 3: <EntryFill ...>
State 4: <EntryFill ...>
State 5: <Protect ...>
State 6: <Sell ...>
```

### PBT rescale.preserves_position

Break: in `backend/app/hat/engine/rescale.py`: `_apply` records the event but leaves the row unchanged (main before).

```
FAILED tests/hat/properties/test_rescale.py::test_preserves_position - assert [cut]
======================= 1 failed, 4 deselected in 0.76s ========================
```

### PBT rescale.applied_once

Break: in `backend/app/hat/engine/rescale.py`: `pending_actions` ignores the actions already applied.

```
FAILED tests/hat/properties/test_rescale.py::test_applied_once - AssertionErr [cut]
```

### PBT rescale.factor_exact

Break: in `backend/app/hat/engine/rescale.py`: `exact_factor` takes the nearest ratio without the 1e-9 check.

```
FAILED tests/hat/properties/test_rescale.py::test_factor_exact - Failed: DID  [cut]
```

### PBT rescale.pending_is_manage_only

Break: in `backend/app/hat/daily/live.py`: `ExitViews` gives a view to a position awaiting a rescale.

```
FAILED tests/hat/properties/test_rescale.py::test_pending_is_manage_only - As [cut]
```

### PBT rescale.unhandled_is_manage_only

Break: in `backend/app/hat/daily/live.py`: `ExitViews` reads `pending_actions` only (main before).

```
E   AssertionError: assert (ExitView(price=Decimal('10.00'), days_held=2, sma={10: Decimal('10.00')}, swing_low={}, regime_off=False) is None) == True
E   Failing test case: test_unhandled_is_manage_only(
```

### PBT rescale.resolution_ends_lock

Check run: `test_rescale.py::test_resolution_ends_lock`.

Break: `resolve_action` without the market-hours refusal.

```
    | app.hat.engine.rescale.ResolutionError: real_position_flatten
```

### PBT rescale.broker_stop_follows

Break: in `backend/app/hat/execution/manage.py`: `TradeManager.sync` does not call `_follow_rescales`.

```
FAILED tests/hat/properties/test_rescale.py::test_broker_stop_follows - Asser [cut]
```

### PBT rescale.next_session_lookup

Break: in `backend/app/hat/engine/rescale.py`: `held_and_watchlisted` leaves out the watchlist.

```
FAILED tests/hat/properties/test_rescale.py::test_next_session_lookup - Asser [cut]
```

### DB governance.rescale_moves_stop

Break: the `to` half of the event match (new quantity, entry and stops) removed from `hat_stop_only_tightens` in `backend/app/hat/schema.py`.

```
tests/hat/db/hat_db_runtime.py:494: in check_governance__rescale_moves_stop
tests/hat/db/hat_db_rules.py:35: in rejects
E   AssertionError: the database accepted: a rescale whose event differs in to.qty_open
```

The five `rescale_function` variants in `test_db_broken_variants.py` (no rescale exception, any event of the position, an event of any position, only the stops matched, the trigger dropped) each make the check fail too.

### TLA StopPlacement!RestingSold

Break: the variant `no_resting_market`: the sync alerts a resting sale but leaves it at its limit (the main code before).

```
Error: Temporal properties were violated.
State 1: <Initial predicate>
State 2: <Clock ...>
State 3: <EntryFill ...>
State 4: <Protect ...>
State 5: <SwapPlaceGtt ...>
State 6: <GttTriggersResting ...>
State 7: <SeeResting ...>
State 8: <RaiseStop ...>
State 9: <Clock ...>
State 10: <Clock ...>
State 11: <Verify ...>
State 12: <Clock ...>
State 13: <Clock ...>
State 14: <RaiseStop ...>
State 15: <EntryFill ...>
State 16: <Protect ...>
State 17: <SwapPlaceGtt ...>
State 18: <Settle ...>
State 19: <Clock ...>
State 20: <Clock ...>
State 21: <Verify ...>
State 22: <Clock ...>
State 23: <GttTriggersUnfilled ...>
State 24: <SeeUnfilled ...>
State 25: <Clock ...>
Back to state 16: <Protect ...>
```

### PBT rescale.price_neutral_changes_nothing

Break: in `backend/app/hat/engine/rescale.py`, `unhandled_actions` no longer excludes `PRICE_NEUTRAL_TYPES` (a dividend is an unhandled action).

```
E   AssertionError: assert (500, [], Tru...vidend'], ...) == (500, [], Fal...alse, [], ...)
E     At index 2 diff: True != False
```

