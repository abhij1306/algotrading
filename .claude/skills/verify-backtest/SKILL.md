---
name: verify-backtest
description: How to run, read and update the checks behind Hire a Trader's backtest (the shared day loop over the live engine, the daily-bar fill proxies, slippage and the statutory cost model, run recording and trial counting, the deflated Sharpe ratio, the lifecycle and risk-increase evidence, determinism and the S1/S2 golden replay). Use before and after any change to backend/app/hat/engine/backtest.py, backend/app/hat/backtest (costs, fills, dsr, runs, market, cost_models), the backtest fixtures or the backtest, costs and dsr properties.
---

# verify-backtest

Checks that the backtest (`backend/app/hat/engine/backtest.py` and `backend/app/hat/backtest/`) tells the truth about a strategy version: it runs the live scan, risk and exit code rather than copies, it never reads the future or forgets dead stocks, its fills are never better than the market allowed and pay every charge, every run is a counted trial, the deflated Sharpe ratio matches the published formula and is recomputed when the family grows, and the same inputs always give the same bytes. Requirement IDs (index: `docs/invariants.md`): (to 061), (the gate evidence),.

## Properties

- `CONF backtest.backtest_shares_engine_imports` The day loop imports the live engine functions (`scan_and_publish`, `open_entry`, `evaluate_position`, `close_position`, `expire_watchlist`, `plan_stop`, `check_width`), never the scan, criteria, scoring, sizing, caps or decision modules behind them, defines no function named like the live logic, and passes no other `decide` to `open_entry`; the cost and fill modules import no engine code. Harm: a fix applied to the live path silently diverges from the backtest, and the evidence describes a strategy that never runs live.
- `PBT backtest.scan_parity` The watchlist the loop publishes for each simulated date equals the live `scan_bot` of the previous session run directly on the same market (same rows, pivots, stops, scores within 1e-6). Harm: the backtest trades setups the live scan would not find.
- `PBT backtest.nightly_scan_parity` The watchlist the nightly job publishes on night d (`Nightly.features`, then `Nightly.scan`, over a store holding raw bars through d and a split of the first instrument stored by d, ex-dated on or before the next session) equals the loop's watchlist for the next session, the loop run later from the store through `load_market` (same rows, levels and scores; ). Harm: the backtest scans inputs the nightly job never reads (another window, unadjusted prices, other features), so its evidence describes a strategy that never runs live.
- `PBT backtest.no_lookahead` Replacing every bar and index close after a date d leaves every watchlist, signal, fill, closed trade and equity point on or before d unchanged. Harm: the backtest uses prices the market had not printed, and every sleeve looks better than it is.
- `PBT backtest.nightly_no_lookahead` Replaying night d's features and scan on a store that already holds later sessions (another future for a random instrument, other index closes, the later nights' features) publishes the same watchlist, ADTR and criteria results included, as night d on the store cut at d. Harm: the nightly path the backtest shares reads a later bar, index close or feature partition, so a replayed signal is not one the market could have produced that night.
- `PBT backtest.survivorship_free` An instrument that stops trading at session x is scanned as a member before x, no fill of it is dated after its last bar, and every position in it closes no later than that bar. Harm: dead stocks vanish from the test and returns are overstated.
- `PBT backtest.market_point_in_time` `load_market` reads bars and index closes up to the period's end only; an instrument delisted by the end has its last bar on or before its delisting as its last traded session, one delisted later has none; industry classifications come with their effective dates and `industry_on` gives the one in force on each day. Harm: a run closes a live stock as delisted, or a later reclassification changes an earlier industry-cap decision.
- `PBT backtest.features_digest_pinned` `load_market` refuses (`backtest_features_stale`) unless the features digest the last complete `features` run stored equals `inputs_digest(stored_factors, None)` and every session of the window has a features partition, read under the features job's lock (a split stored after the last run, a rebuild ranked over a narrower universe, a last run stopped before the last session and no run at all are refused); when it loads, the snapshot's `actions_digest` is that digest and the features it read equal a full rebuild over the same bars and actions. Harm: a run mixes prices adjusted for a split with features computed before it or over another universe, which the nightly scan never sees.
- `PBT backtest.version_pinned` Every signal and trade carries the run's strategy and risk-policy versions, and the metrics name them. Harm: evidence cannot be tied to the version being promoted.
- `PBT backtest.sized_from_allocation` Each trade date's allocation (`BacktestResult.allocations`, and `SignalRow.allocation` on each signal) is the live allocator's for one Live sleeve of health 1: capital = weight x regime multiplier x the account's mark at the night's close (the initial equity before the period), rounded down to money places, risk per trade = the policy's x the multiplier; the multiplier is 1 under `regime_mult` `1` (clamped to the policy's bounds) and under `policy` regime v1 (inputs, 3-day rule from the run's first session, default table, policy bounds, 0.25 smoothing), written out in the test; a night without regime inputs has no allocation and that day's entries are rejected unsized; no accepted entry risks more than capital x risk per trade. Harm: the backtest sizes trades as live never would, so its percentage results describe another book.
- `PBT backtest.invested_share` Each session's invested share (marked positions over the account's mark) is in [0, 1] and above 0 exactly when a trade was open at the close; `metrics.invested_share` is their mean and `metrics.pct_basis` names the account, the weight and the regime rule. Harm: the report hides how little of the account a sleeve deploys.
- `PBT backtest.sizing_conventions_refused` A sleeve weight that is not a Decimal in (0, 1] (`backtest_bad_sleeve_weight`), an unknown regime rule (`backtest_bad_regime_mult`), for a promotion run a weight of its own (`backtest_promotion_off_base_weight`) or regime rule `1` (`backtest_promotion_regime_variant`), with no weight given a policy without the sleeve's base weight or with one of 0 (`backtest_no_base_weight`), and a weight above the policy's cluster cap (`backtest_weight_above_cluster_cap`) are refused; otherwise `conventions` records the weight (normalised), the rule and, under `policy` only, the regime table. Harm: a run sized on a weight of 0, above the account or on a guess passes for evidence.
- `PBT backtest.regime_inputs_loaded` `load_market(regime_inputs=True)` reads the NIFTY 500 and India VIX closes and the features job's breadth as `portfolio.jobs.read_series` and `read_breadth` do, records `breadth_points`, and refuses a store without a VIX close or any breadth (`backtest_no_regime_inputs`); without the flag it reads neither. Harm: the regime-sized backtest reads other inputs than the nightly regime job.
- `PBT backtest.entry_fill_proxy` A gap over (open above the level) fills at the open under either rule; otherwise the default `penetration_1_tick` fills at the first tick-grid price above the level (level + tick on the grid, the next grid price for an off-grid adjusted level) when the high reaches it and `touch` at the level when the high reaches the level, else no entry; penetration never fills where touch does not nor below its price, and without a tick there is no penetration entry. Harm: the backtest buys breakouts live never sees trade, at a better price than live pays.
- `PBT backtest.entry_fill_proxy_chase` In a run under either entry rule (drawn), every opened entry records its level as the trigger price and its buy filled at that rule's proxy price on the instrument's tick and within max chase of the level, with the chase drawn from 0.5%, 1% and 5%. Harm: the backtest buys gaps the live risk check refuses, or ignores the rule a run asked for.
- `PBT backtest.fill_conventions_recorded` Every run's metrics record `fill_path`, `entry_fill_rule`, `gap_through_rule`, `trigger_proxy`, `slippage_bps` (normalised), `cost_model`, `slot_order`, the path rules `ambiguous` (`optimistic` by default), `lower_circuit` and `suspension_markdown`, the sizing `sleeve_weight` (the policy's base weight, 0.5 for S1, by default), `regime_mult` (`1` by default) and `health_mult`, the same thirteen as one `conventions` block, `for_promotion` and `config_digest` (the canonical hash of the first thirteen, which is exactly the block), and a `touch` run appends `entry_fills_on_touch` to `known_gaps`; a run flagged for promotion at a slippage of 0 or under `touch` is refused (`backtest_promotion_without_slippage`, `backtest_promotion_on_touch`; ). Harm: runs under different fill conventions read as comparable evidence, or a promotion rides on perfect fills.
- `PBT backtest.fill_conventions_recorded_digest` Two specs' `config_digest`s are equal exactly when their entry rule, slippage (20 and 20.0 bps alike, -0 and 0 alike), cost model, slot order, ambiguous-day rule, bar-less sessions before the markdown, sleeve weight (none given is the policy's 0.50; 0.5 and 0.50 alike) and regime rule are. Harm: a run under other conventions passes for a rerun, or a rerun reads as another convention.
- `PBT backtest.slot_order_rank` Under `time` candidates take the slots in estimated trigger order (every gap over first: an open above the level, or at it under `touch`; then (level - open) / open ascending, compared by cross-multiplying; no level or open last; ties in score order); `score` keeps the score order; `random:K` is a permutation the same K and day reproduce. Harm: on a capped day the backtest gives the slots to the best-scored setups, not the first to break.
- `PBT backtest.slot_order_refused` A slot order other than `time`, `score` or `random:K` (ASCII digits, no leading zeros, below 10^9) is refused `backtest_bad_slot_order` by `check_conventions`, and a run flagged for promotion under any order but `time` is refused `backtest_promotion_off_time_order`. Harm: two spellings of one rule hash apart, an unknown order reads as the default, or a version is promoted on a slot allocation live cannot get.
- `PBT backtest.slot_order_applied` In a run with one open slot (generated markets with up to six instruments, so most trigger days are capped), each day's triggers reach `evaluate_signal` in the run's slot order computed from the day's watchlist and bars, no trigger takes the slot after an earlier one that day was refused `max_positions`, and `metrics.slot_order` records the rule. Harm: the backtest fills a capped day's slot with a later break than live would take.
- `PBT backtest.raised_stop_dip` The day after an entry (a flat consolidation, then a break at 101.01 with its 5% stop at 95.95), with the high, low, open and close drawn around 1R: the day is ambiguous exactly when the breakeven rule raised the stop on the way up (high at 1R or above, open below it), the low reaches the raised stop and the close is above it, and `metrics.ambiguous_days` counts it; the raised stop fills at its price that day when the close is at or below it, or under `pessimistic` when the low reaches it, and otherwise the position is held. Harm: a breakeven rule reads as never losing a winner the daily bar cannot show was kept.
- `PBT backtest.raised_stop_dip_rules_agree` On generated markets both rules count an ambiguous day or neither does, and with none they make the same trades. Harm: the rule changes a run it has no day to act on, or one rule hides the days the other counts.
- `PBT backtest.lower_circuit_inferred` A session at one price all day, below the previous close and within one tick of an NSE band's lower price (2, 5, 10, 20%, rounded up to the tick) is pinned at its lower circuit; a session that moved, closed at or above the previous close or sat between bands is not; no previous close or tick is unknown. Harm: the backtest sells at a locked lower band, or carries a stop on a day that traded.
- `PBT backtest.lower_circuit_carry` The day after an entry the stock is pinned at a drawn band for one to three sessions: no sale is dated on a pinned session, each counts in `metrics.lower_circuit_days`, and the stop fills at the next session not pinned by `stop_fill` (min(open, stop) when its open or low reaches the stop). Harm: a gap-down crash costs the backtest less than it costs live.
- `PBT backtest.suspension_markdown` A held stock with no bars after a drawn session, not delisted, is carried at its last close for `suspension_sessions` (drawn 0..12) sessions without a bar, then marked 5% lower per further session (floored to the tick, never below it nor above the last close), in every equity point and in its end-of-period close, whose MAE reaches the mark. Harm: a suspended position is carried at a stale price for months.
- `PBT backtest.path_rules_refused` An ambiguous-day rule other than `optimistic` or `pessimistic` is refused `backtest_bad_ambiguous_rule`, and bar-less sessions that are not a whole number in 0..250 (a boolean included) `backtest_bad_suspension`, by `check_conventions`, and a run flagged for promotion with more than the default 5 `backtest_promotion_late_suspension`. Harm: a run under an unknown rule reads as the default, a negative count marks a trading stock down, or a promotion carries a suspended position at its stale close.
- `PBT backtest.stop_on_instrument_tick` Every opened entry's stop is entry x 0.95 rounded down to the instrument's NSE tick of the trade date (`data.ticks.tick_for_date` on the closes before it), with prices scaled into the 0.01 and the 0.10 slab (design). Harm: backtest stops sit on a grid the live trigger never uses, so backtest and live risk differ.
- `PBT backtest.pre_break_low` The entry day's low before the break is the lowest low of the 5-minute bars up to the first one that reaches the level, and the low after it the lowest of the later bars (or a day low they miss); without them it is max(day low, min(open, level x (1 - adr20/100))), with the day low after the break only when lower (design). Harm: a low-of-day stop reads a low made after the entry.
- `PBT backtest.entry_day_stop` Under a low-of-day stop, with and without generated 5-minute bars, every entry's stop is the live plan from the low before the break (the break at the entry price, level + tick under penetration), a gap over without 5-minute bars is skipped and counted as `gap_over_no_intraday`, an entry without them needs adr20 (generated markets blank it on some dates), and an entry-day low at or below the stop exits that day at -1 R (MAE -1), while a higher one bounds the MAE (design). Harm: an S2 entry's stop sits at a low made after the entry, and the entry day's loss is never seen.
- `PBT backtest.contraction_stop_too_wide` A contraction stop the scan left empty skips the trigger as `stop_too_wide`, never `stop_input_missing` (design). Harm: skip counts blame missing data for a stop the plan refused as too wide.
- `PBT backtest.adtr_filter_applied` With an `adtr_filter`, a trigger whose stop is wider than max_multiple x ADTR(20) as of the session before (or with no ADTR, blanked at some closes) is Rejected `adtr_filter` and buys nothing; one within it is never refused for that reason. Harm: the backtest takes wide-stop entries live refuses.
- `PBT backtest.upper_circuit_inferred` An entry price that is the day's high, above the previous close and within one tick of the upper price of an NSE band (2, 5, 10, 20%) above the previous close is locked; one below the high, at or below the previous close, or between bands is not; no previous close or tick is unknown. Harm: the backtest buys a stock locked at its upper band.
- `PBT backtest.upper_circuit_skip` In a run with sessions at an upper band (pinned all day, or opened at the band and faded), a trigger entering at the band is Rejected `max_chase` and buys nothing, and with a 30% chase no other trigger is. Harm: the backtest buys locked gap-ups live never gets.
- `PBT backtest.cooldown_applied` In a run whose policy has `cooldown_sessions` N in [0, 4] (`with_cooldown`), with tight stops so hard stops and re-triggers are common, every trigger Rejected `cooldown` follows a hard stop of its instrument within N sessions and buys nothing, no entry opens within N sessions after one, no signal is refused for a missing or invalid input (the stop history is passed when N > 0), and the run records N. Harm: the knob the cooldown sweep turns does nothing, or the backtest refuses entries live takes.
- `PBT backtest.exit_fill_proxy` A stop fills at min(open, stop) when the day reaches it and is never better than the stop; a day above it does not fill. Harm: gap-down losses are understated.
- `PBT backtest.exit_fill_proxy_close` A trade closed by a close-based rule sold at that day's official close, and `close_exits_at_official_close` is a recorded known gap. Harm: close exits are priced at a better point of the day than the rule could see.
- `PBT backtest.decimal_money` Quantities are whole and at least 1, money is Decimal, and the trades' net P&L sums exactly to the change in equity. Harm: float drift makes equity and the trades disagree.
- `PBT backtest.trades_match_metrics` The rows and sum of R in `trades.parquet` equal the metrics' `trades` and `sum_r`, and each trade's `score` (in the record and the file) is its watchlist row's. Harm: the reported evidence does not match the trades behind it, or the confidence buckets group trades by a score they did not have.
- `PBT backtest.r_and_expectancy` R = realised P&L / ((entry − initial stop) × qty), a whole exit at the initial stop without costs is −1 R, and expectancy is the mean R. Harm: R values are inconsistent and the lifecycle and veto scores read wrong numbers.
- `PBT backtest.metrics_bounds` Max drawdown and win rate are in [0, 1], and the metrics do not depend on the order of trades closing on the same day. Harm: impossible values reach the lifecycle gate.
- `PBT backtest.robustness_metrics` For generated trades and an equity curve, `exposure` (sessions inside some entry..exit span over all sessions; `held_sessions` their count and `first_session`/`last_session` the curve's ends), `longest_drawdown_sessions` (the longest run below the running peak of the curve from the initial equity), `mar` (times the drawdown depth gives `cagr`), `tail_trimmed_r` (the mean R without the best ceil(1, 5, 10 % of the trades)) and `cost_share` (fees over net P&L + fees) equal their definitions. Harm: the tuning report shows an idle, long-underwater, outlier-driven or cost-eaten strategy as robust.
- `PBT backtest.deterministic` Two runs of the same market, with rows, features, members and mappings shuffled, give byte-identical trade files and equal metrics JSON, including the known-gaps list. Harm: evidence cannot be reproduced, so a promotion cannot be audited.
- `PBT backtest.version_status_requires_run` A draft becomes `backtested` only with a completed run of that same version flagged for promotion (`metrics.for_promotion`) with `slot_order` `time`; a running, failed, foreign or unflagged run, or one in another slot order or recorded before, is refused and leaves the draft untouched. Harm: a version is promoted on a run that never finished, on another version's evidence, or on perfect fills.
- `CONF backtest.backtest_run_columns` `hat_backtest_run` has non-null pinned versions, period, snapshot, status, metrics and family key; trades go to `backtests/run_id=<id>/trades.parquet` with the `TRADE_SCHEMA` columns; the statuses are `running`, `completed`, `failed`. Harm: a run cannot be traced to its inputs or its trades file.
- `CONF backtest.known_gaps_listed` The committed golden runs report exactly `KNOWN_GAPS`. Harm: a gap the loop has is not reported, so the evidence reads as more faithful than it is.
- `CONF backtest.backtest_golden_replay` The committed fixture market (`tests/hat/fixtures/backtest_market.csv`) run through S1 and S2 reproduces `tests/hat/fixtures/backtest_golden.json` exactly. Harm: a silent engine change shifts every sleeve's evidence.
- `PBT costs.slippage_adverse` A buy fills at p × (1 + s/10000) rounded up to 4 places, a sell at p × (1 − s/10000) rounded down and never below 0.0001. Harm: rounding hands the backtest a better price than any fill.
- `PBT costs.slippage_adverse_net` A round trip's net P&L does not rise with slippage. Harm: more slippage makes a strategy look better.
- `PBT costs.slippage_adverse_bounds` A slippage outside 0 ≤ s < 10000 bps, or not a Decimal (a float, an integer, NaN, infinity), is refused. Harm: a negative slippage pays the backtest for trading.
- `PBT costs.charges_contract` On generated cost models: each charge is ≥ 0, applies only to its sides, is rate × value + per-order capped and rounded to the paisa; GST is on the taxable components only; the total is their sum plus GST and does not fall as the traded value grows. Harm: costs are missed or double-counted and thin edges look profitable.
- `PBT costs.charges_contract_run` In a run every fill's fees are the cost model's charges on its slipped price, the trades' net P&L is gross minus those fees exactly, and the metrics name the model's version and digest. Harm: a run charges a different price than it filled at, or its results cannot be tied to a rate schedule.
- `PBT costs.charges_contract_float_rates` A rate written as a float, a boolean or nothing is refused. Harm: binary rounding reaches money.
- `CONF backtest.pbt_money_strategies_decimal` The sizing, caps, exposure, allocator and costs properties use no float tolerance, and a float strategy appears only inside `st.one_of` as invalid input. Harm: rounding drift lets a size or a charge cross a limit and the check still passes.
- `PBT dsr.bounds_and_monotonicity` DSR is in [0, 1], never NaN, and does not rise with the trial count. Harm: more trials make a strategy look more significant, the opposite of the correction.
- `PBT dsr.bounds_and_monotonicity_in_sr` DSR does not fall as SR rises wherever the formula's slope in SR is non-negative at both ends. Harm: a better Sharpe ratio scores worse in the region where the formula says it must not.
- `PBT dsr.bounds_and_monotonicity_first_trial` With N = 1 or V = 0, SR0 = 0 and DSR is the probabilistic Sharpe ratio against zero (to 1e-9, against `math.erf`). Harm: the first run of a family is mis-scored.
- `PBT dsr.bounds_and_monotonicity_undefined` Fewer than 2 returns, no variance, or a value that is not finite scores 0 with `dsr_undefined`, never NaN. Harm: a NaN compares false and a gate written as "not below 0.95" lets the sleeve through.
- `PBT dsr.bounds_and_monotonicity_bad_denominator` A non-positive radicand, or one within float noise of 0 (≤ 1e-12), scores 0 with `dsr_undefined`. Harm: float noise around 0 divides into a DSR of 1 and passes the gate.
- `CONF backtest.dsr_published_example` Bailey & López de Prado's example gives SR0 0.1132 and DSR 0.9004 at the printed 4 places, and the code matches the formula computed independently (`math.erf`, bisection) to 1e-6. Harm: a formula slip goes unnoticed because every other check is relative.
- `PBT dsr.trial_counting` A family's trial count is the number of runs recorded for it, failed ones included; it never falls and other families never change it. Harm: discarded variants are forgotten and the DSR under-corrects for luck.
- `PBT dsr.family_key` Two runs share a family key iff they share strategy and universe, whatever their version or period. Harm: a new version escapes the trial penalty.
- `PBT dsr.evidence_current_trials` The lifecycle's evidence is the newest completed run of the live version with the DSR deflated by the family's trials and Sharpe variance now, later failed runs and other versions counted; no run is no evidence. Harm: a sleeve passes the gate on a stale trial count.
- `PBT dsr.evidence_cooldown_in_force` The sleeve's evidence is its newest completed run made under the cooldown in force when it is read (now for the lifecycle, the request's instant for the risk-increase gate): a `--cooldown-sessions` sweep run, or one made before the cooldown changed, is skipped, and a run or a policy without the field counts as 0; with none there is no evidence. Harm: a gate is passed on a run of a rule that is not live.
- `PBT dsr.evidence_promoted_only` The sleeve's evidence, for the lifecycle and the risk-increase gate, is its newest completed run flagged for promotion (`metrics.for_promotion`) whose `metrics.slot_order` is `time` and whose metrics carry the path rules (`lower_circuit`) and `regime_mult` `policy`; a sensitivity run, one under regime rule `1` or one recorded before the flag, the slot order, the path rules or the allocation sizing is skipped, and with none there is no evidence. Harm: a sleeve is promoted, or its risk raised, on perfect or optimistic fills.
- `PBT dsr.gate_evidence` The risk-increase evidence is `build_gate_evidence` over the user's trades closed since the policy version in force at the request took effect (every closed trade when none is; a pending version and trades of an earlier version are not read;) in closing order against the discounted trade-weighted expectancy of each sleeve's newest completed run of its live version; older runs, other versions and other users are not read, and no run is no evidence. Harm: a risk increase is checked against a stale or foreign benchmark.
- `PBT dsr.family_floors` A family's DSR is the formula at N' = max(N, `MIN_TRIALS`) and V' = max(V, `VARIANCE_FLOOR`), never above the unfloored DSR, and a first run (N = 1) or identical variants (V = 0) are deflated against a positive SR0. Harm: a new strategy's first lucky run is scored undeflated and promoted.
- `PBT dsr.pbo_ranked_variants` Variants that differ by a constant shift of the same returns (every block varying) have PBO 0, identical variants PBO 1, and PBO is in [0, 1] over C(S, S/2) splits. Harm: a genuinely best variant reads as overfit, or indistinguishable variants read as skill.
- `PBT dsr.pbo_two_variants` With two variants PBO is the share of splits whose in-sample winner (first on a tie) has an out-of-sample Sharpe ratio no higher than the other's, counted split by split with `statistics`. Harm: an in-sample winner that loses out of sample is counted as robust.
- `PBT dsr.pbo_refused` PBO is None for fewer than 2 variants, an odd block count, unequal series, fewer than 2 sessions per block or a non-finite value. Harm: a one-variant family or a short history reports a probability.
- `PBT dsr.dsr_needs_recorded_run` `run_dsr` and `complete_run` refuse a run that is not a `hat_backtest_run` row (`backtest_run_unrecorded`); a recorded run is scored at its family's trial count and Sharpe variance, its own included, through the floors, and stored so. Harm: a variant tried outside the recorded runs is scored as evidence without counting as a trial.
- `PBT dsr.returns_persisted` A completed run's `returns.parquet` has one row per session close with its equity and its return against the close before (the first against `metrics.initial_equity`), and `read_returns` gives them by date. Harm: PBO and later re-deflation cannot be computed because the returns were never kept.
- `PBT dsr.evidence_latest_by_completion` The sleeve's evidence is the run that completed last, whatever order the runs started in. Harm: a long run started first and finished last is shadowed by an older result.
- `PBT dsr.family_pbo_recorded` Each completed run records its family's PBO (`metrics.pbo`) over the completed runs with a returns file, itself included, on their shared sessions (`dsr.pbo` of their series in run order, a re-run of an equal series counted once), and `pbo_too_few_variants` with one distinct variant. Harm: the overfitting check reads one variant or another family's runs, or scores a deterministic re-run as an overfit second variant.
- `PBT dsr.family_pbo_skips_unreadable` A sibling whose returns file cannot be read as one is left out of the PBO, and the run being completed still completes. Harm: one damaged file fails every later backtest of the family.
- `CONF backtest.dsr_scored_only_by_runs` Under `app/hat` only `backtest/runs.py` (besides `dsr.py`) imports `deflated_sharpe`, `family_deflated_sharpe` or `pbo`, or binds the module `app.hat.backtest.dsr` whole, and `runs.py` imports the last two. Harm: a module scores a result that never became a trial.
- `PBT confidence.label_rule` No bucket, fewer than 30 trades or a mean at or below 0 R is low; otherwise a positive 90% lower bound is high and the rest medium (D132). Harm: thin or losing evidence reads as high and auto places it without the owner.
- `PBT confidence.label_monotone` More trades, a higher mean or a higher lower bound never gives a lower label (D132). Harm: more winning backtest trades take a setup off auto.
- `PBT confidence.band_of` The three setup-score bands split [0.60, 1.00] with no gap or overlap; any other score, or a value that is not a number, is in no band (D132). Harm: a signal is compared with trades of another band, or an unscored one gets a bucket.
- `PBT confidence.bucket_stats` A bucket's count, mean R, 90% interval (`Z_90` standard errors of the mean, none below two trades) and win rate are those of its trades, in any order (D132). Harm: the label reads numbers the trades do not have.
- `PBT confidence.buckets_match_trades` The `hat_backtest_bucket` rows `complete_run` stores equal the buckets recomputed from the run's `trades.parquet`; an unscored trade, or a score outside the bands, is in none (D132). Harm: a label reads a bucket the run's trades do not support.
- `PBT confidence.signal_label_from_evidence` `open_entry` stores the label of the signal's bucket (playbook, the band of its watchlist score on its own day) in the run `backtest_evidence` reads, that bucket as `confidence_basis` and the label on `signal.triggered`; no run, a playbook the run's known gaps name (EP until), an unreadable gap list, or no score in a band gives low, and a run completed after the signal triggered, or at no readable time, is never its evidence. Harm: a signal without evidence is labelled high and auto places it.
- `PBT confidence.backtest_label_is_fixed` A backtest's own signals are labelled low with the basis `backtest`. Harm: a run is read as its own evidence.

