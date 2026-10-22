---
name: verify-backtest
description: How to run, read and update the checks behind Hire a Trader's backtest (the shared day loop over the live engine, the daily-bar fill proxies, slippage and the statutory cost model, run recording and trial counting, the deflated Sharpe ratio, the lifecycle and risk-increase evidence, determinism and the S1/S2 golden replay). Use before and after any change to backend/app/hat/engine/backtest.py, backend/app/hat/backtest (costs, fills, dsr, runs, market, cost_models), the backtest fixtures or the backtest, costs and dsr properties.
---

# verify-backtest

Checks that the backtest (`backend/app/hat/engine/backtest.py` and `backend/app/hat/backtest/`) tells the truth about a strategy version: it runs the live scan, risk and exit code rather than copies, it never reads the future or forgets dead stocks, its fills are never better than the market allowed and pay every charge, every run is a counted trial, the deflated Sharpe ratio matches the published formula and is recomputed when the family grows, and the same inputs always give the same bytes. Specs: `docs/specs/hat/09-backtest.md` (HAT-BT-010 to 061), `06-risk-and-governance.md` (HAT-GOV-091, the gate evidence), `11-verification.md` (HAT-VER-041).

## Properties

- `CONF backtest.backtest_shares_engine_imports` The day loop imports the live engine functions (`scan_and_publish`, `open_entry`, `evaluate_position`, `close_position`, `expire_watchlist`, `plan_stop`, `check_width`), never the scan, criteria, scoring, sizing, caps or decision modules behind them, defines no function named like the live logic, and passes no other `decide` to `open_entry`; the cost and fill modules import no engine code (HAT-BT-010). Harm: a fix applied to the live path silently diverges from the backtest, and the evidence describes a strategy that never runs live.
- `PBT backtest.scan_parity` The watchlist the loop publishes for each simulated date equals the live `scan_bot` of the previous session run directly on the same market (same rows, pivots, stops, scores within 1e-6). Harm: the backtest trades setups the live scan would not find.
- `PBT backtest.no_lookahead` Replacing every bar and index close after a date d leaves every watchlist, signal, fill, closed trade and equity point on or before d unchanged (HAT-BT-011). Harm: the backtest uses prices the market had not printed, and every sleeve looks better than it is.
- `PBT backtest.survivorship_free` An instrument that stops trading at session x is scanned as a member before x, no fill of it is dated after its last bar, and every position in it closes no later than that bar (HAT-BT-012). Harm: dead stocks vanish from the test and returns are overstated.
- `PBT backtest.market_point_in_time` `load_market` reads bars and index closes up to the period's end only; an instrument delisted by the end has its last bar on or before its delisting as its last traded session, one delisted later has none; industry classifications come with their effective dates and `industry_on` gives the one in force on each day (HAT-BT-011, HAT-BT-012). Harm: a run closes a live stock as delisted, or a later reclassification changes an earlier industry-cap decision.
- `PBT backtest.version_pinned` Every signal and trade carries the run's strategy and risk-policy versions, and the metrics name them (HAT-BT-013). Harm: evidence cannot be tied to the version being promoted.
- `PBT backtest.entry_fill_proxy` An entry fills at the level when the open is at or below it and the high reaches it, at the open on a gap over, and not at all when the high stays below (HAT-BT-020). Harm: the backtest buys a gap at the level, a price the market never offered.
- `PBT backtest.entry_fill_proxy_chase` In a run, every opened entry records its level as the trigger price (HAT-CRIT-119) and its buy filled at the proxy price of that level and within max chase of it, with the chase drawn from 0.5%, 1% and 5%. Harm: the backtest buys gaps the live risk check refuses.
- `PBT backtest.stop_on_instrument_tick` Every opened entry's stop is entry x 0.95 rounded down to the instrument's NSE tick of the trade date (`data.ticks.tick_for_date` on the closes before it), with prices scaled into the 0.01 and the 0.10 slab (HAT-BT-020 design, HAT-EXEC-053). Harm: backtest stops sit on a grid the live trigger never uses, so backtest and live risk differ.
- `PBT backtest.pre_break_low` The entry day's low before the break is the lowest low of the 5-minute bars up to the first one that reaches the level, and the low after it the lowest of the later bars (or a day low they miss); without them it is max(day low, min(open, level x (1 - adr20/100))), with the day low after the break only when lower (HAT-BT-020 design, PR 60). Harm: a low-of-day stop reads a low made after the entry.
- `PBT backtest.entry_day_stop` Under a low-of-day stop, with and without generated 5-minute bars, every entry's stop is the live plan from the low before the break, a gap over without 5-minute bars is skipped and counted as `gap_over_no_intraday`, an entry without them needs adr20 (generated markets blank it on some dates), and an entry-day low at or below the stop exits that day at -1 R (MAE -1), while a higher one bounds the MAE (HAT-BT-020 design, PR 60). Harm: an S2 entry's stop sits at a low made after the entry, and the entry day's loss is never seen.
- `PBT backtest.contraction_stop_too_wide` A contraction stop the scan left empty skips the trigger as `stop_too_wide`, never `stop_input_missing` (HAT-BT-020 design, PR 60). Harm: skip counts blame missing data for a stop the plan refused as too wide.
- `PBT backtest.adtr_filter_applied` With an `adtr_filter`, a trigger whose stop is wider than max_multiple x ADTR(20) as of the session before (or with no ADTR, blanked at some closes) is Rejected `adtr_filter` and buys nothing; one within it is never refused for that reason (HAT-BT-025, PR 61). Harm: the backtest takes wide-stop entries live refuses.
- `PBT backtest.upper_circuit_inferred` An entry price that is the day's high, above the previous close and within one tick of the upper price of an NSE band (2, 5, 10, 20%) above the previous close is locked; one below the high, at or below the previous close, or between bands is not; no previous close or tick is unknown (HAT-BT-025, PR 61). Harm: the backtest buys a stock locked at its upper band.
- `PBT backtest.upper_circuit_skip` In a run with sessions at an upper band (pinned all day, or opened at the band and faded), a trigger entering at the band is Rejected `max_chase` and buys nothing, and with a 30% chase no other trigger is (HAT-BT-025, PR 61). Harm: the backtest buys locked gap-ups live never gets.
- `PBT backtest.cooldown_applied` In a run whose policy has `cooldown_sessions` N in [0, 4] (`with_cooldown`), with tight stops so hard stops and re-triggers are common, every trigger Rejected `cooldown` follows a hard stop of its instrument within N sessions and buys nothing, no entry opens within N sessions after one, no signal is refused for a missing or invalid input (the stop history is passed when N > 0), and the run records N (HAT-RISK-036, PR 58). Harm: the knob the cooldown sweep turns does nothing, or the backtest refuses entries live takes.
- `PBT backtest.exit_fill_proxy` A stop fills at min(open, stop) when the day reaches it and is never better than the stop; a day above it does not fill (HAT-BT-021). Harm: gap-down losses are understated.
- `PBT backtest.exit_fill_proxy_close` A trade closed by a close-based rule sold at that day's official close, and `close_exits_at_official_close` is a recorded known gap. Harm: close exits are priced at a better point of the day than the rule could see.
- `PBT backtest.decimal_money` Quantities are whole and at least 1, money is Decimal, and the trades' net P&L sums exactly to the change in equity (HAT-BT-024). Harm: float drift makes equity and the trades disagree.
- `PBT backtest.trades_match_metrics` The rows and sum of R in `trades.parquet` equal the metrics' `trades` and `sum_r` (HAT-BT-032). Harm: the reported evidence does not match the trades behind it.
- `PBT backtest.r_and_expectancy` R = realised P&L / ((entry − initial stop) × qty), a whole exit at the initial stop without costs is −1 R, and expectancy is the mean R (HAT-BT-040). Harm: R values are inconsistent and the lifecycle and veto scores read wrong numbers.
- `PBT backtest.metrics_bounds` Max drawdown and win rate are in [0, 1], and the metrics do not depend on the order of trades closing on the same day (HAT-BT-041). Harm: impossible values reach the lifecycle gate.
- `PBT backtest.deterministic` Two runs of the same market, with rows, features, members and mappings shuffled, give byte-identical trade files and equal metrics JSON, including the known-gaps list (HAT-BT-050). Harm: evidence cannot be reproduced, so a promotion cannot be audited.
- `PBT backtest.version_status_requires_run` A draft becomes `backtested` only with a completed run of that same version; a running, failed or foreign run is refused and leaves the draft untouched (HAT-BT-061). Harm: a version is promoted on a run that never finished, or on another version's evidence.
- `CONF backtest.backtest_run_columns` `hat_backtest_run` has non-null pinned versions, period, snapshot, status, metrics and family key; trades go to `backtests/run_id=<id>/trades.parquet` with the 09 §3 columns; the statuses are `running`, `completed`, `failed` (HAT-BT-032). Harm: a run cannot be traced to its inputs or its trades file.
- `CONF backtest.known_gaps_listed` Spec 09 §7's gap table (HAT-BT-070) lists exactly `KNOWN_GAPS`, in order, and the golden runs report that list (PR 61). Harm: a gap the loop has is not reported, so the evidence reads as more faithful than it is.
- `CONF backtest.backtest_golden_replay` The committed fixture market (`tests/hat/fixtures/backtest_market.csv`) run through S1 and S2 reproduces `tests/hat/fixtures/backtest_golden.json` exactly (HAT-BT-051). Harm: a silent engine change shifts every sleeve's evidence.
- `PBT costs.slippage_adverse` A buy fills at p × (1 + s/10000) rounded up to 4 places, a sell at p × (1 − s/10000) rounded down and never below 0.0001 (HAT-BT-022). Harm: rounding hands the backtest a better price than any fill.
- `PBT costs.slippage_adverse_net` A round trip's net P&L does not rise with slippage. Harm: more slippage makes a strategy look better.
- `PBT costs.slippage_adverse_bounds` A slippage outside 0 ≤ s < 10000 bps, or not a Decimal (a float, an integer, NaN, infinity), is refused. Harm: a negative slippage pays the backtest for trading.
- `PBT costs.charges_contract` On generated cost models: each charge is ≥ 0, applies only to its sides, is rate × value + per-order capped and rounded to the paisa; GST is on the taxable components only; the total is their sum plus GST and does not fall as the traded value grows (HAT-BT-023). Harm: costs are missed or double-counted and thin edges look profitable.
- `PBT costs.charges_contract_run` In a run every fill's fees are the cost model's charges on its slipped price, the trades' net P&L is gross minus those fees exactly, and the metrics name the model's version and digest. Harm: a run charges a different price than it filled at, or its results cannot be tied to a rate schedule.
- `PBT costs.charges_contract_float_rates` A rate written as a float, a boolean or nothing is refused. Harm: binary rounding reaches money.
- `CONF backtest.pbt_money_strategies_decimal` The sizing, caps, exposure, allocator and costs properties use no float tolerance, and a float strategy appears only inside `st.one_of` as invalid input (HAT-VER-041). Harm: rounding drift lets a size or a charge cross a limit and the check still passes.
- `PBT dsr.bounds_and_monotonicity` DSR is in [0, 1], never NaN, and does not rise with the trial count (HAT-BT-042). Harm: more trials make a strategy look more significant, the opposite of the correction.
- `PBT dsr.bounds_and_monotonicity_in_sr` DSR does not fall as SR rises wherever the formula's slope in SR is non-negative at both ends. Harm: a better Sharpe ratio scores worse in the region where the formula says it must not.
- `PBT dsr.bounds_and_monotonicity_first_trial` With N = 1 or V = 0, SR0 = 0 and DSR is the probabilistic Sharpe ratio against zero (to 1e-9, against `math.erf`). Harm: the first run of a family is mis-scored.
- `PBT dsr.bounds_and_monotonicity_undefined` Fewer than 2 returns, no variance, or a value that is not finite scores 0 with `dsr_undefined`, never NaN. Harm: a NaN compares false and a gate written as "not below 0.95" lets the sleeve through.
- `PBT dsr.bounds_and_monotonicity_bad_denominator` A non-positive radicand, or one within float noise of 0 (≤ 1e-12), scores 0 with `dsr_undefined`. Harm: float noise around 0 divides into a DSR of 1 and passes the gate.
- `CONF backtest.dsr_published_example` Bailey & López de Prado's example gives SR0 0.1132 and DSR 0.9004 at the printed 4 places, and the code matches the formula computed independently (`math.erf`, bisection) to 1e-6. Harm: a formula slip goes unnoticed because every other check is relative.
- `PBT dsr.trial_counting` A family's trial count is the number of runs recorded for it, failed ones included; it never falls and other families never change it (HAT-BT-030). Harm: discarded variants are forgotten and the DSR under-corrects for luck.
- `PBT dsr.family_key` Two runs share a family key iff they share strategy and universe, whatever their version or period (HAT-BT-031). Harm: a new version escapes the trial penalty.
- `PBT dsr.evidence_current_trials` The lifecycle's evidence is the newest completed run of the live version with the DSR deflated by the family's trials and Sharpe variance now, later failed runs and other versions counted; no run is no evidence (HAT-BT-060). Harm: a sleeve passes the gate on a stale trial count.
- `PBT dsr.evidence_cooldown_in_force` The sleeve's evidence is its newest completed run made under the cooldown in force when it is read (now for the lifecycle, the request's instant for the risk-increase gate): a `--cooldown-sessions` sweep run, or one made before the cooldown changed, is skipped, and a run or a policy without the field counts as 0; with none there is no evidence (HAT-RISK-036, PR 58). Harm: a gate is passed on a run of a rule that is not live.
- `PBT dsr.gate_evidence` The risk-increase evidence is `build_gate_evidence` over the user's trades closed since the policy version in force at the request took effect (every closed trade when none is; a pending version and trades of an earlier version are not read; PR 34) in closing order against the discounted trade-weighted expectancy of each sleeve's newest completed run of its live version; older runs, other versions and other users are not read, and no run is no evidence (HAT-GOV-091). Harm: a risk increase is checked against a stale or foreign benchmark.

