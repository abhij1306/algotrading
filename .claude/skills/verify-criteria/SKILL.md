---
name: verify-criteria
description: How to run, read and update the checks behind Hire a Trader's criteria library (the 18 daily criteria and the seven registered live ones), stage scoring, the strategy config model (instances, playbooks, versions, YAML, no risk keys), the two seeded bots and the catalogue conformance checks. Use before and after any change to backend/app/hat/engine/criteria, backend/app/hat/engine/strategy or the seed YAML files.
---

# verify-criteria

Checks that every daily criterion answers exactly as its contract says (exact arithmetic, point in time, fail closed on missing data), that criteria combine into a stage result by the stated formula, that an invalid or risk-bearing strategy config can never be saved, that every save is a new version, and that the two seeded bots equal the configs in the spec and start in shadow. Specs: `docs/specs/hat/04-criteria-and-bots.md` §1-§8 (HAT-CRIT-010..040, 100..123; HAT-BOT-050, 052, 060, 061, 070..072, 080..084), `07-execution-and-brokers.md` §12 (HAT-EXEC-120..122), `01-decisions.md` (HAT-DEC-B2..B7, B22, T10), `00-overview.md` (HAT-OVR-002, 003), `03-schema.md` (HAT-SCH-016, 032).

## Properties

- `PBT criteria.series_in_contract` `series_in` passes exactly when the instrument's series is in the list, and reports no data without a series. Harm: a trade-for-trade or illiquid series is traded.
- `PBT criteria.min_turnover_contract` `min_turnover` compares the exact mean traded value of the last `days` sessions with the minimum (equal passes), falls back to close x volume where traded value is null, and has no data without enough bars. Harm: the bot trades stocks too thin to exit.
- `PBT criteria.min_price_contract` `min_price` passes exactly when the latest close is at least the minimum (equal passes). Harm: penny stocks enter the watchlist.
- `PBT criteria.price_band_allowed_contract` `price_band_allowed` reads the band effective on the date (the latest row at or before it, duplicates included) and passes when it is allowed; no row is no data. Harm: stocks with a 2% or 5% circuit, which can lock and trap the exit, are traded.
- `PBT criteria.adr_min_contract` `adr_min` passes exactly when the mean of 100 x (high / low - 1) over the last `days` sessions is at least the minimum. Harm: stocks too sleepy for a breakout bot enter the watchlist.
- `PBT criteria.sma_above_contract` `sma_above` is a strict, exact comparison of the close or an SMA with another SMA; missing bars are no data. Harm: the Stage 2 trend filter admits downtrends or rejects uptrends.
- `PBT criteria.sma_rising_contract` `sma_rising` is true exactly when the SMA is higher than `lookback_days` sessions earlier. Harm: a falling 200-day average counts as a rising trend.
- `PBT criteria.pct_from_52w_low_contract` `pct_from_52w_low` passes exactly when the close is at least `min_pct` above the 252-session low (equal passes). Harm: stocks still near their lows pass the Stage 2 test.
- `PBT criteria.pct_from_52w_high_contract` `pct_from_52w_high` passes exactly when the close is within `max_pct_below` of the 252-session high (equal passes). Harm: stocks far off their highs pass as leaders.
- `PBT criteria.rs_rank_contract` `rs_rank` passes exactly when the stored `rs_pct` is at least the minimum, its score is the clamped linear curve, and `rs_pct` is the universe percentile of the return. Harm: relative-strength laggards enter the watchlist.
- `PBT criteria.momentum_rank_contract` `momentum_rank` puts an instrument in the top slice exactly when fewer than ceil(top% x N) instruments beat it; `any` and `all` combine the windows. Harm: the Qullamaggie scan selects the wrong stocks.
- `PBT criteria.consolidation_contract` `consolidation` finds the tightest window within `days` whose range (and higher lows) is within bounds, scores it, and reports the window high. Harm: a loose base is traded as a tight one.
- `PBT criteria.ma_surf_contract` `ma_surf` passes exactly when the close is within the maximum distance of every listed SMA, with a score that falls with distance. Harm: stocks far from the moving average are treated as surfing it.
- `PBT criteria.prior_move_contract` `prior_move` passes exactly when some low-then-high pair inside the trailing window rose at least `min_pct`, never using a high that came before its low. Harm: stocks with no prior run-up pass the EP setup.
- `PBT criteria.earnings_growth_contract` `earnings_growth` reads only rows filed on or before the date (point in time), and is no data without any. Harm: tomorrow's earnings leak into today's decision.
- `PBT criteria.neglect_contract` `neglect` measures the 63-session return on bars up to the previous session only, and passes at most the maximum. Harm: a stock that already ran passes as neglected, or the evaluation day's own move changes the answer.
- `PBT criteria.vcp_contract` `vcp` passes exactly when the contraction count, depth ratios, final depth and volume dry-up all meet the parameters, never reads a bar after the date, reports the pivot and final contraction low, and fails closed on missing data. Harm: a loose or noisy base is called a volatility contraction pattern and its stop sits in the wrong place.
- `PBT criteria.vcp_parity` The ported `vcp` finds the same contraction chain as a transcription of the legacy detector on generated bars, and matches a frozen oracle of the legacy output on accepted cases. Harm: the port quietly changes which patterns the bot trades.
- `PBT criteria.close_vs_pivot_contract` `close_vs_pivot` passes exactly when close x 100 <= pivot x (100 + `max_above_pct`) (equal passes), its value is the close's percentage above the pivot, a missing or non-positive pivot level fails with `no_pivot` and missing bars with `no_data`. Harm: a name that broke out days ago is published with its stale pivot and bought far above the base.
- `PBT criteria.close_vs_pivot_stage` In a stage, `close_vs_pivot` after `vcp` reads the pivot `vcp` reported for that date (`EvalContext.levels`, filled by `evaluate_stage` from earlier results' numeric extras); before `vcp`, after a `vcp` that found no base, or alone, it fails with `no_pivot`; the shared context is never changed. The stage's `levels`, and the levels the scan publishes, hold the first passed criterion's pivot. Harm: the gate checks a level from another symbol, date or playbook, or none.
- `PBT scoring.stage_levels` With a probe criterion that reports chosen levels and passes or fails, each criterion in a stage sees exactly the levels the passed criteria before it reported (first reporter per key), and the stage's `levels` equal the scan's published `levels`; a failed result adds none; the shared context is unchanged. Harm: the gate approves one level while the watchlist row, and so the trigger, uses another.
- `PBT scoring.numeric_levels` A result's levels are exactly its finite int, float and Decimal extras; flags, text, lists and non-finite values (a Decimal signalling NaN included) are dropped without raising. Harm: one odd extra aborts the nightly scan, or a NaN reaches a gate.
- `PBT config.close_vs_pivot_needs_vcp` A setup with an enabled `close_vs_pivot` is accepted exactly when an enabled `vcp` comes before it; otherwise `close_vs_pivot_needs_vcp`; a `close_vs_pivot` in score mode is refused (`close_vs_pivot_not_hard`). Harm: a config whose gate can never pass is saved and the playbook silently stops trading.
- `PBT scoring.weighted_mean` The stage score is the exact weighted mean of the score criteria (`sum(w s) / sum(w)`). Harm: a weight does not mean what the owner set.
- `PBT scoring.threshold_gate` A stage passes exactly when every hard criterion passed and the score is at least the threshold (equal passes); a stage with no score criterion scores 1.0. Harm: weak setups reach the watchlist or good ones are dropped.
- `PBT scoring.hard_short_circuit` A failing hard criterion stops its stage at once and is stored with its reason and value. Harm: later criteria run on a symbol already rejected, or the reason for the rejection is lost.
- `PBT scoring.disabled_is_noop` Evaluating with a criterion disabled equals evaluating without it. Harm: switching a rule off still changes who qualifies.
- `PBT scoring.stage_order` Enabled criteria evaluate in ascending `order`, whatever the list order, so the recorded hard failure is the lowest-order one; universe, regime, setup run in that order and stop at the first failure. Harm: the stored reason is not reproducible.
- `PBT config.instance_fields` Every stored instance has exactly the seven fields with every parameter spelled out, and a hard criterion's `weight` changes no result. Harm: a hard rule's weight leaks into the score, or two configs that mean the same hash differently.
- `PBT config.stage_slots` An instance fits only a section its type allows, only setup instances may score, and list order never changes the stored config. Harm: a live-only criterion runs in the nightly scan.
- `PBT config.invalid_never_saves` A config with an unknown type, an unknown or invalid parameter or a duplicate order raises a coded error and writes no version row or event. Harm: the engine runs a config whose meaning nobody checked.
- `PBT config.playbook_shape` A playbook holds a setup, triggers, entry filters and one stop rule; regime and exits cannot sit inside it; playbook names are unique. Harm: a playbook overrides what belongs to the bot, or two playbooks collide.
- `PBT config.no_risk_keys` A config naming a risk or sizing key at any depth is refused with `risk_key`. Harm: a strategy edit could raise risk without the cooling-off gate.
- `PBT config.yaml_round_trip` Export then import gives the same config and hash, the text is stable under a second export, and both seeds round-trip. Harm: an exported strategy cannot be re-imported unchanged.
- `PBT config.save_creates_version` Each save adds one draft with the next number (no gaps) whose config and hash are the validated ones, publishing freezes a draft, and a save inside the market session writes nothing. Harm: history has holes or a live strategy changes mid-session.
- `PBT config.new_bot_mode_shadow` A bot created through the service starts in shadow; any other requested mode is refused. Harm: a new bot could place real orders before it has a shadow record.
- `CONF criterion_instance_schema` An instance has exactly the seven fields of the 04 §2 table. Harm: a field the spec never approved changes what a criterion means.
- `CONF criterion_type_registry` The stored `param_schema` of every type equals the schema generated from its parameter model, and the registry holds exactly the catalogue. Harm: app and catalogue validation disagree.
- `CONF criterion_catalogue_complete` The registered types and their stages equal the 25 types of the 04 §4 table. Harm: a config refers to a type with no schema, or a type exists nobody specified.
- `CONF bot_seed_s1_matches_spec` The S1 seed file equals the YAML block of 04 §6 as data, `earnings_growth` off. Harm: the bot that runs is not the bot the owner approved.
- `CONF bot_seed_s2_matches_spec` The S2 seed file equals the YAML block of 04 §7 as data. Harm: the bot that runs is not the bot the owner approved.
- `CONF seed_bots_match_spec_yaml` Both seed files equal their spec YAML (every key, value, list order and `enabled` flag) and validate. Harm: the seeded bots drift from the approved design.
- `CONF bot_seed_default_mode` Both seeds declare `execution_mode: shadow`. Harm: a bot could start live.
- `CONF seed_bots_shadow` Every seeded bot row is shadow, one per seeded strategy. Harm: a new bot trades real money before shadow evidence exists.
- `CONF risk_policy_seed_bot_limits` The seed risk policy holds p_max 0.20 for S1, 0.25 for S2 and 6 open positions. Harm: the bots size against limits that differ from the approved design.
- `CONF import_graph_boundaries` The `app.hat.engine.criteria` package exists in the import graph (the rule is not vacuous) and reaches no broker, execution, database, ORM, router, Telegram or web module, directly or transitively. Harm: a criterion starts to depend on I/O and stops being a pure function of its data.
- `CONF mvp_seed_scope` The seeds create exactly S1 and S2 on NSE, and every strategy and bot row they create is owned by the one user (the broker half of this check is in verify-execution). Harm: a stray strategy or a row owned by nobody ships with the MVP.
- `PBT scan.watchlist_gates` A watchlist row exists exactly for the members that traded on the scan date (a member whose last bar is older is not evaluated) and pass the universe stage, the regime gate and the setup, in that order, with those three stages recorded as passed; a closed gate gives no rows; rows are best score first; bars after the scan date change nothing; a playbook scanned live only (episodic pivot) never produces a nightly row. Harm: a symbol shown that failed a stage proposes a trade the strategy would never take.
- `PBT scan.no_row_for_removed` An instrument removed from the universe produces no watchlist row and the rows of the others are unchanged; `scan_and_publish` uses the bot's own universe on the scan date and publishes once per bot and day. Harm: a removed stock keeps being proposed for entry.
- `PBT scan.rank_within_members` A universe rank (`momentum_rank`) is taken among the bot's members only: feature rows of instruments outside the universe change no row. Harm: another universe's leaders push this bot's stocks out of its top band.
- `PBT scan.no_universe_publishes_nothing` A bot with no universe publishes and records nothing, so the same day's run after a universe is assigned still publishes. Harm: an empty "published" watchlist blocks that day's real one.
- `PBT criteria.gap_up_contract` `gap_up` passes exactly when (open / previous close - 1) x 100 is at least `min_pct` (a 10.00% gap passes), and a missing or non-positive open or close fails closed. Harm: a 9.99% gap starts an EP trade the rule excludes, or a real 10% gap is missed.
- `PBT criteria.volume_pace_contract` `volume_pace` passes exactly when the day's volume projected to the full session (V / elapsed share, a `Fraction`) is at least `vs_avg50` x the 50-day average volume; before the first bar is final, or with no volume or average, it fails closed. Harm: a thin early burst fires a breakout the volume rule rejects.
- `PBT config.release_gate` On SQLite with the real `save_draft`, `publish_version`, `start_shadow`, `promote_version` and `assign_version` (PR 35): a version goes live only from shadow, outside market hours, after at least 20 trading sessions of shadow on the calendar; promotion retires the strategy's previous live version, moves the sleeves that ran it onto the new one and writes one `strategy.promoted` with the chat id; a refusal changes no version, sleeve or event; a Live or Probation sleeve is never pointed at a version that is not live. Harm: an untested edit trades real money, or two live versions of one strategy split the sleeves.
- `PBT criteria.early_volume_contract` `early_volume` passes exactly when the cumulative day volume at the trigger is at least `vs_adv20` x ADV(20); missing inputs fail closed. Harm: an EP gap on thin volume is bought.
- `PBT criteria.catalyst_attached_contract` `catalyst_attached` passes exactly when a matched announcement was published in [at - within_hours, at], both ends included and nothing after `at`; `required_in` never changes the result. Harm: a stale or future-dated announcement is taken as the catalyst of a confirm-mode EP trade.
- `PBT criteria.price_breaks_level_contract` Through the real bar builder and trigger monitor, a `price_breaks_level` playbook fires once, on the first final bar in [09:20, 15:20] whose high is strictly above the level, at that bar's final instant, with the level as the trigger price and the bar's close as the live price; the opening range level does not exist before its bar is final. Harm: a breakout is bought on a touch, before the opening range exists, or after 15:20.
- `PBT criteria.adtr_filter_contract` `adtr_filter` passes exactly when entry - stop is at most `max_multiple` x ADTR(20); a stop at or above the entry, or a missing input, fails closed. Harm: an entry whose stop is wider than the stock's daily range is stopped out by noise.
- `PBT criteria.max_chase_contract` `max_chase` passes exactly when price <= level x (1 + pct/100) and the stock is not locked at its upper circuit (unknown circuit state fails closed), and (`test_max_chase_always_applied`) the filters evaluated at a trigger always hold exactly one `max_chase`: an enabled one in place, a disabled or deleted one last with the last configured percentage, else the strategy default (S1 5, S2 2, else 2), even when a configured filter holds the highest allowed order (10000). Harm: deleting max_chase lets a bot buy any distance above the breakout, or into a locked circuit it cannot exit.
- `PBT criteria.trigger_conjunction` A playbook triggers exactly when it has at least one blocking enabled trigger criterion and every blocking one passes on the same bar; disabled criteria are not evaluated, `catalyst_attached` blocks only in a mode in its `required_in`, and a daily-bar trigger (`neglect`) counts with the result the candidate was built with and fails closed without one. Harm: one passing criterion fires an entry the others reject, or an all-disabled playbook buys every bar.
- `PBT criteria.entry_filters_skip` On SQLite with the real `open_entry`: the stop is planned first, then the entry filters in order with max_chase always present; the first failure is stored as a Rejected signal with that reason and stage `entry_filter`, no position, and no risk call; with no failure the signal goes to the risk check. Harm: a trade the filters reject is opened, or a skip leaves no reason.
- `PBT criteria.level_expression` `pivot` and `opening_range_high` name one level each and `max(consolidation_high, opening_range_high)` the larger of two that must both exist; a missing or non-positive level, or any other expression, has no level (`level_undefined`), and the config refuses an unknown expression. Harm: a zero level, or the consolidation high alone before the opening range exists, fires at the open.
- `PBT criteria.ep_candidate_bands` The EP candidates of a gap scan are, in scan order, exactly the gappers with a gap of at least 10% (exact), in the universe, with a known 10% or 20% band or no band (F&O), whose EP setup passes. Harm: a 2% or 5% band stock that can lock at its circuit after the entry becomes an EP trade.
- `PBT criteria.catalyst_required_in_confirm` On the seeded episodic-pivot playbook with every other trigger passing, a missing catalyst blocks exactly in the modes its `required_in` lists (the seed: confirm) and is recorded, not blocking, elsewhere. Harm: a confirm-mode EP trade reaches the owner with no news behind the gap, or shadow EP trades stop for want of a catalyst feed.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_criteria.py tests/hat/properties/test_scoring.py \
  tests/hat/properties/test_config.py tests/hat/properties/test_scan.py -q --no-cov
python -m pytest tests/hat/conformance/test_conf_criteria.py tests/hat/conformance/test_conf_boundaries.py \
  tests/hat/conformance/test_conf_broker.py -q --no-cov
```

Every check runs on synthetic bars and in-memory SQLite. None calls Fyers, NSE or any network, and none needs a credential or a data file (the VCP oracle is `tests/hat/fixtures/vcp_oracle.json`).

## Bug or model error

When a check fails, decide which side is wrong before touching anything.

- A property failure prints a falsifying example. Re-run it by hand against the criterion function (`app.hat.engine.criteria.evaluate`). If the criterion breaks its contract in `04 §4`, fix the code. If the contract in the spec is ambiguous, the expected value in the test is a model error: record the reading under "Scope changes" and in `docs/specs/hat/open_questions.md`.
- The contract tests restate each rule with `Fraction` or `Decimal` arithmetic that does not share code with the criterion, so a float rounding difference is a bug in the criterion, not in the test.
- `criteria.vcp_parity` compares with a transcription of the legacy detector (`services/vcp/pattern.py` at commit `56b9a08`) and a frozen oracle. A parity failure is either a bug in the port or an intended scope change: an intended change is listed below with its reason, and the oracle case is updated with it.
- Never weaken a property or shrink a strategy. Narrowing needs a reason under "Scope changes".
- A CONF seed check fails when the seed file and the spec YAML differ: the spec is authoritative, so change the file, unless the owner changed the design (then the spec changes first).
- Defaults marked `[OWNER-PENDING]` in the code (empty score 1.0, `sma_rising` as a point-to-point comparison, consolidation and `ma_surf` score curves, `prior_move` measurement, the VCP score curves) are tested as written. A new owner answer changes the code, the test and `open_questions.md` together.

## Updating

- A new criterion type: add its parameter model and evaluator under `engine/criteria`, register it in `engine/criteria/__init__.py` in the order of the 04 §4 table, add its row to the spec table first (`criterion_catalogue_complete` reads it), add its `<code>_contract` property to `test_criteria.py` and a bullet and scratch break here.
- A new config field or rule: the models are in `engine/strategy/config.py`; extend `hat_config_support.py` so the generators produce it, and keep `test_no_risk_keys` generating every key of the risk policy.
- A seed change: edit the spec YAML first, then the file under `engine/strategy/seeds/`; `seed_bots_match_spec_yaml` compares them.
- Live criteria (`gap_up`, `volume_pace`, `early_volume`, `catalyst_attached`, `price_breaks_level`, `adtr_filter`, `max_chase`) read a `LiveContext` (`engine/criteria/live.py`); the trigger stage and entry filters are combined in `engine/trigger.py` and turned into signals in `engine/live_entry.py`. A new live type adds its evaluator, a `<code>_contract` property built from `hat_trigger_support.py`, and a bullet and break here.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-criteria` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/engine/criteria/__init__.py`
- `backend/app/hat/engine/criteria/base.py`
- `backend/app/hat/engine/criteria/indicators.py`
- `backend/app/hat/engine/criteria/universe.py`
- `backend/app/hat/engine/criteria/setup.py`
- `backend/app/hat/engine/criteria/vcp.py`
- `backend/app/hat/engine/criteria/live.py`
- `backend/app/hat/engine/trigger.py`
- `backend/app/hat/engine/live_entry.py`
- `backend/app/hat/engine/strategy/__init__.py`
- `backend/app/hat/engine/strategy/config.py`
- `backend/app/hat/engine/strategy/scoring.py`
- `backend/app/hat/engine/strategy/versions.py`
- `backend/app/hat/engine/strategy/yamlio.py`
- `backend/app/hat/engine/strategy/seeds/stage2-contraction-breakout.yaml`
- `backend/app/hat/engine/strategy/seeds/momentum-breakout-ep.yaml`

Checks:
- `backend/tests/hat/hat_criteria_support.py`
- `backend/tests/hat/hat_config_support.py`
- `backend/tests/hat/properties/test_criteria.py`
- `backend/tests/hat/hat_trigger_support.py`
- `backend/tests/hat/properties/test_scoring.py`
- `backend/tests/hat/conformance/test_conf_criteria.py`
- `backend/tests/hat/fixtures/vcp_oracle.json`
- `backend/app/hat/engine/scan.py`
- `backend/app/hat/engine/watchlist.py`
- `backend/app/hat/engine/regime_gate.py`
- `backend/tests/hat/properties/test_scan.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `00e94d1ac797fd78f83f3ccae2556362841ba6a3add8a0da0096b519eca1edfb`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 12: `publish_version` also refuses a run that is not `completed` (`run_not_completed`, HAT-BT-061). The rule is proved by `backtest.version_status_requires_run` in `verify-backtest`; the checks here were re-run unchanged and pass.
- `rs_rank` has no `weights` parameter: the 3/6/9/12-month blend is fixed by the `rs_pct` feature column (02 §7), so a per-instance blend would not reach the data. `momentum_rank.windows` is a subset of the feature windows 21, 63, 126, 252.
- `vcp` is a port of the legacy detector, not a copy. Dropped (other criteria or outside the spec): the Stage 2 gate, the RS gate, overhead resistance, the volume slope, the grade and the breakout test. A contraction's depth uses the highest high and lowest low of its own bars. The final contraction low is the segment minimum, which can be lower than the legacy pivot low; `vcp_parity` records this. The pivot is the highest high of the final contraction. `MIN_BASE_BARS` stays 15. The score is the mean of four linear sub-scores (an `[OWNER-PENDING]` default).
- PR 15a evaluates the seven live criteria. `catalyst_attached` blocks only in the modes its `required_in` lists, whatever its `mode` field (HAT-CRIT-118 as revised), and `price_breaks_level` breaks on the final bar's high (HAT-CRIT-119 `[OWNER-PENDING]`). Only shadow bots get live candidates until the router (PR 15b); `open_entry` refuses confirm and auto.
- `neglect` is a trigger type that the Q-EP setup also holds, so it is evaluated here and allowed in a setup list (`extra_stages`).
- `PBT criteria.fundamentals_point_in_time` (HAT-DEC-I5) is not built: it needs the fundamentals feed (Phase 2). `earnings_growth_contract` covers the point-in-time rule on the criterion itself.
- `PBT regime.strategy_rules` (HAT-BOT-053) and the shared per-bot cap property of HAT-BOT-050 were built in PR 11 (`verify-portfolio`, `verify-execution`); the config model only stores the regime rule.
- The config does not store `version`; the version number lives on the row. The YAML shape of the seeds (implicit stage and order, one inline playbook named `default`) is accepted and stored in the canonical form (explicit stage and order, `playbooks` list).
- `create_bot` always starts shadow. A config's own `execution_mode` is accepted (the seeds declare shadow) but a mode change goes through the mode-change service of a later PR.
- `docs/specs/hat/04-criteria-and-bots.md` quotes `level: "max(consolidation_high, opening_range_high)"` in the S2 YAML: unquoted, the comma ends the flow mapping and the block does not parse.
- PR 11 built `PBT regime.strategy_rules` (recorded in `verify-portfolio`) and the shared per-bot cap property of HAT-BOT-050 (`PBT trades.shared_bot_cap`, recorded in `verify-execution`). The scan is one pure function of its inputs; its scheduling through `run_job` and the real inputs (bars, features, index closes) are PR 17.
- PR 15b widened the live trigger path (`engine/live_entry.py`) from shadow bots to every active bot with a live version: candidates carry the bot's effective mode for the session, and `route_by_mode` refuses an auto trigger until the router is wired (PR 17). No criteria property was narrowed; `test_criteria.py` and `test_bars.py` pass unchanged.
- PR 16: `route_by_mode` also refuses a confirm bot (`confirm_needs_router`): its approval card needs the router's token signer, so until PR 17 routes live triggers through the router only shadow triggers are written. `test_criteria.py` and `test_bars.py` pass unchanged.
- PR 54 (plan audit N2): a criterion reads the numeric `extras` of the criteria before it in its stage (`EvalContext.levels`, set by `evaluate_stage` on a copy of the context), which `close_vs_pivot` needs for `vcp`'s pivot. `close_vs_pivot` is a hard S1 setup criterion after `vcp`; a symbol whose `vcp` finds no base now stops at `close_vs_pivot` (`no_pivot`) instead of `below_threshold`. The scan publishes the setup stage's `levels` (`StageOutcome.levels`), so the gate and the watchlist row use one pivot; `ep_setup_passes` evaluates the EP setup through `evaluate_stage` (threshold 0, so only hard criteria decide) to pass levels too. `EvalContext` keeps its read bars in a dict its `with_levels` copies share. No property was narrowed; `hat_config_support` generates the new stop `min_pct` but not `close_vs_pivot` (valid only after a `vcp`; `config.close_vs_pivot_needs_vcp` covers it).
- PR 22: the opening range and the day low exist only for a symbol the bar builder watched from the open (HAT-DATA-114), so `hat_trigger_support.drive` first sends each symbol a pre-open tick (`from_open=True`, the default); `price_breaks_level_contract` is unchanged otherwise. The trigger monitor reads the day low from the bar view (`BarView.day_low`) instead of keeping its own; a late-seen candidate is checked by `bars.trigger_never_uses_a_partial_session` in `verify-live-data`.

- PR 35 (HAT-BOT-084, HAT-GOV-083): `versions.py` gains the release gate (`start_shadow`, `promote_version`, `assign_version`) and `release_overview`, which the admin command line calls (`verify-portfolio`, `admin.*`). The 20 sessions are counted with `lifecycle.shadow_gate` on the calendar's trading days, so `lifecycle.shadow_days` proves the count and `config.release_gate` its use. Nothing was narrowed.

## Scratch breaks

One recorded break per check (HAT-VER-072): the real code was changed as described, the check was run and failed, and the code was restored.

### PBT criteria.series_in_contract

Break: `series_in` passes a series that is not in the list.

```
E   AssertionError: assert False == ('EQ' in ['EQ'])
E    +  where False = CriterionResult(passed=False, score=0.0, value=None, reason='series_not_allowed', extras={}).passed
E   Failing test case: test_series_in_contract(
E       # The test always failed when commented parts were varied together.
E       rng=Lcg(0),  # or any other generated value
E       series='EQ',
E       allowed=['EQ'],  # or any other generated value
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
```

### PBT criteria.min_turnover_contract

Break: `min_turnover` requires strictly more than the minimum.

```
E   AssertionError: assert False == (Fraction(100000000, 1) >= (Fraction(10, 1) * (10 ** 7)))
E    +  where False = CriterionResult(passed=False, score=0.0, value=10.0, reason='turnover_below_min', extras={}).passed
E    +  and   Fraction(100000000, 1) = mean([Fraction(100000000, 1), Fraction(100000000, 1), Fraction(100000000, 1), Fraction(100000000, 1), Fraction(100000000, 1), Fraction(100000000, 1), ...])
E    +  and   Fraction(10, 1) = Fraction(10.0)
E   Failing explicit example: test_min_turnover_contract(
E       rng=<hat.hat_criteria_support.Lcg object at 0x7fe075739940>,
E       length=20,
E       days=20,
E       inr_cr=10.0,
E       tie=True,
```

### PBT criteria.min_price_contract

Break: `min_price` requires strictly more than the minimum.

```
E   AssertionError: assert False == (Fraction(50, 1) >= Fraction(50, 1))
E    +  where False = CriterionResult(passed=False, score=0.0, value=50.0, reason='price_below_min', extras={}).passed
E    +  and   Fraction(50, 1) = rupees(5000)
E    +  and   Fraction(50, 1) = Fraction('50.0')
E    +    where '50.0' = str(50.0)
E   Failing explicit example: test_min_price_contract(
E       rng=<hat.hat_criteria_support.Lcg object at 0x7f864cfdcb90>,
E       inr=50.0,
E       close=5000,
E   )
```

### PBT criteria.price_band_allowed_contract

Break: `price_band_allowed` passes a band outside the allowed set.

```
E   AssertionError: assert False == ('none' in [2, 5, 10, 20, 'none'])
E    +  where False = CriterionResult(passed=False, score=0.0, value=None, reason='band_not_allowed', extras={}).passed
E   Failing test case: test_price_band_allowed_contract(
E       # The test always failed when commented parts were varied together.
E       rng=Lcg(0),  # or any other generated value
E       history=[(
E            0,
E            None,  # or any other generated value
E        )],
E       allowed=None,  # or any other generated value
```

### PBT criteria.adr_min_contract

Break: `adr_min` requires strictly more than the minimum.

```
E   AssertionError: assert False == (Fraction(0, 1) >= Fraction(0, 1))
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.0, reason='adr_below_min', extras={}).passed
E    +  and   Fraction(0, 1) = Fraction('0.0')
E    +    where '0.0' = str(0.0)
E   Failing test case: test_adr_min_contract(
E       rng=Lcg(0),  # or any other generated value
E       style='flat',
E       length=5,
E       days=5,
E       pct=0.0,
```

### PBT criteria.sma_above_contract

Break: `sma_above` compares the two sides the wrong way round.

```
E   AssertionError: assert False == (Fraction(10231, 100) > Fraction(25493, 250))
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.338, reason='not_above', extras={}).passed
E   Failing test case: test_sma_above_contract(
E       rng=Lcg(0),  # or any other generated value
E       style='walk',
E       length=60,
E       a='close',
E       b=10,
E   )
E
```

### PBT criteria.sma_rising_contract

Break: `sma_rising` compares the SMA with its later value instead of its earlier one.

```
E   AssertionError: assert False == (Fraction(48061, 500) > Fraction(95651, 1000))
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.471, reason='not_rising', extras={}).passed
E   Failing test case: test_sma_rising_contract(
E       rng=Lcg(0),  # or any other generated value
E       style='walk',
E       length=30,
E       sma=10,
E       lookback=1,
E   )
E
```

### PBT criteria.pct_from_52w_low_contract

Break: `pct_from_52w_low` requires strictly more than the minimum.

```
E   AssertionError: assert False == ((((Fraction(100, 1) / Fraction(100, 1)) - 1) * 100) >= Fraction(0, 1))
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.0, reason='too_close_to_low', extras={}).passed
E    +  and   Fraction(100, 1) = rupees(10000)
E    +    where 10000 = Row(day=datetime.date(2024, 9, 8), high=10000, low=10000, close=10000, volume=3440941, traded=None).close
E    +  and   Fraction(0, 1) = Fraction('0.0')
E    +    where '0.0' = str(0.0)
E   Failing test case: test_pct_from_52w_low_contract(
E       rng=Lcg(0),  # or any other generated value
E       style='flat',
E       length=252,
```

### PBT criteria.pct_from_52w_high_contract

Break: `pct_from_52w_high` requires strictly less than the maximum distance.

```
E   AssertionError: assert False == (((1 - (Fraction(100, 1) / Fraction(100, 1))) * 100) <= Fraction(0, 1))
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.0, reason='too_far_below_high', extras={}).passed
E    +  and   Fraction(100, 1) = rupees(10000)
E    +    where 10000 = Row(day=datetime.date(2024, 9, 8), high=10000, low=10000, close=10000, volume=3440941, traded=None).close
E    +  and   Fraction(0, 1) = Fraction('0.0')
E    +    where '0.0' = str(0.0)
E   Failing test case: test_pct_from_52w_high_contract(
E       rng=Lcg(0),  # or any other generated value
E       style='flat',
E       length=252,
```

### PBT criteria.rs_rank_contract

Break: `rs_rank` requires strictly more than the minimum percentile.

```
|     assert evaluate("rs_rank", params, on_the_edge).passed  # exactly at the bar passes
| AssertionError: assert False
|     assert result.passed == (rs >= min_percentile)
| AssertionError: assert False == (0.0 >= 0.0)
```

### PBT criteria.momentum_rank_contract

Break: `momentum_rank` lets one more instrument into the top slice.

```
|     assert winners == sum(1 for c in column if c >= edge)
| AssertionError: assert 2 == 1
|     assert got == (all(per_window) if match == "all" else any(per_window))
| AssertionError: assert True == False
```

### PBT criteria.consolidation_contract

Break: `consolidation` requires a range strictly below the maximum.

```
E   AssertionError: assert False
E    +  where False = passes('consolidation', 'max_range_pct', 10.0, [Row(day=datetime.date(2024, 1, 1), high=110, low=100, close=100, volume=1000, traded=None), Row(day=datetime.date(202... volume=1000, traded=None), Row(day=datetime.date(
E    +    where 10.0 = float(10)
E   Failing test case: test_inclusive_edges_contract(
E       # The test always failed when commented parts were varied together.
E       k=1,  # or any other generated value
E       pct=10,  # or any other generated value
E   )
E
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQI=') as a decorator on your test function
```

### PBT criteria.ma_surf_contract

Break: `ma_surf` requires a distance strictly below the maximum.

```
E   AssertionError: assert False
E    +  where False = passes('ma_surf', 'max_distance_pct', 10.0, [Row(day=datetime.date(2024, 1, 1), high=9000, low=9000, close=9000, volume=1000, traded=None), Row(day=datetime.date(2024, 1, 2), high=11000, low=11000, close=11000, volume=
E    +    where 10.0 = float(10)
E   Failing test case: test_inclusive_edges_contract(
E       k=1,
E       pct=10,
E   )
E
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQI=') as a decorator on your test function
```

### PBT criteria.prior_move_contract

Break: `prior_move` requires strictly more than the minimum rise.

```
E   AssertionError: assert False
E    +  where False = passes('prior_move', 'min_pct', 10.0, [Row(day=datetime.date(2024, 1, 1), high=100, low=100, close=100, volume=1000, traded=None), Row(day=datetime.date(2024, 1, 2), high=110, low=100, close=100, volume=1000, traded=No
E    +    where 10.0 = float(10)
E   Failing test case: test_inclusive_edges_contract(
E       k=1,
E       pct=10,
E   )
E
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQI=') as a decorator on your test function
```

### PBT criteria.earnings_growth_contract

Break: `earnings_growth` also reads fundamentals filed after the date.

```
E   AssertionError: assert (not False and 'growth_below_min' == 'no_data'
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.0, reason='growth_below_min', extras={}).passed
E
E     - no_data
E     + growth_below_min)
E   Failing test case: test_earnings_growth_contract(
E       # The test sometimes passed when commented parts were varied together.
E       rng=Lcg(0),  # or any other generated value
E       filed=[(
E            1,
```

### PBT criteria.neglect_contract

Break: `neglect` includes the evaluation day's bar in the 63-day return.

```
|     assert result.passed == (ret <= Fraction(str(max_ret)))
| AssertionError: assert True == (Fraction(8600, 9427) <= Fraction(0, 1))
|     assert evaluate("neglect", params, context(changed)) == result  # today's bar is not read
| AssertionError: assert CriterionResu...d', extras={}) == CriterionResu...d', extras={})
|     assert not result.passed and result.reason == "no_data"
| AssertionError: assert (not False and 'already_extended' == 'no_data'
```

### PBT criteria.vcp_contract

Break: `vcp` ignores the volume dry-up rule.

```
E   AssertionError: assert Fraction(1529381, 3) <= (Fraction(1, 2) * Fraction(22502682, 25))
E    +  where Fraction(1, 2) = Fraction('0.5')
E    +    where '0.5' = str(0.5)
E   Failing test case: test_vcp_contract(
E       # The test always failed when commented parts were varied together.
E       rng=Lcg(0),
E       depths=[8, 3],
E       quiet=40,
E       params={
E           'contractions': [2, 6],
```

### PBT criteria.vcp_parity

Break: `vcp` looks one bar less to the right of a pivot.

```
E   assert [(0, 10)] == [(0, 7)]
E
E     At index 0 diff: (0, 10) != (0, 7)
E     Use -v to get more diff
E   Failing test case: test_vcp_parity(
E       # The test always failed when commented parts were varied together.
E       rng=Lcg(2279051),
E       depths=[3],  # or any other generated value
E       quiet=40,  # or any other generated value
E       walk=True,
```

### PBT criteria.close_vs_pivot_contract

Break: (PR 54) a close exactly at the limit fails (`<=` to `<` in `close_vs_pivot`).

```
E   AssertionError: assert False == True
E    +  where False = CriterionResult(passed=False, score=0.0, value=0.0, reason='above_pivot', extras={}).passed
E    +  and   True = close_vs_pivot_holds(Fraction(10007, 100), Fraction(10007, 100), 0.0)
E    +    where Fraction(10007, 100) = rupees(10007)
E   Failing test case: test_close_vs_pivot_contract(
E       # The test always failed when commented parts were varied together.
E       rng=Lcg(0),  # or any other generated value
FAILED tests/hat/properties/test_criteria.py::test_close_vs_pivot_contract - [cut]
```

Break: (PR 54) no pivot passes (`no_pivot` returns a pass).

```
E   AssertionError: assert (True, 'ok') == (False, 'no_pivot')
E
E     At index 0 diff: True != False
E     Use -v to get more diff
E   Failing test case: test_close_vs_pivot_contract(
E       rng=Lcg(0),
E       style='walk',
FAILED tests/hat/properties/test_criteria.py::test_close_vs_pivot_contract - [cut]
```

### PBT criteria.close_vs_pivot_stage

Break: (PR 54) `evaluate_stage` passes the bare context, so no criterion sees an earlier level.

```
E   AssertionError: assert False == True
E    +  where False = CriterionResult(passed=False, score=0.0, value=None, reason='no_pivot', extras={}).passed
E    +    where CriterionResult(passed=False, score=0.0, value=None, reason='no_pivot', extras={}) = CriterionOutcome(type='close_vs_pivot', order=1, mode='hard', weight=1.0, result=CriterionResult(passed=False, score=0.0, value=None, reason='no_pivot', extras={})).result
E    +  and   True = close_vs_pivot_holds(Fraction(9597, 100), Fraction(2422, 25), 0.0)
E    +    where Fraction(9597, 100) = rupees(9597)
E   Failing test case: test_close_vs_pivot_stage(
E       # The test always failed when commented parts were varied together.
FAILED tests/hat/properties/test_criteria.py::test_close_vs_pivot_stage - Ass [cut]
```

Break: (PR 54) `EvalContext.with_levels` writes the levels into the shared context (`staged = self`).

```
E   AssertionError: assert {'pivot': 96....3698961157904} == {}
E
E     Left contains 4 more items:
E     {'contraction_count': 2.0,
E      'dryup_ratio': 0.5663698961157904,
E      'final_contraction_low': 93.77,
E      'pivot': 96.88}
FAILED tests/hat/properties/test_criteria.py::test_close_vs_pivot_stage - Ass [cut]
```

### PBT scoring.stage_levels

Break: (PR 54) a later criterion overrides a level (`levels.setdefault(key, value)` to `levels[key] = value`).

```
FAILED tests/hat/properties/test_scoring.py::test_stage_levels - ExceptionGro [cut]
```

Break: (PR 54) the scan reads the last outcome's raw `extras` instead of the stage's `levels`.

```
E   AssertionError: assert {'pivot': 2.0} == {'pivot': 1.0}
E
E     Differing items:
E     {'pivot': 2.0} != {'pivot': 1.0}
E     Use -v to get more diff
E   Failing test case: test_stage_levels(
E       probes=[('pivot', 1.0, True, 'hard'), ('pivot', 2.0, True, 'hard')],
FAILED tests/hat/properties/test_scoring.py::test_stage_levels - AssertionErr [cut]
```

Break: (PR 54) a failed result's extras become levels (`if result.passed:` to `if True:`).

```
E   AssertionError: assert {'pivot': 1.0} == {}
E
E     Left contains 1 more item:
E     {'pivot': 1.0}
E     Use -v to get more diff
E   Failing test case: test_stage_levels(
E       # The test always failed when commented parts were varied together.
FAILED tests/hat/properties/test_scoring.py::test_stage_levels - AssertionErr [cut]
```

Break: (PR 54) `with_levels` writes into the shared context (`staged = self`).

```
E   AssertionError: assert {'pivot': 1.0} == {}
E
E     Left contains 1 more item:
E     {'pivot': 1.0}
E     Use -v to get more diff
E   Failing test case: test_stage_levels(
E       # The test always failed when commented parts were varied together.
FAILED tests/hat/properties/test_scoring.py::test_stage_levels - AssertionErr [cut]
```

### PBT scoring.numeric_levels

Break: (PR 54) a Decimal's finiteness is read with `math.isfinite`.

```
E   ValueError: cannot convert signaling NaN to float
E   Failing test case: test_numeric_levels(
E       extras={'': Decimal('sNaN')},
E   )
E
FAILED tests/hat/properties/test_scoring.py::test_numeric_levels - ValueError [cut]
```

### PBT config.close_vs_pivot_needs_vcp

Break: (PR 54) the config never refuses `close_vs_pivot` (`raise` to `pass`).

```
E   Failed: DID NOT RAISE ConfigError
E   Failing test case: test_close_vs_pivot_needs_vcp(
E       # The test sometimes passed when commented parts were varied together.
E       gate_at=0,  # or any other generated value
E       vcp_at=None,  # or any other generated value
E       gate_on=True,
E       vcp_on=False,  # or any other generated value
FAILED tests/hat/properties/test_config.py::test_close_vs_pivot_needs_vcp - F [cut]
```

Break: (PR 54, review round) a scored `close_vs_pivot` is saved (the `close_vs_pivot_not_hard` raise to `pass`).

```
    | AssertionError: assert 'close_vs_pivot_not_hard' in {'close_vs_pivot_needs_vcp'}
    | Failed: DID NOT RAISE ConfigError
FAILED tests/hat/properties/test_config.py::test_close_vs_pivot_needs_vcp - B [cut]
```

Break: (PR 54) a disabled `vcp` counts as the pivot source.

```
E   Failed: DID NOT RAISE ConfigError
E   Failing test case: test_close_vs_pivot_needs_vcp(
E       gate_at=0,  # or any other generated value
E       vcp_at=0,
E       gate_on=True,
E       vcp_on=False,
E   )
FAILED tests/hat/properties/test_config.py::test_close_vs_pivot_needs_vcp - F [cut]
```

### PBT scoring.weighted_mean

Break: The weight is left out of the weighted sum.

```
|     assert got == pytest.approx(float(want), abs=1e-12)
| AssertionError: assert 0.5 == 1.0 ± 1.0e-12
|     assert weighted_mean([(w * factor, s) for w, s in pairs]) == pytest.approx(got, abs=1e-12)
| AssertionError: assert 0.5 == 1.0 ± 1.0e-12
```

### PBT scoring.threshold_gate

Break: A score equal to the threshold fails.

```
|     assert passes_threshold(hard_ok, score, threshold) == (hard_ok and score >= threshold)
| AssertionError: assert False == ((True and 0.0 >= 0.0))
|     assert passes_threshold(True, threshold, threshold)  # exactly at the bar passes
| AssertionError: assert False
```

### PBT scoring.hard_short_circuit

Break: The failing hard criterion is not stored on the stage result.

```
E   AssertionError: assert (None is not None)
E    +  where None = StageOutcome(stage='setup', passed=False, score=0.0, outcomes=(CriterionOutcome(type='rs_rank', order=0, mode='hard', ...onResult(passed=False, score=0.0, value=50.0, reason='rs_below_min', extras={})),), failed=None, r
E   Failing test case: test_hard_short_circuit(
E       items=[Instance(type='rs_rank', stage='setup', mode='hard', weight=0.5, params={'min_percentile': 51.0, 'score_from': 70.0, 'score_full': 95.0}, enabled=True, order=0)],
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
E           /data/workspace/wt/hat/pr08-criteria/backend/app/hat/engine/strategy/scoring.py:78
E
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEBQQBBAUECQQNBBEEFQQZBB0EIAUEDQQAB') as a decorator on your test function
```

### PBT scoring.disabled_is_noop

Break: A disabled criterion is still evaluated.

```
E   AssertionError: assert StageOutcome(..., reason='ok') == StageOutcome(..., reason='ok')
E
E     Omitting 5 identical items, use -vv to show
E     Differing attributes:
E     ['outcomes']
E
E     Drill down into differing attribute outcomes:
E       outcomes: (CriterionOutcome(type='rs_rank', order=0, mode='hard', weight=0.5, result=CriterionResult(passed=True, score=0.0, value=50.0, reason='ok', extras={})),) != ()
E       Left contains one more item: CriterionOutcome(type='rs_rank', order=0, mode='hard', weight=0.5, result=CriterionResult(passed=True, score=0.0, value=50.0, reason='ok', extras={}))
E       Use -v to get more diff
```

### PBT scoring.stage_order

Break: The setup stage runs although the regime gate failed.

```
E   assert False
E    +  where False = all(<generator object test_stage_order.<locals>.<genexpr> at 0x7f0ed40d5ff0>)
E   Failing test case: test_stage_order(
E       series='EQ',
E       regime=False,
E       rs=0.0,  # or any other generated value
E   )
E
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEAAEEA') as a decorator on your test function
```

### PBT config.instance_fields

Break: `Instance` gains an eighth field, `note`.

```
E   AssertionError: assert {'enabled', '... 'stage', ...} == {'enabled', '..., 'type', ...}
E
E     Extra items in the left set:
E     'note'
E     Use -v to get more diff
E   Failing test case: test_instance_fields(
E       raw={'bot': 'alpha',
E        'universe': [],
E        'regime': {'index': 'NIFTY 500',
E            'rule': 'sma_fast_above_slow_both_rising',
```

### PBT config.stage_slots

Break: An instance is accepted in a section its type does not allow.

```
E   Failed: DID NOT RAISE ConfigError
E   Failing test case: test_stage_slots(
E       # The test always failed when commented parts were varied together.
E       raw={'bot': 'alpha', 'universe': [{'type': 'series_in', 'params': {
E              'series': ['EQ'],  # or any other generated value
E          }}], 'regime': {
E            'index': 'NIFTY 500',  # or any other generated value
E            'rule': 'sma_fast_above_slow_both_rising',
E            'params': {'fast': 10, 'slow': 20},
E        }, 'exits': [{'rule': 'hard_stop'}], 'setup': {'criteria': []}, 'trigger': [], 'entry_filters': [], 'stop': {
```

### PBT config.invalid_never_saves

Break: Instance params are not validated against the type's model.

```
E   Failed: DID NOT RAISE ConfigError
E   Failing test case: test_invalid_never_saves(
E       raw={'bot': 'alpha',
E        'universe': [{'type': 'series_in', 'params': {'series': ['EQ']}}],
E        'regime': {'index': 'NIFTY 500',
E            'rule': 'sma_fast_above_slow_both_rising',
E            'params': {'fast': 10, 'slow': 20}},
E        'exits': [{'rule': 'hard_stop'}],
E        'playbooks': [{'name': 'breakout',
E          'setup': {'criteria': []},
```

### PBT config.playbook_shape

Break: Two playbooks with one name are accepted.

```
E   Failed: DID NOT RAISE ConfigError
E   Failing test case: test_playbook_shape(
E       raw={'bot': 'alpha',
E        'universe': [],
E        'regime': {'index': 'NIFTY 500',
E            'rule': 'sma_fast_above_slow_both_rising',
E            'params': {'fast': 10, 'slow': 20}},
E        'exits': [{'rule': 'hard_stop'}],
E        'playbooks': [{'name': 'breakout',
E          'setup': {'criteria': []},
```

### PBT config.no_risk_keys

Break: The risk-key scan finds nothing.

```
E   AssertionError: assert 'risk_key' in {'extra_forbidden'}
E    +  where {'extra_forbidden'} = codes_of({'bot': 'alpha', 'universe': [], 'regime': {'index': 'NIFTY 500', 'rule': 'sma_fast_above_slow_both_rising', 'params': {'fast': 10, 'slow': 20}}, 'exits': [{'rule': 'hard_stop'}], ...})
E   Failing test case: test_no_risk_keys(
E       # The test always failed when commented parts were varied together.
E       raw={'bot': 'alpha',
E        'universe': [],
E        'regime': {'index': 'NIFTY 500',
E            'rule': 'sma_fast_above_slow_both_rising',
E            'params': {'fast': 10, 'slow': 20}},
E        'exits': [{'rule': 'hard_stop'}],
```

### PBT config.yaml_round_trip

Break: `export_yaml` drops every exit rule after the first.

```
E   AssertionError: assert 'fde6cfc508ae...455baa39549c4' == 'd5c64d509134...4e749d1dd6e65'
E
E     - d5c64d509134feaf0fb51cc2db95e2a243a35271f550df87ab94e749d1dd6e65
E     + fde6cfc508ae108aa2341f741a60049b55f4ac7f317c0363655455baa39549c4
E   Failing test case: test_yaml_round_trip(
E       # The test always failed when commented parts were varied together.
E       raw={'bot': 'alpha', 'universe': [], 'regime': {
E            'index': 'NIFTY 500',  # or any other generated value
E            'rule': 'sma_fast_above_slow_both_rising',
E            'params': {'fast': 10, 'slow': 20},
```

### PBT config.save_creates_version

Break: The next version number skips one.

```
E   assert 4 == ((1 + 0) + 1)
E    +  where 4 = <app.hat.models.strategy.HatStrategyVersion object at 0x7fe8ff2d2360>.version
E   Failing test case: test_save_creates_version(
E       raws=[{'bot': 'alpha',
E         'universe': [],
E         'regime': {'index': 'NIFTY 500',
E             'rule': 'sma_fast_above_slow_both_rising',
E             'params': {'fast': 10, 'slow': 20}},
E         'exits': [{'rule': 'hard_stop'}],
E         'playbooks': [{'name': 'breakout',
```

### PBT config.new_bot_mode_shadow

Break: `create_bot` starts a bot in `confirm`.

```
E   AssertionError: assert 'confirm' == 'shadow'
E
E     - shadow
E     + confirm
E   Failing test case: test_new_bot_mode_shadow(
E       mode=None,
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
E           /data/workspace/wt/hat/pr08-criteria/backend/app/hat/engine/strategy/versions.py:219
```

### CONF criterion_instance_schema

Break: `Instance` gains an eighth field, `note`.

```
E   AssertionError: assert {'enabled', '... 'stage', ...} == {'enabled', '..., 'type', ...}
E
E     Extra items in the left set:
E     'note'
E     Use -v to get more diff
```

### CONF criterion_type_registry

Break: `sync_criterion_types` stores an empty `param_schema`.

```
E   AssertionError: series_in
E   assert {} == {'additionalP...pe': 'object'}
E
E     Right contains 4 more items:
E     {'additionalProperties': False,
E      'properties': {'series': {'items': {'type': 'string'},
E                                'maxItems': 20,
E                                'minItems': 1,
E                                'title': 'Series',
E
```

### CONF criterion_catalogue_complete

Break: `max_chase` is left out of the registry.

```
E   AssertionError: assert {'series_in':...niverse', ...} == {'series_in':...niverse', ...}
E
E     Omitting 23 identical items, use -vv to show
E     Right contains 1 more item:
E     {'max_chase': 'entry_filter'}
E     Use -v to get more diff
```

Break: (PR 54) the `close_vs_pivot` row is left out of the 04 §4 table.

```
E   AssertionError: assert 24 == 25
E    +  where 24 = len({'series_in': 'universe', 'min_turnover': 'universe', 'min_price': 'universe', 'price_band_allowed': 'universe', ...})
FAILED tests/hat/conformance/test_conf_criteria.py::test_criterion_catalogue_complete
```

### CONF bot_seed_s1_matches_spec

Break: The S1 seed allows a stop 9% below entry (the spec says 8%).

```
E   AssertionError: assert {'bot': 'stag...'shadow', ...} == {'bot': 'stag...'shadow', ...}
E
E     Omitting 10 identical items, use -vv to show
E     Differing items:
E     {'stop': {'rule': 'below_final_contraction_low', 'buffer_pct': 0.5, 'max_pct': 9}} != {'stop': {'rule': 'below_final_contraction_low', 'buffer_pct': 0.5, 'max_pct': 8}}
E     Use -v to get more diff
```

### CONF bot_seed_s2_matches_spec

Break: The S2 seed's gap threshold is 12% (the spec says 10%).

```
E   AssertionError: assert {'bot': 'mome...'shadow', ...} == {'bot': 'mome...'shadow', ...}
E
E     Omitting 7 identical items, use -vv to show
E     Differing items:
E     {'playbooks': [{'name': 'breakout', 'setup': {'threshold': 0.6, 'criteria': [{'type': 'momentum_rank', 'mode': 'hard',...lters': [{'type': 'adtr_filter', 'params': {'max_multiple': 1.5}}, {'type': 'max_chase', 'params': {'pct': 2}}],
E     Use -v to get more diff
```

### CONF seed_bots_match_spec_yaml

Break: The S1 seed enables `earnings_growth` (the spec has it off).

```
E   AssertionError: assert {'bot': 'stag...'shadow', ...} == {'bot': 'stag...'shadow', ...}
E
E     Omitting 10 identical items, use -vv to show
E     Differing items:
E     {'setup': {'threshold': 0.6, 'criteria': [{'type': 'sma_above', 'mode': 'hard', 'params': {'a': 'close', 'b': 150}}, {...': 'hard', 'params': {'a': 50, 'b': 150}}, {'type': 'sma_above', 'mode': 'hard', 'params': {'a': 50, 'b': 200}},
E     Use -v to get more diff
```

### CONF bot_seed_default_mode

Break: The S2 seed declares `execution_mode: confirm`.

```
E   AssertionError: assert 'confirm' == 'shadow'
E
E     - shadow
E     + confirm
```

### CONF seed_bots_shadow

Break: `create_bot` starts a bot in `confirm`.

```
E   AssertionError: assert {'confirm'} == {'shadow'}
E
E     Extra items in the left set:
E     'confirm'
E     Extra items in the right set:
E     'shadow'
E     Use -v to get more diff
```

### CONF risk_policy_seed_bot_limits

Break: The seed policy gives S2 a position cap of 0.30.

```
E   AssertionError: assert {'stage2-cont...t-ep': '0.30'} == {'stage2-cont...t-ep': '0.25'}
E
E     Omitting 1 identical items, use -vv to show
E     Differing items:
E     {'momentum-breakout-ep': '0.30'} != {'momentum-breakout-ep': '0.25'}
E     Use -v to get more diff
```

### CONF import_graph_boundaries

Break: `criteria/base.py` imports `fastapi`.

```
E   AssertionError: assert {'app.hat.eng...astapi'], ...} == {}
E
E     Left contains 7 more items:
E     {'app.hat.engine.criteria': ['app.hat.engine.criteria',
E                                  'app.hat.engine.criteria.base',
E                                  'fastapi'],
E      'app.hat.engine.criteria.base': ['app.hat.engine.criteria.base', 'fastapi'],
E      'app.hat.engine.criteria.indicators': ['app.hat.engine.criteria.indicators',
E
E     ...Full output truncated (15 lines hidden), use '-vv' to show
```

### CONF mvp_seed_scope

Break: The seeded strategies are owned by a different user than their bots.

```
E   AssertionError: <class 'app.hat.models.strategy.HatStrategy'>
E   assert {UUID('59bd34...5deccc630a5')} == {UUID('931739...9af3f65ae0e')}
E
E     Extra items in the left set:
E     UUID('59bd34cc-a4d2-4223-84b8-cb1410143223')
E     UUID('af4ac8a5-b3b0-4760-a242-c5deccc630a5')
E     Extra items in the right set:
E     UUID('931739cb-92c8-4e63-81e1-b9af3f65ae0e')
E     Use -v to get more diff
```

### PBT scan.watchlist_gates

Break: in `backend/app/hat/engine/scan.py`: a closed regime gate no longer stops the scan.

```
FAILED tests/hat/properties/test_scan.py::test_watchlist_gates - ExceptionGro [cut]
============================== 1 failed in 1.64s ===============================
```

Break: in `backend/app/hat/engine/scan.py`: a member whose last bar is before the scan date is evaluated.

```
FAILED tests/hat/properties/test_scan.py::test_watchlist_gates - AssertionErr [cut]
```

### PBT scan.no_row_for_removed

Break: in `backend/app/hat/engine/scan.py`: every instrument with bars is scanned, whether or not it is in the universe.

```
E   AssertionError: assert not ({'00000000-0000-0000-0000-000000000004'} & {'00000000-0000-0000-0000-000000000004'})
FAILED tests/hat/properties/test_scan.py::test_no_row_for_removed - Assertion [cut]
============================== 1 failed in 1.82s ===============================
```

### PBT scan.rank_within_members

Break: in `backend/app/hat/engine/scan.py`: the features are not restricted to the members before the rank.

```
E       rows: () != (WatchRow(playbook='default', ... [cut]
```


### PBT scan.no_universe_publishes_nothing

Break: in `backend/app/hat/engine/watchlist.py`: a bot without a universe publishes an empty watchlist and its event.

```
FAILED tests/hat/properties/test_scan.py::test_no_universe_publishes_nothing
```

### PBT criteria.gap_up_contract

Break: in `backend/app/hat/engine/criteria/live.py`: `gap >= min_pct` became `>` (a gap exactly at the minimum fails).

```
E   AssertionError: assert False == (Fraction(10, 1) >= Fraction(10, 1))
E    +  where False = CriterionResult(passed=False, score=0.0, value=10.0, reason='gap_too_small', extras={}).passed
E    +  and   Fraction(10, 1) = oracle_pct(10.0)
E   Failing explicit example: test_gap_up_contract(
E       open_=11000,
E       prev=10000,
```

### PBT criteria.volume_pace_contract

Break: in `backend/app/hat/engine/criteria/live.py`: `projected >= needed` became `>`.

```
E   AssertionError: assert False == (Fraction(1500, 1) >= (Fraction(3, 2) * 1000))
E    +  where False = CriterionResult(passed=False, score=0.0, value=1500.0, reason='volume_pace_low', extras={}).passed
E    +  and   Fraction(3, 2) = oracle_pct(1.5)
E   Failing explicit example: test_volume_pace_contract(
E       volume=1500,
E       bars=75,
```

### PBT criteria.early_volume_contract

Break: in `backend/app/hat/engine/criteria/live.py`: `cum_volume >= needed` became `>`.

```
E   AssertionError: assert False == (500 >= (Fraction(1, 2) * 1000))
E    +  where False = CriterionResult(passed=False, score=0.0, value=500.0, reason='early_volume_low', extras={}).passed
E    +  and   Fraction(1, 2) = oracle_pct(0.5)
E   Failing explicit example: test_early_volume_contract(
E       volume=500,
E       adv=1000,
```

### PBT criteria.catalyst_attached_contract

Break: in `backend/app/hat/engine/criteria/live.py`: the window lost its upper end (`since <= t`), so a future-dated announcement counts.

```
E   AssertionError: assert True == False
E    +  where True = CriterionResult(passed=True, score=1.0, value=-0.0002777777777777778, reason='ok', extras={}).passed
E    +  and   False = bool([])
E   Failing explicit example: test_catalyst_attached_contract(
E       offsets=[-1],
E       hours=1,
```

### PBT criteria.price_breaks_level_contract

Break: in `backend/app/hat/engine/criteria/live.py`: `high > level` became `>=` (a touch is a break).

```
E   AssertionError: assert datetime.datetime(2026, 10, 21, 9, 20, 2, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) == (datetime.datetime(2026, 10, 21, [cut]
E    +  where datetime.datetime(2026, 10, 21, 9, 20, 2, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) = Trigger(candidate=Candidate(bot_id=UUID('00000 [cut]
E    +  and   datetime.datetime(2026, 10, 21, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) = ist(datetime.date(2026, 10, 21), 9, 15)
```

### PBT criteria.adtr_filter_contract

Break: in `backend/app/hat/engine/criteria/live.py`: `width <= max_multiple x ADTR` became `<`.

```
E   AssertionError: assert False == ((10000 - 9000) <= (Fraction(1, 1) * 1000))
E    +  where False = CriterionResult(passed=False, score=0.0, value=1.0, reason='stop_wider_than_adtr', extras={}).passed
E    +  and   Fraction(1, 1) = oracle_pct(1.0)
E   Failing explicit example: test_adtr_filter_contract(
E       entry=10000,
E       stop=9000,
```

### PBT criteria.max_chase_contract

Break: in `backend/app/hat/engine/criteria/live.py`: the upper-circuit check was removed (`if ctx.locked_upper_circuit:` became `if False:`).

```
E   AssertionError: assert True == ((not True))
E    +  where True = CriterionResult(passed=True, score=1.0, value=0.0, reason='ok', extras={}).passed
E   Failing explicit example: test_max_chase_contract(
E       price=10000,
E       level=10000,
E       pct=2.0,
```

Break: in `backend/app/hat/engine/trigger.py`: `entry_filter_instances` returns the enabled filters as they are (`if any(...MAX_CHASE...)` became `if True:`), so a deleted max_chase is not applied.

```
E   assert 0 == 1
E    +  where 0 = len([])
E   Failing test case: test_max_chase_always_applied(
E       # The test always failed when commented parts were varied together.
E       filters=([], 'deleted'),
E       configured=None,  # or any other generated value
```

Break: in `backend/app/hat/engine/trigger.py`: the forced max_chase is built as a validated config `Instance`, so a filter at order 10000 gives it order 10001 and planning fails.

```
E   pydantic_core._pydantic_core.ValidationError: 1 validation error for Instance
E   order
E     Input should be less than or equal to 10000 [type=less_than_equal, input_value=10001, input_type=int]
```

### PBT criteria.trigger_conjunction

Break: in `backend/app/hat/engine/trigger.py`: `fired = bool(blocking_results) and all(blocking_results)` became `all(blocking_results)` (an empty trigger set fires).

```
E   assert True == ((False))
E    +  where True = TriggerEvaluation(fired=True, outcomes=(), level=None).fired
E    +  and   False = bool([])
```

Break: in `backend/app/hat/engine/trigger.py`: `evaluate_trigger` ignores the candidate's daily-bar results (`result = None`), so `neglect` is evaluated live and never passes.

```
E   AssertionError: assert False == ((True and True))
E    +  where False = TriggerEvaluation(fired=False, outcomes=(CriterionOutcome(type='neglect', order=0, blocking=True, result=CriterionResult(passed=False, sco [cut]
E   Failing explicit example: test_trigger_conjunction(
```

### PBT criteria.entry_filters_skip

Break: in `backend/app/hat/engine/trigger.py`: every failing filter overwrites the skip (`if skip is None and not result.passed` became `if not result.passed`), so the last failure, not the first, is stored.

```
E   AssertionError: assert 'max_chase' == 'adtr_filter'
E     
E     - adtr_filter
E     + max_chase
E   Failing test case: test_entry_filters_skip(
E       # The test always failed when commented parts were varied together.
```

### PBT criteria.level_expression

Break: in `backend/app/hat/engine/criteria/live.py`: `resolve_level` returns the larger of the known levels even when one is missing.

```
E   AssertionError: assert Decimal('0.01') == None
E    +  where Decimal('0.01') = resolve_level('max(consolidation_high, opening_range_high)', {'pivot': None, 'consolidation_high': None, 'opening_rang [cut]
E    +  and   None = maybe(None)
E   Failing test case: test_level_expression(
```

### PBT criteria.ep_candidate_bands

Break: in `backend/app/hat/engine/live_entry.py`: `ep_candidates` dropped the EP setup condition.

```
E   AssertionError: assert ['S0'] == []
E     
E     Left contains one more item: 'S0'
E     
```

### PBT criteria.catalyst_required_in_confirm

Break: in `backend/app/hat/engine/trigger.py`: every trigger criterion blocks (`blocking = True`), ignoring `required_in`.

```
E   AssertionError: assert True == ('shadow' in ['confirm'])
E   AssertionError: assert False == ((False or 'shadow' not in ['confirm']))
============================== 1 failed in 1.36s ===============================
```

### PBT config.release_gate

Break: (1) in `backend/app/hat/engine/strategy/versions.py`: `promote_version` skips the 20-session check.

```
E   Failed: DID NOT RAISE VersionError
E   Failing explicit example: test_release_gate(
E       state='shadow',
E       waited=19,
```

Break: (2) `promote_version` leaves the sleeves on the version it replaces.

```
E   AssertionError: assert UUID('bdb267bc-d16e-4df5-8582-211ca6e2d722') == UUID('ab2ec9ca-ce2d-4de4-b352-ed088a69b4b5')
E    +  where UUID('bdb267bc-...') = <app.hat.models.strategy.HatBot object at 0x7f472bd41d30>.live_version_id
E   Failing explicit example: test_release_gate(
E       state='live',
```

Break: (3) `promote_version` does not retire the previous live version.

```
E   sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: hat_strategy_version.strategy_id
E   [SQL: UPDATE hat_strategy_version SET status=?, promoted_at=? WHERE hat_strategy_version.id = ?]
```

Break: (4) `assign_version` points a Live sleeve at a version still in shadow.

```
E   Failed: DID NOT RAISE VersionError
E   Failing explicit example: test_release_gate(
E       state='live',
E       waited=20,
```