## Run

```bash
cd backend
# every pytest, Hypothesis or break run goes under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_backtest.py tests/hat/properties/test_costs.py \
  tests/hat/properties/test_dsr.py tests/hat/properties/test_confidence.py -q --no-cov
$CAP python -m pytest tests/hat/conformance/test_conf_backtest.py -q --no-cov
# an intended engine change regenerates the golden file in the same commit
PYTHONPATH=.:tests/hat python tests/hat/hat_backtest_support.py
```

No check touches the network, Fyers or the HAT database: a run's ledger is an in-memory SQLite database, and the run, trial and evidence properties use SQLite in memory too. A run takes about a second, so the run-level properties use 12 examples (`RUNS`); the `test_backtest.py` file takes about a minute.

## Bug or model error

- A failing run-level property prints the generated paths (open, high, low, close per session per instrument). Rebuild the market with `hat_backtest_support.market(paths, sessions(34))` and run `run_backtest` on it; the result holds the watchlists, signals, fills, trades and equity to compare. If the real loop breaks the rule, that is a finding: fix the loop, never the property.
- `backtest.scan_parity` failing means the loop fed the scan different inputs than the live nightly scan gets (the bar window, the features date, the index closes, the members). The independent scan is `hat_backtest_support.scan_watch`; keep it independent of the loop.
- `backtest.nightly_scan_parity` failing means the nightly job (`daily.market.scan_inputs`, `Nightly.features`, `Nightly.scan`) and the loop (`backtest.market.load_market`, `_Run._publish`) read different inputs: the bar window, the adjusted view's actions, the features partition, the index closes or the members. The night world is `hat_backtest_support.night_world` and `store_session` (raw prices over a split's factor before its ex-date). `backtest.nightly_no_lookahead` failing means a nightly read is not bounded by the scan date. `backtest.features_digest_pinned` failing means the loader's check and the features job's recorded digest disagree (`data.features.inputs_digest`, `STATE_FILE`).
- `CONF backtest.backtest_shares_engine_imports` names the private module imported or the function redefined: call the live function instead (add a parameter to it, as `fills` was added to `open_entry`, rather than copying it).
- `CONF backtest.backtest_golden_replay` failing after an intended change: check the diff of the regenerated golden file line by line, explain it in the PR, and commit it with the change. Unexplained diffs are bugs.
- `dsr.*` failures print the stats (SR, T, skew, kurtosis), N and V; recompute with the deflated-Sharpe formula in `backtest/dsr.py`. The SR-monotonicity property holds only where the slope is non-negative; a counterexample outside that region is the formula, not the code.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new exit or entry behaviour belongs in the live engine (`trades.py`, `exits.py`, `stops.py`); the loop only feeds it prices. Change `fills.py` only for a new price proxy, and say so in this skill.
- A rate change is a new cost-model file `cost_models/<version>.yaml` with a new version; never edit a published one (runs record its digest).
- A new known gap goes into `KNOWN_GAPS` and the module docstring of `engine/backtest.py` together, and the golden is regenerated.
- The golden fixture market was generated once (a random walk with an accumulation, flag and breakout per instrument; parameters ADV=0.025, ADV_DAYS=22, FLAG=0.006, FLAG_DAYS=6, SIDE_DAYS=20, recorded in the description) and is frozen; regenerate only the golden JSON, not the market, for an engine change.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-backtest` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/engine/backtest.py`: `run_backtest`, `industry_on`, `compute_metrics`, `trades_parquet`, `lower_circuit_locked`, `check_conventions`, `main`
- `backend/app/hat/backtest/fills.py`: `entry_fill`, `stop_fill`, `raised_stop_dip`, `suspended_mark`
- `backend/app/hat/backtest/slots.py`: `is_slot_order`, `trigger_rank`, `order_slots`
- `backend/app/hat/backtest/costs.py`: `parse_cost_model`, `load_cost_model`, `charges`, `check_slippage`, `slipped`, `BacktestFills`
- `backend/app/hat/backtest/cost_models/cnc-delivery-1.yaml`
- `backend/app/hat/backtest/dsr.py`: `return_stats`, `trial_variance`, `expected_max_sharpe`, `deflated_sharpe`, `family_deflated_sharpe`, `pbo`
- `backend/app/hat/backtest/runs.py`: `family_key`, `trial_count`, `run_dsr`, `family_pbo`, `read_returns`, `start_run`, `complete_run`, `fail_run`, `record_backtest`, `backtest_bot`, `backtest_evidence`, `gate_evidence`, `bucket_rows`, `signal_confidence`
- `backend/app/hat/engine/confidence.py`: `band_of`, `bucket_stats`, `buckets`, `label_of`, `confidence_of`
- `backend/app/hat/backtest/market.py`: `load_market`, `_industries`
- `backend/app/hat/engine/strategy/versions.py`: `publish_version`