## Run

```bash
cd backend
# every pytest, Hypothesis or break run goes under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_backtest.py tests/hat/properties/test_costs.py \
  tests/hat/properties/test_dsr.py -q --no-cov
$CAP python -m pytest tests/hat/conformance/test_conf_backtest.py -q --no-cov
# an intended engine change regenerates the golden file in the same commit
PYTHONPATH=.:tests/hat python tests/hat/hat_backtest_support.py
```

No check touches the network, Fyers or the HAT database: a run's ledger is an in-memory SQLite database, and the run, trial and evidence properties use SQLite in memory too. A run takes about a second, so the run-level properties use 12 examples (`RUNS`); the `test_backtest.py` file takes about a minute.

## Bug or model error

- A failing run-level property prints the generated paths (open, high, low, close per session per instrument). Rebuild the market with `hat_backtest_support.market(paths, sessions(34))` and run `run_backtest` on it; the result holds the watchlists, signals, fills, trades and equity to compare. If the real loop breaks the rule, that is a finding: fix the loop, never the property.
- `backtest.scan_parity` failing means the loop fed the scan different inputs than the live nightly scan gets (the bar window, the features date, the index closes, the members). The independent scan is `hat_backtest_support.scan_watch`; keep it independent of the loop.
- `CONF backtest.backtest_shares_engine_imports` names the private module imported or the function redefined: call the live function instead (add a parameter to it, as `fills` was added to `open_entry`, rather than copying it).
- `CONF backtest.backtest_golden_replay` failing after an intended change: check the diff of the regenerated golden file line by line, explain it in the PR, and commit it with the change. Unexplained diffs are bugs.
- `dsr.*` failures print the stats (SR, T, skew, kurtosis), N and V; recompute with the formula in 09 §4. The SR-monotonicity property holds only where the slope is non-negative; a counterexample outside that region is the formula, not the code.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- A new exit or entry behaviour belongs in the live engine (`trades.py`, `exits.py`, `stops.py`); the loop only feeds it prices. Change `fills.py` only for a new price proxy, with its 09 §2 text.
- A rate change is a new cost-model file `cost_models/<version>.yaml` with a new version; never edit a published one (runs record its digest).
- A new known gap goes into `KNOWN_GAPS`, the module docstring of `engine/backtest.py` and 09 §7 together.
- The golden fixture market was generated once (a random walk with an accumulation, flag and breakout per instrument; parameters ADV=0.025, ADV_DAYS=22, FLAG=0.006, FLAG_DAYS=6, SIDE_DAYS=20, recorded in the PR 12 description) and is frozen; regenerate only the golden JSON, not the market, for an engine change.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-backtest` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/engine/backtest.py`: `run_backtest`, `industry_on`, `compute_metrics`, `trades_parquet`, `main`
- `backend/app/hat/backtest/fills.py`: `entry_fill`, `stop_fill`
- `backend/app/hat/backtest/costs.py`: `parse_cost_model`, `load_cost_model`, `charges`, `check_slippage`, `slipped`, `BacktestFills`
- `backend/app/hat/backtest/cost_models/cnc-delivery-1.yaml`
- `backend/app/hat/backtest/dsr.py`: `return_stats`, `trial_variance`, `expected_max_sharpe`, `deflated_sharpe`
- `backend/app/hat/backtest/runs.py`: `family_key`, `trial_count`, `start_run`, `complete_run`, `fail_run`, `record_backtest`, `backtest_bot`, `backtest_evidence`, `gate_evidence`
- `backend/app/hat/backtest/market.py`: `load_market`, `_industries`
- `backend/app/hat/engine/strategy/versions.py`: `publish_version`

