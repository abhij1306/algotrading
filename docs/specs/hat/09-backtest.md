# 09: Backtest

Scope: the backtest engine that shares scan, risk and exit code with live trading, the fill and cost model, backtest runs and trial counting, metrics and the deflated Sharpe ratio, replay determinism, and the evidence the sleeve lifecycle consumes. Requirement format and verification methods: see README.md and 11-verification.md.

One engine runs backtest and live (T12). Backtests are the first stage of every release gate (BL "Governance"): a draft strategy version is backtested automatically, then shadowed, then promoted. Every backtest run counts as a trial so the deflated Sharpe ratio can correct for the number of variants tried (BL "Sleeve lifecycle"; PL4).

## §1 Shared engine contract

#### HAT-BT-010: Backtest calls the same scan, risk and exit code as live
- **Statement:** The backtest day loop calls the same universe/setup scan, `evaluate_signal` sizing and caps, stop rules and exit-stack functions that live trading calls; it has no copies of them.
- **Source:** MA "Decisions register" (T12 one engine for backtest and live); MA "Boundary rules" (criteria library keeps backtest and live on one code path); PLAN PR 12.
- **PR:** 11, 12
- **Verification:** CONF `backtest_shares_engine_imports`, PBT `backtest.scan_parity`
- **Acceptance:**
  - Property: for every generated feature frame and trade date, the watchlist produced by the backtest loop equals the watchlist produced by the live nightly scan for the same bot version, universe version and date. Harm: backtest evidence describes a strategy that never runs live.
  - Property: the import graph of `app/hat/engine/backtest` reaches scan, sizing and exit logic only through the same functions the live path imports. Harm: a fix applied to one path silently diverges from the other.
- **Design (PR 12):** the loop drives the live functions (`watchlist.scan_and_publish`, `trades.open_entry`, `trades.evaluate_position`, `trades.close_position`, `trades.expire_watchlist`, `stops.plan_stop`, `stops.check_width`) on a private ledger: an in-memory SQLite database with the shadow engine's schema, holding the run's one bot, its pinned version, its point-in-time universe and the virtual signals, orders, fills, positions and trade results. The ledger never touches the HAT database and nothing in it reaches a broker. Each simulated date d runs, in order: the nightly scan of d's previous session published as d's watchlist; the nightly and live exit rules of each open position along d's path (§2); the entries; the 15:25 close rules at the official close; the delisting closes (HAT-BT-012); the session-end watchlist expiry. The exposure ladder is simulated with each backtest trade counted as a real one; kill switches are not (their re-arm is manual), and a sleeve's allocation is the policy's risk per trade with regime and health multipliers of 1. These gaps are listed in every run's `metrics.known_gaps` (§7).