Checks:
- `backend/tests/hat/hat_backtest_support.py`
- `backend/tests/hat/properties/test_backtest.py`
- `backend/tests/hat/properties/test_costs.py`
- `backend/tests/hat/properties/test_dsr.py`
- `backend/tests/hat/properties/test_confidence.py`
- `backend/tests/hat/conformance/test_conf_backtest.py`
- `backend/tests/hat/fixtures/backtest_market.csv`
- `backend/tests/hat/fixtures/backtest_golden.json`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `2f23b2f377fa3db89906eb01cf61eb230b822b73`
Covered digest: `f2d30ac8c63e4d331940b5749daa0b4c5bf482f1e5270b1f3c27f208cac00a01`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or fixture, then the break was removed and the check returned to passing. The breaks were applied and run by one script, one at a time.

### CONF backtest.backtest_shares_engine_imports

Break: in `backend/app/hat/engine/backtest.py`: the day loop imports `scan_bot` directly.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:69: in test_backtest_shares_engine_imports
    assert not [m for m in imported if any(m == p or m.startswith(p + ".") for p in PRIVATE)]
E   AssertionError: assert not ['app.hat.engine.scan.scan_bot']
FAILED tests/hat/conformance/test_conf_backtest.py::test_backtest_shares_engine_imports - AssertionError: assert not ['app.hat.engine.scan.scan_bot']
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
```

### PBT backtest.scan_parity

Break: in `backend/app/hat/engine/backtest.py`: the nightly scan reads the bars up to the session before the scan date.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:96: in test_scan_parity
    @RUNS
tests/hat/properties/test_backtest.py:103: in test_scan_parity
    _assert_parity(result, data, day)
tests/hat/properties/test_backtest.py:87: in _assert_parity
    assert [_watch_key(w) for w in ran] == [_watch_key(w) for w in live]
```