Checks:
- `backend/tests/hat/hat_backtest_support.py`
- `backend/tests/hat/properties/test_backtest.py`
- `backend/tests/hat/properties/test_costs.py`
- `backend/tests/hat/properties/test_dsr.py`
- `backend/tests/hat/conformance/test_conf_backtest.py`
- `backend/tests/hat/fixtures/backtest_market.csv`
- `backend/tests/hat/fixtures/backtest_golden.json`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `2f23b2f377fa3db89906eb01cf61eb230b822b73`
Covered digest: `87ecb721a998a1fe910f0fd74a9737ba4db631996c214b537a5644cf277f866f`

## Scope changes

- PR 58: the loop reads the per-instrument cooldown (HAT-RISK-036) through `trades.open_entry`, as live does; `engine.backtest.with_cooldown` and `--cooldown-sessions` (`runs.backtest_bot(cooldown_sessions=)`) replace N in the run's copy of the policy, refused as `backtest_bad_cooldown` outside [0, 250], and every run records `metrics.cooldown_sessions`; `runs.backtest_evidence` (`_latest_run`) reads only runs made under the cooldown in force (code review of PR 58: a sweep run must not become the sleeve's evidence). The golden gained that key (0) only; no trade changed.
- PR 61: the loop runs the playbook's entry filters at each trigger through `trigger.evaluate_entry_filters` (moved out of `trigger.plan_entry`, behaviour unchanged): `adtr_filter` on the planned stop with ADTR(20) as of the session before, and `max_chase` with the upper circuit inferred from the entry price and the bar (`upper_circuit_locked`: the day's high, above the previous close, within a tick of an NSE band's upper price); a failure is a Rejected signal with the filter's type, counted in `metrics.rejections`. `KNOWN_GAPS` grew from 4 to 11 codes (09 §7, HAT-BT-070). The golden was regenerated: S2's 2025-07-08 trade (entry 639.09, stop 612.60, width 26.49 against 1.0 x ADTR 24.92) is now Rejected `adtr_filter`, so S2 has one trade fewer (`net_pnl` -4358.17 to -3047.66, `avg_loss_r` -1.165 to -1.222); S1 and S2 gain the seven new gap codes and S2 `rejections: {adtr_filter: 1}`.
- PR 60: the entry day is split at the break (`pre_break_low`): a low-of-day stop is planned from the low before the break (5-minute bars, else max(day low, min(open, level x (1 - adr20/100)))), a gap-over entry under it is skipped without 5-minute bars (`gap_over_no_intraday`), as is one with neither 5-minute bars nor adr20 (`stop_input_missing`; an adr20 of 0 is a value, not a gap), the low after the break can stop the trade out on the entry day (recorded at 09:45, after the 09:30 entry) and seeds the MAE, and an empty contraction plan skips as `stop_too_wide`. The golden was regenerated: S2's 2025-07-08 entry now plans its stop from the estimated pre-break low (602.90 to 612.60, qty 34 to 47) and the day's later low stops it out that day (r -0.0055 to -1.0526, MAE -1.0), moving S2's `avg_loss_r` (-0.816 to -1.165), `sum_r` (-2.45 to -3.50) and `net_pnl` (-3054.42 to -4358.17); S1 and S2 gain the `entry_day_low` metric. The backtest loader does not read `bars_5m` yet, so a CLI run skips every S2 gap over.
- PR 54: the loop's re-check of a contraction stop at the fill price (`check_width`) also applies the rule's `min_pct` (`stop_too_narrow`, default 0 = off; property in `verify-stops`). The seeds' golden replay is unchanged by the S1 `close_vs_pivot` criterion.
- Rebased onto PR 15a (HAT-EXEC-053): the loop plans stops with the instrument's NSE slab tick from `data.ticks`, read from the adjusted closes (live reads unadjusted ones; spec 09 HAT-BT-020 design). The golden was regenerated: the scan's planned stop now uses the next session's slab tick (PR 15a, `scan.next_session_tick`): one trade's ~₹2,300 stock sits on the 0.10 tick, so its initial stop moved from 2290.55 to 2290.50, with the R, P&L and return figures that follow. The golden runs only contraction stops (planned by the scan), so `backtest.stop_on_instrument_tick` is what pins the loop's own tick.