#### HAT-BT-011: No look-ahead
- **Statement:** Every decision the backtest makes for trade date d uses only bars, features, corporate actions, universe membership and regime state dated <= d (with intraday triggers on d using only d's own prices up to the trigger).
- **Source:** BL "Stock universes" (point-in-time); BL "Regime model v2" (features known at close t, used from t+1); DS "Historical data build"; T12.
- **PR:** 12
- **Verification:** PBT `backtest.no_lookahead`
- **Acceptance:**
  - Property: for every generated market history and every date d, mutating any data dated after d leaves all of the backtest's decisions (watchlists, signals, fills, exits) on or before d unchanged. Harm: the backtest uses information the market did not have, and every sleeve looks better than it is.
- **Design (PR 12):** d's watchlist is the nightly scan of d's previous session, run on the bars, features and index closes dated on or before that session; the scan's window holds 700 sessions, more than the 652 the longest criterion reads, so it equals a scan of the full history.

#### HAT-BT-012: Survivorship-free, adjusted data
- **Statement:** The backtest reads the adjusted-bar view of the Parquet store (raw bars × cumulative corporate-action factors) and point-in-time universe membership, including instruments delisted since.
- **Source:** DS "Historical data build" (delisted stocks come in automatically); SCH "Parquet" (adjusted prices are a DuckDB view); BL "Stock universes"; B27.
- **PR:** 12
- **Verification:** PBT `backtest.survivorship_free`
- **Acceptance:**
  - Property: for every generated history containing an instrument delisted at date x, the instrument is eligible on dates < x where it was a universe member, and its open positions are closed no later than its last traded bar. Harm: dead stocks vanish from the test and returns are overstated.
- **Design (PR 12):** a position still open on its instrument's last traded session is closed at that session's close with exit reason `delisted`, so a delisted stock's loss stays in the results. Positions open on the period's last date close at that date's close with exit reason `end_of_period`.

#### HAT-BT-013: A run pins one strategy version and one risk-policy version
- **Statement:** Each backtest run records and uses exactly one `strategy_version_id` and one `risk_policy_version_id` for its whole period.
- **Source:** SCH "Backtest and ops tables" (`hat_backtest_run`); BL "Summary" (Versioned).
- **PR:** 12
- **Verification:** PBT `backtest.version_pinned`
- **Acceptance:**
  - Property: for every run, every simulated signal and trade carries the run's strategy and risk-policy version IDs. Harm: evidence cannot be tied to the version being promoted.

## §2 Fill and cost model

Daily bars cannot show where in the day a pivot broke. The trigger proxy below is a documented approximation; the gap between it and live fills is measured in shadow mode (PLAN Q4; MA "Risks and open items").

Each simulated day is read as one path: **open, then low, then high, then close** (the adverse move first). At the open the nightly rules and then the live rules see the open price, so a gap through a stop exits at the open. On the way down a stop fills by HAT-BT-021; on the way up each profit rule (`partial_at_r`, `pct_gain_target`, a raised stop) fires at the first price at which `exits.evaluate` acts, as many times as the stack keeps acting; an entry breaks its level on the way up (from below, after the day's low) or at the open (a gap over). The close rules then see the official close. The fill proxies are pure functions in `app/hat/backtest/fills.py`; whether an entry is taken is `evaluate_signal`'s decision and which exit fires is `exits.evaluate`'s.

#### HAT-BT-020: Entry fill on daily bars
- **Statement:** On daily bars, an entry fills at the trigger level when the day's high reaches it, or at the open when the day opens above the trigger level (gap over); otherwise no entry.
- **Source:** PLAN Q4; BL "Criteria library" (`price_breaks_level`, `max_chase`).
- **PR:** 12
- **Verification:** PBT `backtest.entry_fill_proxy`, PBT `backtest.entry_fill_proxy_chase`
- **Acceptance:**
  - Property: for every (open, high, level), the entry price is level if open <= level <= high, open if open > level, and there is no entry if high < level; the entry is still subject to max-chase, so a gap-over open above level × (1 + pct/100) is skipped. Harm: the backtest buys at prices the live engine would refuse, inflating results.
- **[OWNER-PENDING]** daily-bar trigger proxy: default as stated (PLAN Q4); episodic-pivot (opening-range) backtests need the Fyers minute history the optional loader writes to `bars_5m` (HAT-DATA-119, I4); the discrepancy is measured in shadow; owner to confirm.
- **Deviation (PR 12):** the trigger is the break of the level alone. `volume_pace`, `adtr_filter` and the opening range need intraday bars and are not modelled, and the episodic-pivot playbook, built live from the pre-open gap scan, is not backtested (its rows are skipped): the PR 12 loop runs on daily bars only, and the minute-history loader exists for a later EP backtest. Both are listed in `metrics.known_gaps`.

#### HAT-BT-021: Stop and close-based exit fills
- **Statement:** A stop fills at the stop price, or at the open when the day opens below the stop; close-based exits (`close_below_sma`, `time_stop`, `partial_after_days`, `trailing_swing_low`) fill at the day's official close.
- **Source:** BL "Stops and exits"; MA "Failure handling" (close vs live price); BL "Defaults that are assumptions" (exit at 15:25 vs official close); PLAN Q10.
- **PR:** 12
- **Verification:** PBT `backtest.exit_fill_proxy`, PBT `backtest.exit_fill_proxy_close`
- **Acceptance:**
  - Property: for every (open, low, stop) on a held day, the stop fill price is min(open, stop) when low <= stop, and the fill is never better than the stop. Harm: gap-down losses are understated.
  - Property: for every close-based exit, the fill price equals that day's close, and the difference from a 15:25 live price is recorded as a known gap, not corrected. Harm: the measured gap between backtest and live exits is hidden.
- **[OWNER-PENDING]** gap-through stop fill: default min(open, stop); docs silent; owner to confirm.

#### HAT-BT-022: Slippage
- **Statement:** Every simulated fill is moved against the trade by a configured slippage in basis points.
- **Source:** PLAN PR 12 (fill and cost model: slippage bps); BL "Defaults that are assumptions" (liquidity cap validated by slippage in shadow mode).
- **PR:** 12
- **Verification:** PBT `costs.slippage_adverse`, PBT `costs.slippage_adverse_net`, PBT `costs.slippage_adverse_bounds`
- **Acceptance:**
  - Property: for every fill price p > 0 and slippage s with 0 <= s < 10000 bps (a configured value outside this range is rejected when the run starts), a buy fills at p × (1 + s/10000) and a sell at p × (1 − s/10000), so net P&L is monotone non-increasing in s. Harm: the backtest assumes perfect fills and overstates edge.
- **Design (PR 12):** prices are stored to 4 places, and rounding never favours the trade: a buy's slipped price is rounded up, a sell's down, and a sell never fills below 0.0001. The slippage is a Decimal checked when the run starts (`cost_bad_slippage` otherwise); the command line takes `--slippage-bps` (default 0) and every run's metrics record it.
- **[OWNER-PENDING]** slippage default: the docs give no number; the value is set by the owner, calibrated from shadow-mode slippage, and every run records the value it used.

#### HAT-BT-023: Statutory costs for delivery trades
- **Statement:** Every simulated buy and sell is charged STT, stamp duty, exchange transaction charges, SEBI turnover fees and GST at delivery (CNC) rates held in a versioned cost-model config, and each run records the cost-model version used.
- **Source:** MA "Phase progression" (Tax and cost model: STT, stamp duty, exchange fees); PLAN PR 12; B24.
- **PR:** 12
- **Verification:** PBT `costs.charges_contract`, PBT `costs.charges_contract_run`, PBT `costs.charges_contract_float_rates`
- **Acceptance:**
  - Property: for every trade (side, qty >= 1, price > 0) and every cost config with non-negative rates, each charge is >= 0, total charges are monotone non-decreasing in traded value, each charge applies only to the sides its config entry names, GST applies only to the components the config marks taxable, and net P&L = gross P&L − total charges, all in Decimal. Harm: costs are missed or double-counted and thin edges look profitable.
  - Property: for every run, the stored metrics name the cost-model version, and rerunning with the same version reproduces the charges exactly. Harm: results cannot be compared after a rate change.
- **[OWNER-PENDING]** rates: the docs give no rates; STT, stamp duty, exchange transaction charges, SEBI fees and GST rates are taken by the owner from current statutory and exchange schedules and stored as a versioned config; nothing is hard-coded.
- **Design (PR 12):** a cost model is a YAML file `app/hat/backtest/cost_models/<version>.yaml` whose `version` equals its name; the default is `cnc-delivery-1` (brokerage 0, STT 0.1% both sides, stamp duty 0.015% on buys, NSE transaction charges 0.00297%, SEBI fees 0.0001%, GST 18% on brokerage, transaction and SEBI charges), its rates the owner-pending defaults above. Rates are strings or integers; a float, boolean, negative or missing rate is refused (`cost_bad_*`), so binary rounding never reaches money. Each component is `rate × value + per_order`, capped by its optional `cap`, rounded half up to the paisa; GST is the rounded sum of the taxable components × `gst_rate`, rounded the same way. The charges are taken on the slipped fill price. A run's metrics store the model's version and the config hash of the file (`cost_model.digest`). The command line takes `--cost-model`.

#### HAT-BT-024: Money in Decimal, quantities integer
- **Statement:** Backtest money math uses Decimal and share quantities are whole numbers rounded down, as in live sizing.
- **Source:** SCH "Summary and conventions" (Money: numeric, never float); BL "Risk management / Position size" (floor); BL "Evaluation rules" (quantities round down).
- **PR:** 12
- **Verification:** PBT `backtest.decimal_money`
- **Acceptance:**
  - Property: for every simulated trade, qty is an integer >= 1 and every money field (price, fees, P&L, equity) is a Decimal; summing trade P&L equals the change in equity exactly. Harm: float drift makes equity and kill-switch drawdowns disagree with the trades.

## §3 Runs, family_key and trial counting

#### HAT-BT-030: Every run increments the trial count of its family
- **Statement:** Every backtest run, whatever its outcome, is counted as one trial of its `family_key` (strategy × universe), and the family's trial count is the number of runs recorded for it.
- **Design (PR 12):** a run is recorded with status `running` before the engine starts, then ends `completed` (metrics and trades written) or `failed` (`metrics = {"error": <code>}`, a fixed code, never exception text). A failed run stays a trial; the command line commits the failed row before it reports the refusal. Refusals before a run is recorded (unknown bot or version, no universe, no policy in force, a bad cost model or slippage, no data) record nothing.
- **Source:** BL "Sleeve lifecycle" (every backtest run is counted as a trial); SCH "Portfolio layer" (`hat_backtest_run` gains `family_key`); PL4.
- **PR:** 12
- **Verification:** PBT `dsr.trial_counting`
- **Acceptance:**
  - Property: for every sequence of runs across families, the trial count of a family equals the number of `hat_backtest_run` rows with that `family_key`, is monotone non-decreasing over time, and runs of other families never change it. Harm: discarded variants are forgotten and the deflated Sharpe ratio under-corrects for luck.

#### HAT-BT-031: Family key is strategy × universe
- **Statement:** `family_key` identifies the (strategy, universe) pair, so all versions and parameter variants of one strategy on one universe share it.
- **Source:** SCH "Portfolio layer" (`family_key` (strategy × universe)).
- **PR:** 12
- **Verification:** PBT `dsr.family_key`
- **Acceptance:**
  - Property: for every two runs, their family keys are equal iff they share strategy_id and universe_id, independent of version, parameters or period. Harm: a strategy author escapes the trial penalty by creating a new version.
- **Design (PR 12):** `family_key` is the text `<strategy_id>:<universe_id>` (both canonical UUIDs); the universe is the sleeve's universe version's universe, so a new universe version stays in the family.

#### HAT-BT-032: Run record and trade output
- **Statement:** Each run stores `strategy_version_id`, `risk_policy_version_id`, `period_start`, `period_end`, `data_snapshot`, `status`, `metrics` and `family_key`, and writes its trades to `backtests/run_id=<id>/trades.parquet`.
- **Design (PR 12):** the trades file is written atomically under `HAT_DATA_ROOT`. On completion `metrics.dsr` stores the deflated Sharpe probability at that moment as `{prob (6 places, text), sr0, reason, trials, variance}`; the lifecycle recomputes it at evaluation time (HAT-BT-060). `metrics.returns` stores the daily-return Sharpe ratio, count, skewness and kurtosis the DSR reads.
- **Source:** SCH "Backtest and ops tables" (`hat_backtest_run`); SCH "Parquet, backtests, ops, indexes".
- **PR:** 3, 12
- **Verification:** CONF `backtest_run_columns`, PBT `backtest.trades_match_metrics`
- **Acceptance:**
  - Property: for every completed run, the trade count and sum of R in `trades.parquet` equal the values in `metrics`. Harm: reported evidence does not match the trades behind it.

## §4 Metrics, expectancy and deflated Sharpe

#### HAT-BT-040: Trade R and expectancy
- **Statement:** A trade's R multiple is realised P&L (after costs) divided by initial risk (entry − initial stop) × entry quantity, and expectancy is the mean R over closed trades.
- **Source:** BL "Your vetoes are measured" (R); SCH `hat_trade_result.r_multiple`; MA "Reuse vs build" (evaluation frameworks: expectancy, drawdown, adherence).
- **PR:** 11, 12
- **Verification:** PBT `backtest.r_and_expectancy`
- **Acceptance:**
  - Property: for every closed trade with entry > initial stop and qty >= 1, r_multiple = realized_pnl / ((entry − initial_stop) × qty), a trade stopped at its initial stop with zero costs has R = −1, and expectancy equals the arithmetic mean of R. Harm: R values are inconsistent between trades, so expectancy, lifecycle gates and veto scores are wrong.

#### HAT-BT-041: Standard metrics reuse the existing module
- **Statement:** Drawdown, Sharpe and trade statistics come from `app/hat/backtest/metrics.py` (HAT's own copy of the legacy `services/backtest_metrics.py`, K1; on polars since PR 12, HAT-DATA-078), extended with R statistics (expectancy, win rate, average win and loss in R, MFE and MAE in R).
- **Source:** MA "Reuse vs build" (evaluation frameworks reused); PLAN §4.5, PR 12.
- **PR:** 12
- **Verification:** PBT `backtest.metrics_bounds`
- **Acceptance:**
  - Property: for every generated equity curve and trade list, max drawdown is in [0, 1], win rate is in [0, 1], and metrics are invariant to the order in which trades closing on the same day are listed. Harm: impossible metric values reach the lifecycle gate.

#### HAT-BT-042: Deflated Sharpe probability
- **Statement:** The deflated Sharpe probability of a run is computed per Bailey & López de Prado (2014): DSR = Φ((SR − SR0) × sqrt(T − 1) / sqrt(1 − γ3 × SR + ((γ4 − 1)/4) × SR²)), with SR0 = sqrt(V) × ((1 − γ) × Φ⁻¹(1 − 1/N) + γ × Φ⁻¹(1 − 1/(N × e))), N the family trial count, V the variance of the family's trial Sharpe ratios, γ the Euler-Mascheroni constant, T the number of returns, γ3 skewness and γ4 kurtosis of returns.
- **Source:** BL "Sleeve lifecycle" (deflated Sharpe ratio corrects for the number of trials; Bailey & López de Prado); PL4.
- **PR:** 12
- **Verification:** PBT `dsr.bounds_and_monotonicity`, PBT `dsr.bounds_and_monotonicity_in_sr`, PBT `dsr.bounds_and_monotonicity_first_trial`, PBT `dsr.bounds_and_monotonicity_undefined`, PBT `dsr.bounds_and_monotonicity_bad_denominator`, CONF `dsr_published_example`
- **Acceptance:**
  - Property: for every return series and family statistics, DSR is in [0, 1], is monotone non-increasing in N with all else fixed, and monotone non-decreasing in SR with all else fixed wherever the formula's slope in SR, 1 − γ3 (SR + SR0)/2 + ((γ4 − 1)/4) SR × SR0, is non-negative. Harm: more trials could make a strategy look more significant, the opposite of the correction.
  - Property: with N = 1 (or V = 0), SR0 = 0 and DSR equals the probabilistic Sharpe ratio against a zero benchmark. Harm: the first run of a family is mis-scored.
  - Property: the implementation reproduces the worked example published by Bailey & López de Prado to within 1e-6. Harm: a formula slip goes unnoticed because every other check is relative.
- **Finding (PR 12):** DSR is not monotone in SR everywhere. With a heavy positive skew the denominator grows faster than the numerator and a higher SR can score lower; the slope's sign is the expression above. The property holds on that domain only; the formula is the paper's and is not changed.
- **Design (PR 12):** the published example (annualised SR 2.5, T = 1250 daily returns, γ3 = −3, γ4 = 10, N = 100, annualised V = 0.5) gives SR0 = 0.1132 and DSR = 0.9004 at the paper's printed 4 places; the 1e-6 tolerance is against the same formula computed independently (`math.erf` and a bisection inverse), since the paper prints 4 places. SR, T, γ3 and γ4 are of the run's daily equity returns (population moments, γ4 not excess); V is the sample variance of the family's completed runs' per-period Sharpe ratios, the run's own included. A radicand at or below 1e-12 is a non-positive denominator (float noise around 0 would otherwise give DSR = 1).
  - Property: for every return series with fewer than 2 returns, zero variance, or a non-positive denominator under the square root, the run stores DSR = 0 with reason `dsr_undefined` (never NaN), so the lifecycle gate fails closed. Harm: a NaN compares false everywhere and a gate written as "not below 0.95" lets the sleeve through.
- **[OWNER-PENDING]** N = 1 convention: default SR0 = 0; the paper's formula is undefined at N = 1; owner to confirm. Undefined-DSR convention (DSR = 0, fail closed): default; owner to confirm.

## §5 Replay verification

#### HAT-BT-050: Backtests are deterministic
- **Statement:** A run with the same data snapshot, strategy version, risk-policy version, cost-model version and parameters produces byte-identical `trades.parquet` and metrics.
- **Source:** MA "Decisions register" (T16 replay tests, superseded in method by OWN-FV); BL "Summary" (every trade can be replayed and explained).
- **PR:** 12
- **Verification:** PBT `backtest.deterministic`
- **Acceptance:**
  - Property: for every generated fixture market and config, two runs produce byte-identical trade files and equal metrics JSON, independent of dict ordering, thread scheduling and wall-clock time. Harm: evidence cannot be reproduced, so a promotion decision cannot be audited.

#### HAT-BT-051: Golden replay of a fixed fixture market
- **Statement:** A committed fixture market run through S1 and S2 reproduces committed golden metrics JSON, in the style of the existing `tests/fixtures/*_golden.json`.
- **Source:** PLAN PR 12 (replay test harness); OWN-FV (golden replay allowed as a determinism property).
- **PR:** 12
- **Verification:** CONF `backtest_golden_replay`
- **Acceptance:**
  - Property: the metrics produced from the committed fixture equal the committed golden file exactly, and any intended change updates both in the same commit. Harm: a silent change in engine behaviour shifts every sleeve's evidence.

## §6 Lifecycle gate evidence

#### HAT-BT-060: Evidence hook for the Backtested state
- **Statement:** The backtest supplies the sleeve lifecycle (05 §6) with each run's trade count, deflated Sharpe probability and family trial count; the Research → Backtested guard passes only when DSR >= 0.95 and trades >= 200.
- **Source:** BL "Sleeve lifecycle"; PL4; PLAN PR 12 (lifecycle evidence hook).
- **PR:** 9, 12
- **Verification:** PBT `lifecycle.backtest_gate`, PBT `dsr.evidence_current_trials`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- **Acceptance:**
  - Property: for every evidence record, the guard result is (dsr_prob >= 0.95 and trades >= 200), and the evidence always carries the family trial count current at evaluation time. Harm: a sleeve passes the gate on stale trial counts.
- **[A]** DSR >= 0.95 and >= 200 trades: defaults; validated by owner review of the first sleeves' evidence.
- **Design (PR 12):** `app.hat.backtest.runs.backtest_evidence` is the lifecycle's `BacktestEvidence` source (05 §6): the newest completed run of the sleeve's live version on its family, with the DSR recomputed at the family's trial count and Sharpe variance now (failed runs and other versions of the strategy count), its trade count and expectancy, as `BacktestFacts`. With no such run there is no evidence and Research → Backtested stays closed. The same runs feed the risk-increase gate (06, HAT-GOV-091): `runs.gate_evidence` builds `gate_evidence` from the user's closed trades against the trade-weighted mean expectancy of the sleeves' newest completed runs, discounted by the lifecycle's backtest discount (`DEFAULT_DISCOUNT` 0.58, HAT-PORT-065, owner-pending); with no completed run it gives none and the increase is refused.

#### HAT-BT-061: Strategy versions record their backtest
- **Statement:** A strategy version leaves `draft` for `backtested` only with a `backtest_run_id` of a completed run of that version, and status `live` requires both `backtest_run_id` and `shadow_started_at`.
- **Source:** SCH "Governance enforced in the database" (no promotion without evidence); BL "Governance" (draft → automatic backtest → shadow → promote); B5.
- **PR:** 2, 12
- **Verification:** DB `governance.live_requires_evidence`, PBT `backtest.version_status_requires_run`
- **Acceptance:**
  - Property: for every version with status other than `draft`, `backtest_run_id` references a completed run whose `strategy_version_id` is that version. Harm: a version is promoted on another version's evidence.
  - Property: for every row with status `live`, the database rejects it unless `backtest_run_id` and `shadow_started_at` are not null. Harm: an untested strategy trades.

## §7 Command line and known gaps

`python -m app.hat.engine.backtest --bot <id> --from YYYY-MM-DD --to YYYY-MM-DD [--version <id>] [--slippage-bps N] [--cost-model NAME]` backtests a sleeve's live strategy version (or `--version`, another version of the same strategy) on the sleeve's universe, under the risk-policy version in force now, over the Parquet store under `HAT_DATA_ROOT` (the adjusted `bars_daily` view, `features_daily`, the regime index closes and point-in-time membership from the database). It records the run as a trial, writes the trades file and prints the run id, status, trade count and DSR; a refusal prints a fixed code and exits non-zero.

Every run's `metrics.known_gaps` lists what the daily-bar loop does not model, in this order: `trigger_is_level_break_only`, `episodic_pivot_not_backtested`, `close_exits_at_official_close`, `kill_switches_not_simulated`. The gaps are measured in shadow mode (PLAN Q4), not corrected.