Re-run after the CodeAnt review made the property check every simulated date of a run, not one drawn date: the same break still fails.

### PBT backtest.nightly_scan_parity

Break: in `backend/app/hat/daily/market.py`: `scan_inputs` reads the bars without the stored corporate actions (`store.connect()`), so the night after a split scans unadjusted prices.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:197: in test_nightly_scan_parity
    assert ran == published
E   AssertionError: assert [('default',  [cut]l('76.0000'))] == [('default',  [cut]l('76.0000'))]
E     At index 0 diff: ('default', '00000000-0000-0000-0000-000000001000', Decimal('0.7669542296'), Decimal('119.0600'), Decimal('113.1000')) != ('default', '00000000-0000-0000-0000-000000001000', Dec [cut]
E     Full diff:
E       [
E           (
E               'default',
```

### PBT backtest.no_lookahead

Break: in `backend/app/hat/engine/backtest.py`: the scan's regime reads every index close of the period, later ones included.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:102: in test_no_lookahead
    s.paths(SESSIONS),
               ^^^
tests/hat/properties/test_backtest.py:117: in test_no_lookahead
    assert _upto(first, day) == _upto(second, day)
E   AssertionError: assert {'watch': [Wa...des': [], ...} == {'watch': [],...des': [], ...}
```

### PBT backtest.nightly_no_lookahead

Break: in `backend/app/hat/daily/market.py`: `scan_inputs` reads the regime index closes through `date.max` instead of the scan date.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:268: in test_nightly_no_lookahead
    assert later.watchlist(nxt) == cut.watchlist(nxt)
E   AssertionError: assert [] == [('default',  [cut].0000'),  [cut])]
E     Right contains one more item: ('default', '00000000-0000-0000-0000-000000001000', Decimal('1.0000000000'), Decimal('80.0000'), Decimal('76.0000'), Decimal('0.0000'),  [cut])
E     Full diff:
E     + []
E     - [
E     -     (
```

### PBT backtest.survivorship_free

Break: in `backend/app/hat/engine/backtest.py`: `_delistings` returns at once: a delisted position is not closed at its last bar.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:124: in test_survivorship_free
    @RUNS
tests/hat/properties/test_backtest.py:138: in test_survivorship_free
    assert all(f.day <= last for f in result.fills if f.instrument_id == victim)
E   assert False
E    +  where False = all(<generator object test_survivorship_free.<locals>.<genexpr> at 0x7f0055ec2420>)
```

### PBT backtest.market_point_in_time

Break (CodeAnt review): in `backend/app/hat/backtest/market.py`: an instrument delisted after the period's end still gets its last loaded bar as a last traded session.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:449: in test_market_point_in_time
tests/hat/properties/test_backtest.py:526: in test_market_point_in_time
E   AssertionError: assert {'00000000-00...e(2025, 1, 8)} == {}
```

Break (CodeAnt review): in `backend/app/hat/backtest/market.py`: only the newest classification is loaded, in force from the start.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:449: in test_market_point_in_time
tests/hat/properties/test_backtest.py:528: in test_market_point_in_time
E   AssertionError: assert [(datetime.da... 1), 'ind-a')] == [(datetime.da... 6), 'ind-a')]
```

Break: in `backend/app/hat/engine/backtest.py`: `industry_on` takes the newest classification whatever the day.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:449: in test_market_point_in_time
tests/hat/properties/test_backtest.py:531: in test_market_point_in_time
E   AssertionError: assert 'ind-b' == 'ind-a'
```

### PBT backtest.features_digest_pinned

Break: in `backend/app/hat/backtest/market.py`: `_current_features` skips the digest comparison (`if False or not all(`).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1660: in test_features_digest_pinned
    with pytest.raises(BacktestError) as refused:
E   Failed: DID NOT RAISE BacktestError
E   Failing test case: test_features_digest_pinned(
E       paths={'00000000-0000-0000-0000-000000001000': [(80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
```

### PBT backtest.version_pinned

Break: in `backend/app/hat/engine/backtest.py`: the metrics name a fresh version id, not the run's.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:148: in test_version_pinned
    @RUNS
tests/hat/properties/test_backtest.py:155: in test_version_pinned
    assert result.metrics["strategy_version_id"] == str(s.VERSION)
E   AssertionError: assert 'f912e2c0-c9f...-82ca501293ca' == '00000000-000...-0000000000b1'
E
```

### PBT backtest.sized_from_allocation

Break: in `backend/app/hat/engine/backtest.py`: `_Run._allocate` allocates over the initial equity instead of the account's mark at the night's close.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1819: in test_sized_from_allocation
    assert (allocation.regime_mult, allocation.capital) == (mult, capital), day
E   AssertionError: datetime.date(2025, 2, 12)
E   assert (Decimal('1') [cut]'10000.0000')) == (Decimal('1') [cut]('9999.9736'))
E     At index 1 diff: Decimal('10000.0000') != Decimal('9999.9736')
E     Full diff:
E       (
E           Decimal('1'),
```

### PBT backtest.invested_share

Break: in `backend/app/hat/engine/backtest.py`: `_mark` records the cash's share of the mark instead of the positions'.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1852: in test_invested_share
    assert (share > 0) is held, day
E   AssertionError: datetime.date(2025, 2, 4)
E   assert (Decimal('1') > 0) is False
E   Failing test case: test_invested_share(
E       paths={'00000000-0000-0000-0000-000000001000': [(80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
```

### PBT backtest.sizing_conventions_refused

Break: in `backend/app/hat/engine/backtest.py`: `check_conventions` accepts a sleeve weight of 0 (`0 <= sleeve_weight <= 1`).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1903: in test_sizing_conventions_refused
    with pytest.raises(BacktestError) as refused:
E   Failed: DID NOT RAISE BacktestError
E   Failing explicit example: test_sizing_conventions_refused(
E       weight=Decimal('0'),
E       rule='1',
E       has_base=True,
E   )
```

### PBT backtest.regime_inputs_loaded

Break: in `backend/app/hat/backtest/market.py`: `load_market` checks only the NIFTY 500 closes, so a store without India VIX loads.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1955: in test_regime_inputs_loaded
    with pytest.raises(BacktestError) as refused:
E   Failed: DID NOT RAISE BacktestError
E   Failing test case: test_regime_inputs_loaded(
E       # The test always failed when commented parts were varied together.
E       paths={'00000000-0000-0000-0000-000000001000': [(80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
```

### PBT backtest.entry_fill_proxy

Break: in `backend/app/hat/backtest/fills.py`: a gap over the level fills at the level, not the open.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:163: in test_entry_fill_proxy
    @example(D(10), D(12), D(10))
                   ^^^
tests/hat/properties/test_backtest.py:170: in test_entry_fill_proxy
    assert entry_fill(open_, high, level) == expected
E   AssertionError: assert Decimal('1') == Decimal('1.01')
```

### PBT backtest.entry_fill_proxy_chase

Break: in `backend/app/hat/engine/backtest.py`: the risk decision gets the fill as the trigger level, so max chase never bites.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:209: in test_entry_fill_proxy_chase
    assert signal.trigger_price == level  # the signal records the level it broke
E   AssertionError: assert Decimal('175.3800') == Decimal('172.9800')
```

### PBT backtest.fill_conventions_recorded

Break: in `backend/app/hat/engine/backtest.py`: a run flagged for promotion runs at a slippage of 0.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:811: in test_fill_conventions_recorded
    with pytest.raises(BacktestError, match="backtest_promotion_(without_slippage|on_touch)"):
E   Failed: DID NOT RAISE BacktestError
E   Failing explicit example: test_fill_conventions_recorded(
E       paths={'00000000-0000-0000-0000-000000001000': [(100.0,
E          101.0,
E          99.0,
E          100.0),
E         (100.0, 101.0, 99.0, 100.0),
E         (100.0, 101.0, 99.0, 100.0),
```

### PBT backtest.fill_conventions_recorded_digest

Break: in `backend/app/hat/engine/backtest.py`: `config_digest` leaves the slippage out.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:844: in test_fill_conventions_recorded_digest
    assert (config_digest(spec(*a)) == config_digest(spec(*b))) is same  # type: ignore[arg-type]
E   AssertionError: assert ('340f4ea148ee [cut]4efad28dd8de1' == '340f4ea148ee [cut]4efad28dd8de1'
E       fad28dd8de1) is False
E   Failing test case: test_fill_conventions_recorded_digest(
E       a=('penetration_1_tick', Decimal('0'), 'zero'),
E       b=('penetration_1_tick', Decimal('5'), 'zero'),
E   )
```

### PBT backtest.slot_order_rank

Break: in `backend/app/hat/backtest/slots.py`: a gap over is ranked by its (negative) distance, so gap overs no longer tie and leave the score order.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:933: in test_slot_order_rank
    assert _before(x, y, fill) < 0 or (_before(x, y, fill) == 0 and i < j)
E   AssertionError: assert (0 < 0 or (0 == 0 and 1 < 0))
E    +  where 0 = _before((Decimal('101'), Decimal('100')), (Decimal('100'), Decimal('100')), 'touch')
E    +  and   0 = _before((Decimal('101'), Decimal('100')), (Decimal('100'), Decimal('100')), 'touch')
E   Failing explicit example: test_slot_order_rank(
E       cands=[(Decimal('100'), Decimal('100')), (Decimal('101'), Decimal('100'))],
E       rule='time',
```

### PBT backtest.slot_order_refused

Break: in `backend/app/hat/backtest/slots.py`: `random:K` accepts leading zeros, a second spelling of one seed.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:968: in test_slot_order_refused
    with pytest.raises(BacktestError, match="backtest_bad_slot_order"):
E   Failed: DID NOT RAISE BacktestError
E   Failing explicit example: test_slot_order_refused(
E       rule='random:07',
E   )
```

### PBT backtest.slot_order_applied

Break: in `backend/app/hat/engine/backtest.py`: the loop takes the watchlist in score order whatever the run's slot order.

```
tests/hat/properties/test_backtest.py F
  | ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
    |   File "backend/tests/hat/properties/test_backtest.py", line 1011, in test_slot_order_applied
    |     assert order < 0 or (order == 0 and score_key(x) < score_key(y))
    | AssertionError: assert (1 < 0 or (1 == 0))
    |     rule='time',
    |     fill='penetration_1_tick',
    |   File "backend/tests/hat/properties/test_backtest.py", line 1018, in test_slot_order_applied
    |     assert rows == [w for w in ranked if w in rows]
    | AssertionError: assert [WatchRow(tra [cut]('192.4100'))] == [WatchRow(tra [cut]l('76.0000'))]
    |     rule='random:0',
    |     fill='penetration_1_tick',
```

### PBT backtest.raised_stop_dip

Break: in `backend/app/hat/engine/backtest.py`: the close phase counts an ambiguous day but never fills the raised stop, whatever the rule.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1100: in test_raised_stop_dip
    assert [f.price for f in sold] == ([_ENTRY] if exits_at_stop else [])
E   AssertionError: assert [] == [Decimal('101.01')]
E     Right contains one more item: Decimal('101.01')
E     Full diff:
E     + []
E     - [
E     -     Decimal('101.01'),
```

### PBT backtest.raised_stop_dip_rules_agree

Break: in `backend/app/hat/engine/backtest.py`: an ambiguous day is counted only under `pessimistic`.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1115: in test_raised_stop_dip_rules_agree
    assert (counts[AMBIGUOUS_OPTIMISTIC] == 0) is (counts[AMBIGUOUS_PESSIMISTIC] == 0)
E   assert (0 == 0) is (1 == 0)
E   Failing test case: test_raised_stop_dip_rules_agree(
E       paths={'00000000-0000-0000-0000-000000001000': [(80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
E         (80.0, 80.0, 80.0, 80.0),
```

### PBT backtest.lower_circuit_inferred

Break: in `backend/app/hat/engine/backtest.py`: `lower_circuit_locked` needs the price strictly within a tick of the band, so a price at it is not pinned.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1142: in test_lower_circuit_inferred
    assert lower_circuit_locked(at(price), prev_close, tick) is True
E   AssertionError: assert False is True
E    +  where False = lower_circuit_locked((Decimal('94.95'), Decimal('94.95'), Decimal('94.95'), Decimal('94.95')), Decimal('100'), Decimal('0.05'))
E    +    where (Decimal('94.95'), Decimal('94.95'), Decimal('94.95'), Decimal('94.95')) = <function test_lower_circuit_inferred.<locals>.at>(Decimal('94.95'))
E   Failing explicit example: test_lower_circuit_inferred(
E       prev_close=Decimal('100'),
E       band=Decimal('5'),
```

### PBT backtest.lower_circuit_carry

Break: in `backend/app/hat/engine/backtest.py`: the morning counts a pinned session but still runs the position's way down, so the stop sells at the locked band.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1187: in test_lower_circuit_carry
    assert not [f for f in _sells(result) if f.day in locked_days]
E   AssertionError: assert not [FillRow(day=datetime.date(2025, 2, 6), instrument_id='00000000-0000-0000-0000-000000001000', purpose='exit_full', side='sell', qty=247, price=Decimal('92.9600'), fees=D [cut]
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_lower_circuit_carry(
E       band=Decimal('5'),
E       locked=2,
E       reopen=9650,
```

### PBT backtest.suspension_markdown

Break: in `backend/app/hat/engine/backtest.py`: `_mark_price` counts the sessions without a bar one short, so the markdown starts a session late.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1223: in test_suspension_markdown
    assert value == cash + buy.qty * mark(n)
E   AssertionError: assert Decimal('1000985.5300') == (Decimal('975050.5300') + (247 * Decimal('99.75')))
E    +  where 247 = FillRow(day=datetime.date(2025, 2, 4), instrument_id='00000000-0000-0000-0000-000000001000', purpose='entry', side='buy', qty=247, price=Decimal('101.0100'), fees=Decimal('0.0000') [cut]
E    +  and   Decimal('99.75') = <function test_suspension_markdown.<locals>.mark>(32)
E   Failing explicit example: test_suspension_markdown(
E       traded=10,
E       after=0,
```

### PBT backtest.path_rules_refused

Break: in `backend/app/hat/engine/backtest.py`: `check_conventions` takes a boolean as a bar-less session count.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:1263: in test_path_rules_refused
    with pytest.raises(BacktestError, match=code):
E   Failed: DID NOT RAISE BacktestError
E   Failing explicit example: test_path_rules_refused(
E       rule='pessimistic',
E       after=True,
E   )
```

### PBT backtest.stop_on_instrument_tick

Break: in `backend/app/hat/engine/backtest.py`: the stop planned on the old fixed 0.05 tick instead of the instrument's NSE slab tick.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:241: in test_stop_on_instrument_tick
    assert signal.stop_price == floor_to_tick(entry * D("0.95"), tick)
E   AssertionError: assert Decimal('76.0500') == Decimal('76.07')
```

### PBT backtest.pre_break_low

Break: in `backend/app/hat/engine/backtest.py`: with 5-minute bars, the low before the break taken over the whole day's bars.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:279: in test_pre_break_low
    assert before == min(seen)
E   AssertionError: assert Decimal('8') == Decimal('9')
```

### PBT backtest.entry_day_stop

Break: in `backend/app/hat/engine/backtest.py`: the low-of-day stop planned from the whole day's low again.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:368: in test_entry_day_stop
    assert signal.stop_price == low_of_day(entry, [before], 1, _tick(data, iid, day)).stop
E   AssertionError: assert Decimal('79.9100') == Decimal('79.99')
```

### PBT backtest.contraction_stop_too_wide

Break: in `backend/app/hat/engine/backtest.py`: an empty contraction plan skipped as `stop_input_missing` again.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:399: in test_contraction_stop_too_wide
    assert result.skips.get(SKIP_BAD_INPUT, 0) == 0
E   AssertionError: assert 13 == 0
```

### PBT backtest.adtr_filter_applied

Break: in `backend/app/hat/engine/backtest.py`: the failing filter not passed to `open_entry` (no Rejected signal, the entry opens).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:437: in test_adtr_filter_applied
    assert (signal.reason == "adtr_filter") == wide
E   AssertionError: assert (None == 'adtr_filter') == True
```

### PBT backtest.upper_circuit_inferred

Break: in `backend/app/hat/engine/backtest.py`: the day's-high test dropped, so an entry below the high reads as locked.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:472: in test_upper_circuit_inferred
    assert upper_circuit_locked(price, price + tick, prev_close, tick) is False
E   AssertionError: assert True is False
```

### PBT backtest.upper_circuit_skip

Break: in `backend/app/hat/engine/backtest.py`: the circuit state always False at the trigger.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:524: in test_upper_circuit_skip
    assert (signal.reason == "max_chase") == locked
E   AssertionError: assert (None == 'max_chase') == True
```

### PBT backtest.cooldown_applied

Break: in `backend/app/hat/engine/trades.py`: `open_entry` passes an empty hard-stop history, so the policy's cooldown never bites.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:480: in test_cooldown_applied
    assert not cooling(iid, day, same_day=False)
E   AssertionError: assert not True
```

### PBT backtest.exit_fill_proxy

Break: in `backend/app/hat/backtest/fills.py`: a gap through the stop fills at the stop, not the open.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:193: in test_exit_fill_proxy
    @example(D(10), D(9), D(9))
                   ^^^
/data/workspace/algotrading/venv/lib/python3.14/site-packages/hypothesis/core.py:1695: in _raise_to_user
    raise the_error_hypothesis_found
tests/hat/properties/test_backtest.py:201: in test_exit_fill_proxy
```

### PBT backtest.exit_fill_proxy_close

Break: in `backend/app/hat/engine/backtest.py`: the close rules see and fill at the day's low, not the official close.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:208: in test_exit_fill_proxy_close
    @RUNS
tests/hat/properties/test_backtest.py:226: in test_exit_fill_proxy_close
    assert any(f.price == close for f in sells)
E   assert False
E    +  where False = any(<generator object test_exit_fill_proxy_close.<locals>.<genexpr> at 0x7f972f222880>)
```

### PBT backtest.decimal_money

Break: in `backend/app/hat/engine/backtest.py`: a trade row's net P&L adds its fees back.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:233: in test_decimal_money
    @RUNS
tests/hat/properties/test_backtest.py:245: in test_decimal_money
    assert result.equity[-1][1] - s.spec(s.config(), DAYS).initial_equity == net
E   AssertionError: assert (Decimal('999889.0800') - Decimal('1000000')) == Decimal('0.0000')
E    +  where Decimal('1000000') = RunSpec(config=StrategyConfig(bot='stage2-contraction-breakout', market='NSE', execution_mode='shadow', universe=[Inst...5b31654ccc8aa239811794cb2ed08960e3a8cb3457ea46e0d489a'), slippage_bps=Decimal('0')), initial_equity=Decimal('1000000')).initial_equity
```

### PBT backtest.trades_match_metrics

Break: in `backend/app/hat/engine/backtest.py`: the metrics count only winning trades.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:252: in test_trades_match_metrics
    @RUNS
tests/hat/properties/test_backtest.py:258: in test_trades_match_metrics
    assert table.num_rows == result.metrics["trades"]
E   assert 2 == 0
E    +  where 2 = pyarrow.Table\nentry_date: date32[day]\nexit_date: date32[day]\nplaybook: string\ninstrument_id: string\nqty: int64\nentry_p...6.0000]]\nexit_reason: [["time_stop","end_of_period"]]\nrealized_pnl: [[-55.4600,-55.4600]]\nfees: [[55.4600,55.4600]]\n....num_rows
```

### PBT backtest.r_and_expectancy

Break: in `backend/app/hat/engine/backtest.py`: expectancy is the winners' R over the trade count.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:266: in test_r_and_expectancy
    @RUNS
tests/hat/properties/test_backtest.py:279: in test_r_and_expectancy
    assert result.metrics["expectancy_r"] == str(mean.quantize(D("0.000001")))
E   AssertionError: assert '0.000000' == '-0.010000'
E
```

### PBT backtest.metrics_bounds

Break: in `backend/app/hat/engine/backtest.py`: max drawdown reported with its sign (negative).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:312: in test_metrics_bounds
    st.lists(st.tuples(st.integers(0, 9), R_VALUES), max_size=30),
               ^^^
tests/hat/properties/test_backtest.py:325: in test_metrics_bounds
    assert 0 <= metrics["max_drawdown"] <= 1
E   assert 0 <= -0.999999
```

### PBT backtest.robustness_metrics

Break: in `backend/app/hat/engine/backtest.py`: `exposure` sees each trade only on its exit date.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:820: in test_robustness_metrics
    assert metrics["exposure"] == len(inside) / len(days)
E   assert 0.2 == (3 / 5)
E    +  where 3 = len([datetime.date(2025, 1, 6), datetime.date(2025, 1, 7), datetime.date(2025, 1, 8)])
E    +  and   5 = len([datetime.date(2025, 1, 6), datetime.date(2025, 1, 7), datetime.date(2025, 1, 8), datetime.date(2025, 1, 9), datetime.date(2025, 1, 10)])
E   Failing explicit example: test_robustness_metrics(
E       rows=[(0, 2, Decimal('-1'), 5), (5, 6, Decimal('4'), 0)],
E       curve=[Decimal('100'),
```

### PBT backtest.deterministic

Break: in `backend/app/hat/engine/backtest.py`: the trade rows ordered by a freshly minted id instead of (exit date, entry date, instrument, playbook).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:337: in test_deterministic
    @RUNS
tests/hat/properties/test_backtest.py:357: in test_deterministic
    assert trades_parquet(first.trades) == trades_parquet(second.trades)
E   AssertionError: assert b'PAR1\x15\x0...e\x00\x00PAR1' == b'PAR1\x15\x0...e\x00\x00PAR1'
E
```

### PBT backtest.version_status_requires_run

Break: in `backend/app/hat/engine/strategy/versions.py`: the completed-run check removed from `publish_version`.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:369: in test_version_status_requires_run
    @settings(max_examples=25, deadline=None)
                   ^^^
tests/hat/properties/test_backtest.py:400: in test_version_status_requires_run
    with pytest.raises(VersionError):
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

### CONF backtest.backtest_run_columns

Break: in `backend/app/hat/backtest/runs.py`: the trades path without the `run_id=` partition name.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:110: in test_backtest_run_columns
    assert runs.trades_path(Path("/root"), run_id) == Path(
E   AssertionError: assert PosixPath('/root/backtests/00000000-0000-7000-8000-0000000000b1/trades.parquet') == PosixPath('/root/backtests/run_id=00000000-0000-7000-8000-0000000000b1/trades.parquet')
E    +  where PosixPath('/root/backtests/00000000-0000-7000-8000-0000000000b1/trades.parquet') = <function trades_path at 0x7f2e65fee770>(PosixPath('/root'), UUID('00000000-0000-7000-8000-0000000000b1'))
E    +    where <function trades_path at 0x7f2e65fee770> = runs.trades_path
E    +    and   PosixPath('/root') = Path('/root')
```

### CONF backtest.known_gaps_listed

Break: in `backend/app/hat/engine/backtest.py`: `entry_day_low_estimated` dropped from `KNOWN_GAPS`.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:219: in test_known_gaps_listed
E   assert False
E    +  where False = all(<generator object test_known_gaps_listed.<locals>.<genexpr> at 0x7f14ccb233e0>)
```

### CONF backtest.backtest_golden_replay

Break: in `backend/app/hat/engine/backtest.py`: the open no longer checked against the stop, so a gap through it fills at the stop on the way down.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:166: in test_backtest_golden_replay
    assert s.golden_document() == golden
E   AssertionError: assert {'momentum-br...ist_rows': 8}} == {'momentum-br...ist_rows': 8}}
E
E     Omitting 1 identical items, use -vv to show
E     Differing items:
```

Break (CodeAnt review, excursions stop at a fill): in `backend/app/hat/engine/backtest.py`: a stopped position's excursion takes the bar's low below the stop fill.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:166: in test_backtest_golden_replay
    assert s.golden_document() == golden
E   AssertionError: assert {'momentum-br...ist_rows': 8}} == {'momentum-br...ist_rows': 8}}
E
E     Omitting 1 identical items, use -vv to show
E     Differing items:
```

### PBT costs.slippage_adverse

Break: in `backend/app/hat/backtest/costs.py`: a buy's slipped price rounded down.

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:59: in test_slippage_adverse
    @example(D("100"), "buy", D("9999"))
                   ^^^
tests/hat/properties/test_costs.py:66: in test_slippage_adverse
    assert got == (price * (1 + bps / 10_000)).quantize(PLACES, rounding=ROUND_CEILING)
E   AssertionError: assert Decimal('0.0001') == Decimal('0.0002')
```

### PBT costs.slippage_adverse_net

Break: in `backend/app/hat/backtest/costs.py`: a sell slipped upwards (in the trade's favour).

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:75: in test_slippage_adverse_net
    def test_slippage_adverse_net(entry: D, exit_: D, qty: int, low: D, high: D) -> None:
                   ^^^
tests/hat/properties/test_costs.py:86: in test_slippage_adverse_net
    assert net(high) <= net(low)
E   AssertionError: assert Decimal('2.0000') <= Decimal('1.9999')
```

### PBT costs.slippage_adverse_bounds

Break: in `backend/app/hat/backtest/costs.py`: a slippage of exactly 10000 bps accepted.

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:90: in test_slippage_adverse_bounds
    st.one_of(
tests/hat/properties/test_costs.py:100: in test_slippage_adverse_bounds
    with pytest.raises(CostError):
         ^^^^^^^^^^^^^^^^^^^^^^^^
E   Failed: DID NOT RAISE CostError
```

### PBT costs.charges_contract

Break: in `backend/app/hat/backtest/costs.py`: GST charged on every component, taxable or not.

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:106: in test_charges_contract
    def test_charges_contract(raw: dict[str, Any], side: str, qty: int, price: D, more: int) -> None:
                   ^^^
tests/hat/properties/test_costs.py:124: in test_charges_contract
    assert got.gst == _money(taxable * D(raw["gst_rate"])) >= 0
E   AssertionError: assert Decimal('0.01') == Decimal('0.00')
```

### PBT costs.charges_contract_run

Break: in `backend/app/hat/backtest/costs.py`: fees charged on the reference price, not the slipped fill.

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:134: in test_charges_contract_run
    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
                   ^^^
tests/hat/properties/test_costs.py:143: in test_charges_contract_run
    assert fill.fees == charges(model, fill.side, fill.qty, fill.price).total
E   AssertionError: assert Decimal('29.6000') == Decimal('29.62')
```

### PBT costs.charges_contract_float_rates

Break: in `backend/app/hat/backtest/costs.py`: float rates accepted.

```
tests/hat/properties/test_costs.py F
tests/hat/properties/test_costs.py:152: in test_charges_contract_float_rates
    def test_charges_contract_float_rates(rate: object) -> None:
                   ^^^
tests/hat/properties/test_costs.py:155: in test_charges_contract_float_rates
    with pytest.raises(CostError):
         ^^^^^^^^^^^^^^^^^^^^^^^^
```

### CONF backtest.pbt_money_strategies_decimal

Break: in `backend/tests/hat/properties/test_costs.py`: a bare `st.floats` strategy added to `test_costs.py`.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:201: in test_pbt_money_strategies_decimal
    assert _is_call(parents.get(node), "one_of"), (area, node.lineno)
E   AssertionError: ('costs', 30)
E   assert False
E    +  where False = _is_call(Assign(targets=[Name(id='LOOSE', ctx=Store())], value=Call(func=Attribute(value=Name(...), attr='floats', ctx=Load(...)), args=[Constant(value=0, kind=None), Constant(value=1, kind=None)], keywords=[]), type_comment=None), 'one_of')
E    +    where Assign(targets=[Name(id='LOOSE', ctx=Store())], value=Call(func=Attribute(value=Name(...), attr='floats', ctx=Load(...)), args=[Constant(value=0, kind=None), Constant(value=1, kind=None)], keywords=[]), type_comment=None) = <built-in method get of dict object at 0x7f8eebe36780>(Call(func=Attribute(value=Name(id='st', ctx=Load(...)), attr='floats', ctx=Load()), args=[Constant(value=0, kind=None), Constant(value=1, kind=None)], keywords=[]))
```

### PBT dsr.bounds_and_monotonicity

Break: in `backend/app/hat/backtest/dsr.py`: SR0 negated, so more trials raise the DSR.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:75: in test_bounds_and_monotonicity
    @example(ReturnStats(0.1, 250, 0.0, 3.0), 1, 0.0, 2)
                   ^^^
tests/hat/properties/test_dsr.py:81: in test_bounds_and_monotonicity
    assert deflated_sharpe(s, trials + more, variance).prob <= got.prob + 1e-12
E   assert 0.5006557055110974 <= (0.5 + 1e-12)
```

### PBT dsr.bounds_and_monotonicity_in_sr

Break: in `backend/app/hat/backtest/dsr.py`: the numerator reversed to SR0 - SR.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:85: in test_bounds_and_monotonicity_in_sr
    def test_bounds_and_monotonicity_in_sr(s: ReturnStats, other: float, trials: int, v: float) -> None:
                   ^^^
tests/hat/properties/test_dsr.py:96: in test_bounds_and_monotonicity_in_sr
    assert first.prob <= second.prob + 1e-12
E   assert 0.5 <= (0.49960105778608893 + 1e-12)
```

### PBT dsr.bounds_and_monotonicity_first_trial

Break: in `backend/app/hat/backtest/dsr.py`: the N = 1 case left to the inverse normal.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:100: in test_bounds_and_monotonicity_first_trial
    def test_bounds_and_monotonicity_first_trial(
                   ^^^
tests/hat/properties/test_dsr.py:105: in test_bounds_and_monotonicity_first_trial
    got = deflated_sharpe(s, trials, v)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
```

### PBT dsr.bounds_and_monotonicity_undefined

Break: in `backend/app/hat/backtest/dsr.py`: the volatility floor removed from `return_stats`.

```
tests/hat/properties/test_dsr.py F
  + Exception Group Traceback (most recent call last):
  |   File "/tmp/claude-1000/-data-workspace-wt-hat-pr12-backtest/b5cc6546-d93d-4f00-8bcc-c5ce687ba958/scratchpad/pr12/root4/backend/tests/hat/properties/test_dsr.py", line 116, in test_bounds_and_monotonicity_undefined
  |     st.one_of(
  |
  |   File "/data/workspace/algotrading/venv/lib/python3.14/site-packages/hypothesis/core.py", line 2299, in wrapped_test
  |     raise the_error_hypothesis_found
```

### PBT dsr.bounds_and_monotonicity_bad_denominator

Break: in `backend/app/hat/backtest/dsr.py`: the radicand floor replaced by `<= 0`.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:134: in test_bounds_and_monotonicity_bad_denominator
    def test_bounds_and_monotonicity_bad_denominator(
                   ^^^
tests/hat/properties/test_dsr.py:143: in test_bounds_and_monotonicity_bad_denominator
    assert (got.prob, got.reason) == (0.0, UNDEFINED)
E   AssertionError: assert (1.0, None) == (0.0, 'dsr_undefined')
```

### CONF backtest.dsr_published_example

Break: in `backend/app/hat/backtest/dsr.py`: SR0 from the first quantile alone.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:144: in test_dsr_published_example
    assert round(expected_max_sharpe(trials, variance), 4) == 0.1132
E   assert 0.104 == 0.1132
E    +  where 0.104 = round(0.10403743971334876, 4)
E    +    where 0.10403743971334876 = expected_max_sharpe(100, 0.002)
FAILED tests/hat/conformance/test_conf_backtest.py::test_dsr_published_example - assert 0.104 == 0.1132
```

### PBT dsr.trial_counting

Break: in `backend/app/hat/backtest/runs.py`: failed runs not counted as trials.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:190: in test_trial_counting
    @settings(max_examples=40, deadline=None)
                   ^^^
tests/hat/properties/test_dsr.py:210: in test_trial_counting
    assert after[key] == rows
E   assert 0 == 1
```

### PBT dsr.family_key

Break: in `backend/app/hat/backtest/runs.py`: the family key drops the universe.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:217: in test_family_key
    @settings(max_examples=40, deadline=None)
                   ^^^
tests/hat/properties/test_dsr.py:234: in test_family_key
    assert (first.family_key == second.family_key) is same
E   AssertionError: assert ('01a15f36-7e2...-dce57a6882c1' == '01a15f36-7e2...-dce57a6882c1'
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

### PBT dsr.evidence_cooldown_in_force

Break: in `backend/app/hat/backtest/runs.py`: the newest completed run is the evidence whatever its cooldown.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:304: in test_evidence_cooldown_in_force
    assert facts.trades == (want[-1] if want else 0)
E   AssertionError: assert 2 == 1
E    +  where 2 = BacktestFacts(dsr_prob=Decimal('0.778222'), trades=2, expectancy=Decimal('2'), trials=2).trades
```

### PBT dsr.evidence_promoted_only

Break: in `backend/app/hat/backtest/runs.py`: the evidence reads the newest completed run whether or not it is flagged for promotion.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:350: in test_evidence_promoted_only
    assert facts.trades == (want[-1] if want else 0)
E   AssertionError: assert 1 == 0
E    +  where 1 = BacktestFacts(dsr_prob=Decimal('0.778222'), trades=1, expectancy=Decimal('1'), trials=1).trades
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_evidence_promoted_only(
E       flags=[None],
E   )
```

### PBT dsr.gate_evidence

Break: in `backend/app/hat/backtest/runs.py`: the benchmark is the plain mean of the sleeves' expectancies, not weighted by their trades.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:375: in test_gate_evidence
    st.lists(SLEEVE, min_size=1, max_size=3),
               ^^^
tests/hat/properties/test_dsr.py:433: in test_gate_evidence
    assert _numbers(got) == _numbers(want)
E   AssertionError: assert {'live_trades...imal('0.005')} == {'live_trades...33333333333')}
```

Break: `gate_evidence` counts every closed trade, whatever version is in force (main before).

```
E   AssertionError: assert {'live_trades...000000'), ...} == {'live_trades...ecimal('0.5')}
```

Break: the newest version is taken even while it is pending.

```
E   AssertionError: assert {'live_trades...ecimal('0.5')} == {'live_trades...ecimal('0.5')}
```

### PBT dsr.family_floors

Break: in `backend/app/hat/backtest/dsr.py`: `family_deflated_sharpe` drops the trial-count floor.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:171: in test_family_floors
    assert (got.prob, got.sr0, got.reason) == (floored.prob, floored.sr0, floored.reason)
E   assert (0.942261266719699, 0.0, None) == (0.8371427528 [cut]5896005, None)
E     At index 0 diff: 0.942261266719699 != 0.837142752822158
E     Full diff:
E       (
E     -     0.837142752822158,
E     -     0.03756318025896005,
```

### PBT dsr.pbo_ranked_variants

Break: in `backend/app/hat/backtest/dsr.py`: `pbo` picks the in-sample worst variant instead of the best.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:197: in test_pbo_ranked_variants
    assert found.prob == 0.0
E   assert 1.0 == 0.0
E    +  where 1.0 = Pbo(prob=1.0, splits=2).prob
E   Failing explicit example: test_pbo_ranked_variants(
E       noise=[0.01,
E        -0.01,
E        0.02,
```

### PBT dsr.pbo_two_variants

Break: in `backend/app/hat/backtest/dsr.py`: `pbo` picks the in-sample worst variant instead of the best.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:228: in test_pbo_two_variants
    assert math.isclose(found.prob, lost / len(halves), abs_tol=1e-12)
E   assert False
E    +  where False = <built-in function isclose>(1.0, (0 / 2), abs_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
E    +    and   1.0 = Pbo(prob=1.0, splits=2).prob
E    +    and   2 = len([(0,), (1,)])
E   Failing explicit example: test_pbo_two_variants(
```

### PBT dsr.pbo_refused

Break: in `backend/app/hat/backtest/dsr.py`: `pbo` needs only one session per block.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:245: in test_pbo_refused
    assert pbo(matrix, blocks) is None
E   assert Pbo(prob=1.0, splits=252) is None
E    +  where Pbo(prob=1.0, splits=252) = pbo([[0.0, 0.0, 0.0, 0.0, 0.0, 0.0,  [cut]], [0.0, 0.0, 0.0, 0.0, 0.0, 0.0,  [cut]]], 10)
E   Failing test case: test_pbo_refused(
E       case='short',
E       noise=[0.0,
E        0.0,
```

### PBT dsr.dsr_needs_recorded_run

Break: in `backend/app/hat/backtest/runs.py`: `_recorded` checks only that the run has an id, so a transient run is scored.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:743: in test_dsr_needs_recorded_run
    with pytest.raises(runs.RunError, match="backtest_run_unrecorded"):
E   Failed: DID NOT RAISE RunError
E   Failing test case: test_dsr_needs_recorded_run(
E       # The test always failed when commented parts were varied together.
E       others=[],  # or any other generated value
E       sr=0.0,  # or any other generated value
E       recorded=False,
```

### PBT dsr.returns_persisted

Break: in `backend/app/hat/backtest/runs.py`: the first return ignores the initial equity (null).

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:785: in test_returns_persisted
    assert table.column("ret").to_pylist() == want
E   assert [None] == [0.0]
E     At index 0 diff: None != 0.0
E     Full diff:
E       [
E     -     0.0,
E     +     None,
```

### PBT dsr.evidence_latest_by_completion

Break: in `backend/app/hat/backtest/runs.py`: `_latest_run` orders the candidates by id (start) again.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:809: in test_evidence_latest_by_completion
    assert runs.backtest_evidence(session, bot).trades == order[-1] + 1
E   AssertionError: assert 4 == (0 + 1)
E    +  where 4 = BacktestFacts(dsr_prob=Decimal('0.683814'), trades=4, expectancy=Decimal('1'), trials=4).trades
E    +    where BacktestFacts(dsr_prob=Decimal('0.683814'), trades=4, expectancy=Decimal('1'), trials=4) = <function backtest_evidence>(<sqlalchemy.orm.session.Session object>, <app.hat.models.strateg [cut]
E    +      where <function backtest_evidence> = runs.backtest_evidence
E   Failing explicit example: test_evidence_latest_by_completion(
E       order=[3, 2, 1, 0],
```

### PBT dsr.family_pbo_recorded

Break: in `backend/app/hat/backtest/runs.py`: `family_pbo` reads only the newest completed run.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:840: in test_family_pbo_recorded
    assert (got["variants"], got["sessions"]) == (len(series), 24)
E   assert (1, 24) == (2, 24)
E     At index 0 diff: 1 != 2
E     Full diff:
E       (
E     -     2,
E     ?     ^
```

### PBT dsr.family_pbo_skips_unreadable

Break: in `backend/app/hat/backtest/runs.py`: `read_returns` reads a sibling's file without a guard, so a damaged file fails the run being completed.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:874: in test_family_pbo_skips_unreadable
    runs.complete_run(
E   pyarrow.lib.ArrowInvalid: Could not open Parquet input source '/tmp/tmpi697pvfz/backtests/run_id=01a1668c-3f48-75c3-b774-f1a1b5c914ad/returns.parquet': Parquet file size is 0 bytes
E   Failing explicit example: test_family_pbo_skips_unreadable(
E       garbage=b'',
E   )
```

### CONF backtest.dsr_scored_only_by_runs

Break: in `backend/app/hat/backtest/market.py`: `market.py` imports `pbo` from `backtest/dsr.py`.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:176: in test_dsr_scored_only_by_runs
    assert offenders == []
E   AssertionError: assert ['hat/backtest/market.py'] == []
E     Left contains one more item: 'hat/backtest/market.py'
E     Full diff:
E     - []
E     + [
E     +     'hat/backtest/market.py',
```

### PBT confidence.label_rule

Break: in `backend/app/hat/engine/confidence.py`: a bucket of any size can be high (`trades < 1`).

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:78: in test_label_rule
    assert label == LOW
E   AssertionError: assert 'high' == 'low'
E     - low
E     + high
E   Failing explicit example: test_label_rule(
E       stats=BucketStats(trades=29,
E        mean_r=Decimal('1'),
E        ci90_low=Decimal('0.5'),
E        ci90_high=Decimal('2'),
```

### PBT confidence.label_monotone

Break: in `backend/app/hat/engine/confidence.py`: a bucket above 150 trades is low.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:104: in test_label_monotone
    assert RANK[label_of(better)[0]] >= RANK[label_of(stats)[0]]
E   assert 0 >= 1
E   Failing test case: test_label_monotone(
E       # The test always failed when commented parts were varied together.
E       stats=BucketStats(
E           trades=94,
E           mean_r=Decimal('0.001'),
E           ci90_low=None,  # or any other generated value
E           ci90_high=None,  # or any other generated value
```

### PBT confidence.band_of

Break: in `backend/app/hat/engine/confidence.py`: a band includes its upper edge, so 0.75 falls in 0.60-0.75.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:129: in test_band_of
    assert [band] == inside
E   AssertionError: assert ['0.60-0.75'] == ['0.75-0.90']
E     At index 0 diff: '0.60-0.75' != '0.75-0.90'
E     Full diff:
E       [
E     -     '0.75-0.90',
E     +     '0.60-0.75',
E       ]
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
```

### PBT confidence.bucket_stats

Break: in `backend/app/hat/engine/confidence.py`: the interval is half as wide.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:157: in test_bucket_stats
    assert abs(stats.ci90_low - (mean - half)) <= Decimal("0.000002")
E   AssertionError: assert Decimal('0.0004114268134757361000000000002') <= Decimal('0.000002')
E    +  where Decimal('0.0004114268134757361000000000002') = abs((Decimal('0.000089') - (Decimal('0.0005') - Decimal('0.0008224268134757361000000000002'))))
E    +    where Decimal('0.000089') = BucketStats(trades=2, mean_r=Decimal('0.000500'), ci90_low=Decimal('0.000089'), ci90_high=Decimal('0.000911'), win_rate=Decimal('0.500000')).ci90_low
E    +  and   Decimal('0.000002') = Decimal('0.000002')
E   Failing test case: test_bucket_stats(
E       rs=[Decimal('0.000'), Decimal('0.001')],
E       rnd=HypothesisRandom(generated data),
E   )
```

### PBT confidence.buckets_match_trades

Break: in `backend/app/hat/backtest/runs.py`: `complete_run` stores no buckets.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:234: in test_buckets_match_trades
    assert stored == reread
E   AssertionError: assert {} == {('pb', '0.60 [cut]('0.000000'))}
E     Right contains 1 more item:
E     {('pb', '0.60-0.75'): BucketStats(trades=1,
E                                       mean_r=Decimal('0.000000'),
E                                       ci90_low=None,
E                                       ci90_high=None,
E                                       win_rate=Decimal('0.000000'))}
E     Full diff:
```

### PBT confidence.signal_label_from_evidence

Break: in `backend/app/hat/backtest/runs.py`: the run's known gaps are not read.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:299: in test_signal_label_from_evidence
    assert signal.confidence == expected
E   AssertionError: assert 'high' == 'low'
E     - low
E     + high
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_signal_label_from_evidence(
E       evidence='high',
E       playbook='pb',
E       gaps='unreadable',
```

### PBT confidence.backtest_label_is_fixed

Break: in `backend/app/hat/engine/confidence.py`: the backtest label reads `no_run`.

```
tests/hat/properties/test_confidence.py F
tests/hat/properties/test_confidence.py:314: in test_backtest_label_is_fixed
    assert confidence.BACKTEST_CONFIDENCE.basis == {"reason": confidence.BACKTEST}
E   AssertionError: assert {'reason': 'no_run'} == {'reason': 'backtest'}
E     Differing items:
E     {'reason': 'no_run'} != {'reason': 'backtest'}
E     Full diff:
E       {
E     -     'reason': 'backtest',
E     +     'reason': 'no_run',
E       }
```