- MFE and MAE stop at a fill: a position the stop closes on the way down, or a profit exit closes on the way up, never saw the rest of that bar (CodeAnt review of PR 12). No property isolates the excursions; the golden replay pins them (the fix moved one trade's `mae_r` and `avg_mae_r` only).

- PR 43: `main` takes its settings from `operator_settings()` (loads `.env` first) instead of `load_settings()`; `verify-data`'s `CONF store.cli_root_after_env` holds every command line to it. No backtest check changed.

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `dsr.bounds_and_monotonicity_in_sr` holds on the domain where the slope 1 − γ3 (SR + SR0)/2 + ((γ4 − 1)/4) SR × SR0 is non-negative at both ends. Finding: outside it a higher SR with a heavy skew scores lower; that is the paper's formula (recorded in 09 §4), and the code is not changed.
- `dsr.bounds_and_monotonicity_bad_denominator` found that a radicand of float noise around 0 gave DSR = 1.0; `RADICAND_FLOOR` (1e-12) now scores it undefined. With real moments the radicand is bounded below by a square (it is never negative), so the property generates moments below that bound (γ4 = 1, γ3 ≥ 1/SR).
- `backtest.entry_fill_proxy_chase` draws the max chase from 0.5%, 1% and 5%: the generated opens gap up by at most 3%, so a 5% chase alone could never bite (found by its scratch break, which first survived).
- `backtest.no_lookahead` mutates bars and index closes after d; features are derived from the bars by `compute_features`, so they follow. Corporate actions and membership changes after d are covered by the adjusted view (`verify-data`) and `backtest.survivorship_free`.
- The run-level properties use 12 examples (a run takes about a second); the pure ones use the profile's count.
- The published DSR example is checked at the paper's printed 4 places plus 1e-6 against an independent computation of the same formula, since the paper prints only 4 places.
- PR 34 (design review 2026-10-22 item 9): `runs.gate_evidence` takes the request time `at` and counts only the trades closed at or after the `effective_from` of the policy version in force then (`effective_version`), so each step up needs its own 50 trades. `policy_service.EvidenceSource` gains `at` (`request_policy_change` passes `requested_at`); `test_caps`' fixed evidence lambdas take it. `dsr.gate_evidence` draws a policy step (versions 1, 2 in force at a drawn minute, 3 pending) or none. Nothing was narrowed.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or fixture (HAT-VER-072), then the break was removed and the check returned to passing. The breaks were applied and run by one script, one at a time.

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

### PBT backtest.stop_on_instrument_tick

Break: in `backend/app/hat/engine/backtest.py`: the stop planned on the old fixed 0.05 tick instead of the instrument's NSE slab tick.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:241: in test_stop_on_instrument_tick
    assert signal.stop_price == floor_to_tick(entry * D("0.95"), tick)
E   AssertionError: assert Decimal('76.0500') == Decimal('76.07')
```

Break: in `backend/app/hat/engine/backtest.py`: the stop always planned on the finest tick (0.01), whatever the price slab.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:241: in test_stop_on_instrument_tick
    assert signal.stop_price == floor_to_tick(entry * D("0.95"), tick)
E   AssertionError: assert Decimal('2282.2800') == Decimal('2282.20')
```

### PBT backtest.pre_break_low

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: with 5-minute bars, the low before the break taken over the whole day's bars.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:279: in test_pre_break_low
    assert before == min(seen)
E   AssertionError: assert Decimal('8') == Decimal('9')
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: the trigger bar's low counted after the break as well as before it.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:280: in test_pre_break_low
    assert after == (min(rest) if rest else None)
E   AssertionError: assert Decimal('9') == None
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: a day low the 5-minute bars miss dropped instead of counted after the break.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:280: in test_pre_break_low
    assert after == (min(rest) if rest else None)
E   AssertionError: assert None == Decimal('8.99')
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: without the bars, the estimate min(open, level x (1 - adr20/100)) used even below the day's low.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:287: in test_pre_break_low
    assert before == max(day_low, min(open_, level * (1 - adr20 / 100)))
E   AssertionError: assert Decimal('8.55') == Decimal('9')
```

### PBT backtest.entry_day_stop

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: the low-of-day stop planned from the whole day's low again.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:368: in test_entry_day_stop
    assert signal.stop_price == low_of_day(entry, [before], 1, _tick(data, iid, day)).stop
E   AssertionError: assert Decimal('79.9100') == Decimal('79.99')
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: a gap over without 5-minute bars entered instead of skipped.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:356: in test_entry_day_stop
    assert result.skips.get("gap_over_no_intraday", 0) == gaps
E   AssertionError: assert 0 == 13
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: a low-of-day entry with no 5-minute bars and no adr20 entered on the whole day's low.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:366: in test_entry_day_stop
    assert intraday or adr20 is not None
E   assert (() or None is not None)
```

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: the entry day's low after the break never run (no same-day stop, no MAE seed).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:374: in test_entry_day_stop
    assert outcome == (day, "hard_stop", -1, -1)
E   AssertionError: assert (datetime.dat....7777780000')) == (datetime.dat...stop', -1, -1)
```

### PBT backtest.contraction_stop_too_wide

Break: (PR 60) in `backend/app/hat/engine/backtest.py`: an empty contraction plan skipped as `stop_input_missing` again.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:399: in test_contraction_stop_too_wide
    assert result.skips.get(SKIP_BAD_INPUT, 0) == 0
E   AssertionError: assert 13 == 0
```

### PBT backtest.adtr_filter_applied

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: the failing filter not passed to `open_entry` (no Rejected signal, the entry opens).

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:437: in test_adtr_filter_applied
    assert (signal.reason == "adtr_filter") == wide
E   AssertionError: assert (None == 'adtr_filter') == True
```

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: ADTR(20) read as of the trigger day instead of the session before.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:437: in test_adtr_filter_applied
    assert (signal.reason == "adtr_filter") == wide
E   AssertionError: assert ('adtr_filter' == 'adtr_filter'
```

### PBT backtest.upper_circuit_inferred

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: the day's-high test dropped, so an entry below the high reads as locked.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:472: in test_upper_circuit_inferred
    assert upper_circuit_locked(price, price + tick, prev_close, tick) is False
E   AssertionError: assert True is False
```

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: the above-the-previous-close test dropped, so a flat bar at the previous close reads as locked.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:474: in test_upper_circuit_inferred
    assert upper_circuit_locked(flat, flat, prev_close, tick) is False
E   AssertionError: assert True is False
```

### PBT backtest.upper_circuit_skip

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: the circuit state always False at the trigger.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:524: in test_upper_circuit_skip
    assert (signal.reason == "max_chase") == locked
E   AssertionError: assert (None == 'max_chase') == True
```

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: only a bar pinned at the band all day is locked, so an entry at the band that later faded opens.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:524: in test_upper_circuit_skip
    assert (signal.reason == "max_chase") == locked
E   AssertionError: assert (None == 'max_chase') == True
```

### PBT backtest.cooldown_applied

Break: (PR 58) in `backend/app/hat/engine/trades.py`: `open_entry` passes an empty hard-stop history, so the policy's cooldown never bites.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:480: in test_cooldown_applied
    assert not cooling(iid, day, same_day=False)
E   AssertionError: assert not True
```

Break: (PR 58) in `backend/app/hat/engine/trades.py`: `open_entry` reads the hard-stop history only when the cooldown is off, so every entry under one is refused for a missing input.

```
tests/hat/properties/test_backtest.py F
tests/hat/properties/test_backtest.py:474: in test_cooldown_applied
    assert signal.reason not in ("missing_input", "invalid_input")  # the history is passed
E   AssertionError: assert 'missing_input' not in ('missing_input', 'invalid_input')
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

Break: (PR 61) in `backend/app/hat/engine/backtest.py`: `suspension_not_marked` dropped from `KNOWN_GAPS`.

```
tests/hat/conformance/test_conf_backtest.py F
tests/hat/conformance/test_conf_backtest.py:167: in test_known_gaps_listed
    assert tuple(codes) == KNOWN_GAPS
E   AssertionError: assert ('trigger_is_...at_open', ...) == ('trigger_is_...at_open', ...)
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

Break: (PR 58) in `backend/app/hat/backtest/runs.py`: the newest completed run is the evidence whatever its cooldown.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:304: in test_evidence_cooldown_in_force
    assert facts.trades == (want[-1] if want else 0)
E   AssertionError: assert 2 == 1
E    +  where 2 = BacktestFacts(dsr_prob=Decimal('0.778222'), trades=2, expectancy=Decimal('2'), trials=2).trades
```

Break: (PR 58) in `backend/app/hat/backtest/runs.py`: a run without `cooldown_sessions` does not read as 0.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:304: in test_evidence_cooldown_in_force
E   assert 0 == 1
E    +  where 0 = BacktestFacts(dsr_prob=None, trades=0, expectancy=None, trials=0).trades
E   Failing test case: test_evidence_cooldown_in_force(
E       in_force=None,
E       later=None,  # or any other generated value
E       made=[None],
E   )
```

Break: (PR 58) in `backend/app/hat/backtest/runs.py`: `gate_evidence` picks each sleeve's run by the cooldown in force now, not at the request.

```
tests/hat/properties/test_dsr.py F
tests/hat/properties/test_dsr.py:312: in test_evidence_cooldown_in_force
    assert _numbers(got) == _numbers(build_gate_evidence([], benchmark))
E   AssertionError: assert {'live_trades [cut]cimal('0.84')} == {'live_trades [cut]cimal('0.42')}
E     Differing items:
E     {'backtest_expectancy_r': Decimal('0.84')} != {'backtest_expectancy_r': Decimal('0.42')}
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

Break (PR 34): `gate_evidence` counts every closed trade, whatever version is in force (main before PR 34).

```
E   AssertionError: assert {'live_trades...000000'), ...} == {'live_trades...ecimal('0.5')}
```

Break (PR 34): the newest version is taken even while it is pending.

```
E   AssertionError: assert {'live_trades...ecimal('0.5')} == {'live_trades...ecimal('0.5')}
```
