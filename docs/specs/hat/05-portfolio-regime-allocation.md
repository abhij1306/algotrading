# 05: Portfolio, Regime and Allocation

Scope: sleeves and the risk cluster, versioned universes, the MVP regime engine (v1 rules), the health multiplier, the nightly allocator, the sleeve lifecycle, and the Phase 2 regime model (v2, specified, not built). Requirement format and verification methods: see README.md and 11-verification.md.

The engine is strategy-agnostic: it runs **sleeves** (one strategy version on one universe version), and three dials set each sleeve's weight every night. Regime scales weights continuously; only live evidence promotes or retires a sleeve. The allocator is the only place regime touches money, and it can only scale a sleeve down from its base weight (BL "Portfolio layer").

## §1 Sleeves and risk cluster

| Sleeve | Strategy | Universe | Status |
| --- | --- | --- | --- |
| S1 | Minervini-style (`stage2-contraction-breakout`) | MVP universe (manual) | MVP |
| S2 | Qullamaggie-style (`momentum-breakout-ep`) | MVP universe | MVP |
| S3+ | Any future strategy | Any universe version | Added later |

#### HAT-PORT-010: A sleeve is a bot pinning one strategy version and one universe version
- **Statement:** `hat_bot` is the sleeve: it pins one live strategy version and one universe version and carries `lifecycle_state` and `risk_cluster`; adding a sleeve is a new row, not new code.
- **Source:** BL "Portfolio layer"; SCH "Portfolio layer (7 tables)"; PL1.
- **PR:** 2, 9
- **Verification:** CONF `bot_sleeve_columns`, PBT `allocator.sleeve_count_agnostic`
- **Acceptance:**
  - Property: for every number of sleeves n >= 1 (generated rows), the allocator and scan run without code changes and produce exactly one allocation row per sleeve per trade date. Harm: adding a third sleeve would need engine changes, defeating PL1.
  - Property: the `hat_bot` model has `universe_version_id`, `lifecycle_state` (research / backtested / shadow / live / probation / retired) and `risk_cluster`, and no risk-number columns. Harm: risk numbers leak into the sleeve row and bypass the risk policy.

#### HAT-PORT-011: S1 and S2 form one risk cluster with a combined cap
- **Statement:** S1 and S2 share one `risk_cluster`, and the sum of the final weights of the sleeves in a cluster never exceeds the cluster cap in the risk policy, until an uncorrelated sleeve exists.
- **Source:** BL "Portfolio layer / Sleeves"; PL1.
- **PR:** 9
- **Verification:** PBT `allocator.cluster_cap`
- **Acceptance:**
  - Property: for every set of base weights, multipliers and cluster cap c, after allocation the sum of `final_weight` over sleeves with the same `risk_cluster` is <= c. Harm: two strategies that lose money together get a combined weight larger than the owner allowed.
- **[OWNER-PENDING]** cluster cap and base weights: default cluster cap 100%, base weights S1 50% and S2 50% (PLAN Q3); owner must set real numbers before confirm/auto.

## §2 Universes

A universe is a versioned, point-in-time list of stocks; each sleeve pins one universe version. MVP universes are hand-picked (BL "Stock universes"; PL5).

|  | MVP | Phase 2 |
| --- | --- | --- |
| Definition | Manual list (e.g. NIFTY 500 + include/exclude) | Screener: liquidity, listing age and fundamental criteria |
| Evaluation | When the owner edits it | Monthly and after each results season |
| Membership changes | Owner edits | Promote and retire per rules |
| Versioning | Each edit = new version | Each rule change = new version through backtest → shadow |

#### HAT-PORT-020: Universe versions are immutable with one live version
- **Statement:** Each universe edit creates a new `hat_universe_version`; published versions never change, and at most one version per universe is live.
- **Source:** BL "Stock universes"; SCH "Portfolio layer" (`hat_universe_version`: same immutability and one-live-version rules as strategy versions); PL5.
- **PR:** 2, 9
- **Verification:** DB `governance.universe_version_immutable`, DB `governance.one_live_version`
- **Acceptance:**
  - Property: for every non-draft universe version, an UPDATE of `definition` is rejected by the database. Harm: a backtest's universe changes after the fact, so its evidence no longer matches what runs live.
  - Property: for every attempt to set a second version of the same universe to `live`, the database rejects it. Harm: two universes compete and the sleeve trades an unapproved list.
- A seeded draft records its member list and changes no membership interval; publishing it applies the list from its `as_of` date and retires the previously live version (PBT `universe.seeded_version_is_seen_only_once_published`).

#### HAT-PORT-021: Membership is point-in-time
- **Statement:** The member set of a universe on date d is exactly the instruments whose `hat_universe_member` row has effective_from <= d and (effective_to is null or effective_to > d), delisted instruments included for past dates.
- **Source:** BL "Stock universes"; SCH "Portfolio layer" (`hat_universe_member`); DS "Historical data build" (survivorship-free).
- **PR:** 9
- **Verification:** PBT `universe.point_in_time_members`
- **Acceptance:**
  - Property: for every generated membership history and date d, the member query returns exactly the rows active at d, and adding rows with effective_from > d never changes the result for d. Harm: backtests use today's survivors and overstate returns.

#### HAT-PORT-022: MVP universe content
- **Statement:** The MVP universe is a manual version seeded from NIFTY 500 point-in-time constituents plus owner include/exclude lists, filtered further by each bot's universe criteria.
- **Source:** BL "Stock universes" (MVP column); PL5; PLAN Q5.
- **PR:** 9
- **Verification:** PBT `universe.include_exclude`
- **Acceptance:**
  - Property: for every constituent set C, include list I and exclude list X, the version's members at its effective date equal (C ∪ I) \ X. Harm: an excluded stock gets traded, or an included one is silently dropped.
- **[OWNER-PENDING]** MVP universe: default NIFTY 500 point-in-time constituents via the existing universe service, include and exclude lists empty (PLAN Q5); owner to confirm.

#### HAT-PORT-023: Leaving the universe stops entries but never force-sells
- **Statement:** When a stock leaves a sleeve's universe, the sleeve makes no new entries in it and its open positions run to their own exits.
- **Source:** BL "Stock universes / Phase 2 promotion and retirement rules" (Leaving the universe).
- **PR:** 9, 11
- **Verification:** PBT `universe.leave_no_force_sell`, PBT `scan.no_row_for_removed`
- **Acceptance:**
  - Property: for every membership change and open position in the removed stock, no exit order is generated by the change itself and no watchlist row or signal for that stock is produced after the effective date. Harm: a universe edit dumps positions at a bad price, or keeps buying a stock the owner removed.

## §3 Regime engine v1 (MVP, rules only)

Three inputs classify the market into four states each night; each sleeve has a multiplier per state (BL "Regime engine v1").

| Input | Measure | Source |
| --- | --- | --- |
| Trend | NIFTY 500 close vs its 200-day and 50-day SMA | `index_daily` |
| Volatility | India VIX percentile over the trailing 252 days | NSE MCP `get_index_snapshot` |
| Breadth | % of universe members above their 50-day SMA; advance/decline ratio | Own bars + `get_market_breadth` |

| State | Rule (*assumption*) | Momentum sleeve multiplier (*assumption*) |
| --- | --- | --- |
| Bull, quiet | Close > 200 SMA and > 50 SMA; VIX percentile < 67; breadth ≥ 50% | 1.0 |
| Bull, volatile | Close > 200 SMA; VIX percentile ≥ 67 or breadth 40-50% | 0.75 |
| Correction | Close > 200 SMA but < 50 SMA, or breadth < 40% | 0.5 |
| Bear | Close < 200 SMA | 0.25 |

#### HAT-PORT-030: State changes only after 3 consecutive days
- **Statement:** The confirmed regime state changes to a new state only on the 3rd consecutive trading day on which the raw classification equals that new state.
- **Source:** BL "Regime engine v1" (State changes); SCH `hat_regime_state.confirmed`; PL3.
- **PR:** 9
- **Verification:** TLA `RegimeConfirmation!ThreeDayRule`, PBT `regime.three_day_confirmation`
- **Acceptance:**
  - Property: in every sequence of raw daily states, the confirmed state at day t differs from the confirmed state at day t−1 only if the raw state equals the new confirmed state on days t−2, t−1 and t. Harm: one wild day moves capital in or out of the market.
  - Property: in every sequence, whenever the raw state stays at some value s for 3 consecutive days, the confirmed state is s at the end of the 3rd day (liveness). Harm: the engine never acknowledges a real change in market conditions.
- **[A]** confirmation length: default 3 days; validated by backtest of sleeve drawdowns with 1-5 days.
- **[OWNER-PENDING]** first day: with no earlier stored day there is nothing to confirm against, so the first raw state is the confirmed state (bootstrap); docs silent; owner to confirm.

#### HAT-PORT-031: Regime inputs are computed exactly as defined
- **Statement:** Trend uses NIFTY 500 close vs its SMA200 and SMA50; volatility is the percentile rank of today's India VIX among its trailing 252 daily values; breadth is the percentage of universe members with close above their SMA50, plus the advance/decline ratio.
- **Source:** BL "Regime engine v1" (inputs table); DS "Source map" (index levels, India VIX, market breadth).
- **PR:** 6, 9
- **Verification:** PBT `regime.inputs`
- **Acceptance:**
  - Property: for every generated index, VIX and member bar history, the VIX percentile is in [0, 100] and monotone non-decreasing in today's VIX, breadth is in [0, 100], and every input on date d uses only data dated <= d. Harm: the regime reads the future in backtests or mis-scales risk live.
- **[OWNER-PENDING]** percentile rank definition: default = 100 × (count of the trailing 252 values, today's included, that are strictly below today's) / 252, so the window is today and the 251 days before it; docs silent; owner to confirm.
- The advance/decline ratio is stored with the inputs when the ingest provides it (PR 7) and does not enter the v1 classification; a day without index, VIX or breadth values, or with too little history for the SMAs and the VIX window, fails the `regime` job with a stable code and stores nothing.

#### HAT-PORT-032: Classification is total and deterministic
- **Statement:** Every combination of inputs maps to exactly one of the four states, using the table above, with the more defensive state winning where rows overlap.
- **Source:** BL "Regime engine v1" (state table); PL3.
- **PR:** 9
- **Verification:** PBT `regime.classification_total`
- **Acceptance:**
  - Property: for every (close, sma50, sma200, vix_pct, breadth), the classifier returns exactly one state; it returns Bear whenever close <= SMA200; Correction whenever close > SMA200 and (close <= SMA50 or breadth < 40); Bull volatile whenever close > SMA200 and none of the Correction conditions hold and (vix_pct >= 67 or 40 <= breadth < 50); Bull quiet otherwise only when close > SMA200, close > SMA50, vix_pct < 67 and breadth >= 50. Harm: overlapping rows give a different state depending on code order, and risk scaling becomes arbitrary.
  - Property: the classification is monotone in defensiveness: lowering close, raising VIX percentile or lowering breadth never moves the state to a less defensive one. Harm: worse conditions could raise exposure.
- **[A]** state rules (200/50 SMA, VIX percentile 67, breadth 40/50%): defaults as in the table; validated by backtest of each sleeve's expectancy per state.
- **[OWNER-PENDING]** overlap and boundary handling: default precedence Bear > Correction > Bull volatile > Bull quiet; close exactly equal to SMA200 counts as not above (Bear) and close exactly equal to SMA50 counts as not above (Correction), so the four rows cover every input; docs silent; owner to confirm.

#### HAT-PORT-033: Momentum sleeve multipliers per state
- **Statement:** The regime multiplier for a momentum sleeve is 1.0 (Bull quiet), 0.75 (Bull volatile), 0.5 (Correction) or 0.25 (Bear) of the confirmed state, until replaced by backtested values shrunk toward 1.0.
- **Source:** BL "Regime engine v1" (multiplier column; "Multipliers are placeholders"); PL2.
- **PR:** 9
- **Verification:** PBT `regime.multiplier_table`
- **Acceptance:**
  - Property: for every confirmed state and the default table, the regime multiplier equals the table value, and for every replacement table derived from backtests each value lies between the backtested estimate and 1.0 and within the policy bounds [0.25, 1.0]. Harm: a thin backtest sample produces an extreme multiplier.
- **[A]** multipliers 1.0 / 0.75 / 0.5 / 0.25: placeholders; validated by each sleeve's backtested expectancy per state, shrunk toward 1.0, before go-live.

#### HAT-PORT-034: Regime output is stored daily and never touches lifecycle
- **Statement:** Each trade date stores one `hat_regime_state` row per (market, model version) with state, inputs, probabilities (null for v1) and `confirmed`, plus each sleeve's multiplier; regime never changes a sleeve's lifecycle state.
- **Source:** BL "Regime engine v1" (Output stored daily); BL "Sleeve lifecycle"; SCH `hat_regime_state`, `hat_regime_model_version`; PL2, PL3.
- **PR:** 9
- **Verification:** DB `portfolio.unique_keys`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle`
- **Acceptance:**
  - Property: for every (market_id, model_version_id, trade_date), at most one `hat_regime_state` row exists. Harm: two regime readings for one day give two different allocations.
  - Property: in every reachable state of the lifecycle model, no regime action changes `lifecycle_state`. Harm: a bear market retires a sound sleeve, or a bull market promotes an unproven one.
  - Property: a `hat_regime_model_version` row of kind `rules`, version 1, exists and is referenced by every v1 regime state. Harm: regime outputs cannot be traced to the rules that produced them.
- **[OWNER-PENDING]** reading of `state` and `confirmed`: default = `state` is the day's raw classification, `confirmed` is true when it equals the confirmed state, and the confirmed state itself and the run length are kept in `inputs` (`confirmed_state`, `raw_state`, `run`); the allocator reads the confirmed state; owner to confirm.

## §4 Health multiplier (per sleeve)

| Sleeve drawdown from its peak | Multiplier | State |
| --- | --- | --- |
| Under 8% | 1.0 | Live |
| 8% to 12% | 0.5 | Live |
| 12% or more | 0 | Probation |

#### HAT-PORT-040: Health multiplier from sleeve drawdown bands
- **Statement:** With drawdown dd = 1 − equity / equity_peak of the sleeve, the health multiplier is 1.0 if dd < 8%, 0.5 if 8% <= dd < 12%, and 0 with a move to Probation if dd >= 12%.
- **Source:** BL "Health multiplier (per sleeve)"; SCH `hat_daily_bot_snapshot` (equity, equity_peak); PL2, PL4.
- **PR:** 9
- **Verification:** PBT `health.bands`, TLA `SleeveLifecycle!ProbationOnHealthZero`
- **Acceptance:**
  - Property: for every (equity >= 0, equity_peak > 0), the multiplier equals the band value above and is monotone non-increasing in dd. Harm: a sleeve in a deep drawdown keeps full capital.
  - Property: in every reachable state, a sleeve whose health multiplier is 0 is in Probation by the next allocation. Harm: a broken sleeve keeps trading live.
- **[A]** bands 8% / 12%: defaults modelled on multi-manager pods; validated by backtest of sleeve drawdown paths (PR 12) and live review.

## §5 Allocator

```
w_s = b_s * m_regime_s * m_health_s,   m in [0.25, 1.0],   cash = 1 - sum_s(w_s)
```

#### HAT-PORT-050: Weight formula and policy bounds
- **Statement:** Each live sleeve's final weight is base weight × regime multiplier × health multiplier, where multipliers are clamped to the bounds in the risk policy (default [0.25, 1.0]); a sleeve not in Live has weight 0.
- **Source:** BL "Portfolio layer / Allocation"; SCH (multiplier bounds live in `hat_risk_policy_version.policy`); PL2.
- **PR:** 9
- **Verification:** PBT `allocator.formula_and_bounds`
- **Acceptance:**
  - Property: for every base weight b in [0,1], regime and health inputs, and policy bounds [lo, hi] with hi <= 1.0 in the default policy, the stored `final_weight` equals b × clamp(m_regime) × m_health for live sleeves and 0 otherwise, and 0 <= final_weight <= b. Harm: the allocator gives a sleeve more weight than its base.
  - Property: for every allocation, the stored `regime_mult` and `health_mult` are the values used, so `final_weight = base_weight × regime_mult × health_mult` holds row by row. Harm: the audit trail cannot explain a weight.
- Where a cap binds (HAT-PORT-011 and HAT-PORT-055), `final_weight` is the product scaled down in proportion and rounded down, so it is below base × regime_mult × health_mult; the row identity holds exactly when no cap binds. A sleeve returning from health 0 (a previous health multiplier of 0) restarts at its target, not at 0 plus a smoothing step.
- **[OWNER-PENDING]** health 0 vs lower bound 0.25: default reading is that bounds apply to the regime multiplier and to the health values 1.0 and 0.5, while health 0 moves the sleeve to Probation whose weight is 0 (BL lifecycle table); owner to confirm.

#### HAT-PORT-051: Multipliers above 1 only through a policy increase
- **Statement:** The upper multiplier bound can exceed 1.0 only through a risk-policy increase, which is subject to the 7-day cooling-off.
- **Source:** BL "Allocation" (Bounds 0.25-1.0 by default); SCH (bounds inside `hat_risk_policy_version.policy`); PL2.
- **PR:** 9, 10
- **Verification:** PBT `allocator.bounds_from_policy`, DB `governance.risk_increase_cooling_off`
- **Acceptance:**
  - Property: for every allocation, the bounds used are read from the risk-policy version effective at the allocation's trade date, and no code path supplies a bound from elsewhere. Harm: a sleeve is scaled above its base without the cooling-off gate.

#### HAT-PORT-052: Multipliers scale both capital and risk per trade
- **Statement:** The allocation row's `capital` = final_weight × total equity and `risk_pct` = policy risk per trade × regime_mult × health_mult, and the risk manager sizes new entries with these values.
- **Source:** BL "Allocation" (Multipliers scale both the sleeve's capital cap and its risk per trade); SCH `hat_allocation_daily`; B13-B15.
- **PR:** 9, 10
- **Verification:** PBT `allocator.capital_and_risk`
- **Acceptance:**
  - Property: for every allocation row, capital = final_weight × total equity and risk_pct = r × regime_mult × health_mult with r from the policy (default 0.25%), and risk_pct <= r whenever the multiplier bounds are <= 1. Harm: risk per trade stays at full size in a bear market.
- **[OWNER-PENDING]** total equity source: default = broker funds read at session start (PLAN Q3); owner must set real numbers before confirm/auto.

#### HAT-PORT-053: Recomputed nightly, applied to new entries only
- **Statement:** Allocation is recomputed nightly after the regime job and applies only to new entries; open positions keep their stops and exits.
- **Source:** BL "Allocation" (Recomputed nightly); MA "Daily cycle" step 7.
- **PR:** 9, 17
- **Verification:** TLA `JobScheduler!AllocateAfterRegime`, PBT `allocator.no_effect_on_open_positions`
- **Acceptance:**
  - Property: in every run of the nightly job graph, `allocate` for trade date d starts only after `regime` for d succeeded. Harm: weights use yesterday's regime.
  - Property: for every allocation change, no stop, exit order or position quantity of an open position changes as a result. Harm: a weight cut forces sales the strategy did not call for.

#### HAT-PORT-054: Smoothing caps the daily multiplier move at 0.25
- **Statement:** Each multiplier moves by at most 0.25 from its previous day's value, except that a sleeve entering Probation (health multiplier 0) gets weight 0 at once: that is a lifecycle change (§6), not a smoothed multiplier move.
- **Source:** BL "Allocation" (Smoothing); PL2.
- **PR:** 9
- **Verification:** PBT `allocator.smoothing`
- **Acceptance:**
  - Property: for every sequence of target multipliers of a sleeve that stays out of Probation, |m_t − m_{t−1}| <= 0.25 for each stored multiplier, and m_t reaches the target within ceil(|target − m_{t−1}| / 0.25) days if the target stays fixed. Harm: capital swings fully in one night on a regime flip.
  - Property: for every day on which a sleeve's health multiplier target is 0, its final weight that day is 0 regardless of the previous day's value. Harm: a sleeve past its 12% drawdown keeps getting new capital for several days while smoothing walks it down.
- **[A]** smoothing step: default 0.25 per day; validated by backtest of allocation turnover.

#### HAT-PORT-055: Unallocated weight stays cash
- **Statement:** The sum of final weights never exceeds 1, and weight removed from one sleeve is never redistributed to another.
- **Source:** BL "Allocation" (Unallocated weight stays cash); PL2.
- **PR:** 9
- **Verification:** PBT `allocator.cash_remainder`
- **Acceptance:**
  - Property: for every allocation, sum of final_weight <= 1, cash = 1 − sum >= 0, and for every sleeve final_weight <= base_weight regardless of other sleeves' multipliers. Harm: a cut on one sleeve silently raises risk on another.

#### HAT-PORT-056: One audited allocation row per sleeve and day
- **Statement:** Each (trade_date, bot_id) has exactly one `hat_allocation_daily` row holding base_weight, regime_mult, health_mult, final_weight, capital and risk_pct.
- **Source:** SCH `hat_allocation_daily` (Unique (trade_date, bot_id); the audit trail for every weight).
- **PR:** 2, 9
- **Verification:** DB `portfolio.unique_keys`, TLA `JobScheduler!AtMostOnceSuccess`
- **Acceptance:**
  - Property: for every rerun of the allocate job on the same trade date, the table holds one row per sleeve with the values of the first successful run. Harm: two weights for one day and the risk manager picks either.

## §6 Sleeve lifecycle

| State | Entry condition | Weight |
| --- | --- | --- |
| Research | Draft strategy × universe pair | 0 |
| Backtested | Backtest passes: deflated Sharpe probability ≥ 0.95 and ≥ 200 backtest trades (*assumptions*); every backtest run is counted as a trial | 0 |
| Shadow | Passed backtest; 20 trading days of paper trading | 0 (virtual) |
| Live | Shadow passed | Base × multipliers |
| Probation | Health multiplier hits 0, or live expectancy's 90% confidence interval sits below zero after 50 trades | 0; keeps trading in shadow |
| Retired | Still failing after 3 months or 50 more shadow trades in probation | 0; re-entry only through the full gate |

#### HAT-PORT-060: Lifecycle transitions follow the table and its guards
- **Statement:** A sleeve moves Research → Backtested → Shadow → Live, Live → Probation, Probation → Live (owner promotion only, see the pending default below), Probation → Retired, and Retired → Research only, each transition guarded by its entry condition and recorded as an event.
- **Source:** BL "Sleeve lifecycle"; SCH `hat_bot.lifecycle_state`; PL4.
- **PR:** 9, 12
- **Verification:** TLA `SleeveLifecycle!AllowedTransitions`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- **Acceptance:**
  - Property: in every reachable state, every lifecycle change is one of the allowed edges and is accompanied by an event in the same transaction. Harm: a sleeve skips the backtest or shadow stage and trades unproven.
  - Property: in every reachable state, a Live sleeve passed through Backtested and Shadow since it last left Research. Harm: a retired sleeve returns to money without the full gate.
- Re-entry to Live from Probation restarts the health multiplier at its target (no ramp), because the sleeve's previous multiplier was 0.
- **[OWNER-PENDING]** Probation → Live recovery: docs do not define it; default = not automatic, only by owner promotion after the probation condition no longer holds, recorded as an event; owner to confirm.

#### HAT-PORT-061: Backtested requires DSR probability ≥ 0.95 and ≥ 200 trades
- **Statement:** A sleeve enters Backtested only when a backtest run of its family has deflated Sharpe probability >= 0.95 and >= 200 trades, with every run of the family counted as a trial.
- **Source:** BL "Sleeve lifecycle"; SCH `hat_backtest_run.family_key`; PL4.
- **PR:** 9, 12
- **Verification:** PBT `lifecycle.backtest_gate`, TLA `SleeveLifecycle!AllowedTransitions`
- **Acceptance:**
  - Property: for every evidence record (dsr_prob, trades, trial count), the Research → Backtested guard is true iff dsr_prob >= 0.95 and trades >= 200, where dsr_prob was computed with the family's full trial count (09 §3-§4). Harm: a lucky variant from many tries is promoted.
- **[A]** DSR >= 0.95, >= 200 trades: defaults; validated by owner review of the first sleeves' evidence.

#### HAT-PORT-062: Shadow lasts 20 trading days before Live
- **Statement:** A sleeve moves Shadow → Live only after at least 20 trading days in Shadow, counted on `hat_trading_calendar`.
- **Source:** BL "Sleeve lifecycle"; BL "Governance" (shadow for 20 trading days); PL4.
- **PR:** 9
- **Verification:** TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.shadow_days`
- **Acceptance:**
  - Property: for every calendar and shadow start date, the Shadow → Live guard is false while fewer than 20 trading days (holidays and weekends excluded) have elapsed. Harm: a sleeve goes live on a few lucky days.
- **[A]** shadow length: default 20 trading days; validated by live review.

#### HAT-PORT-063: Probation trigger and effect
- **Statement:** A Live sleeve enters Probation when its health multiplier hits 0 or, after at least 50 live trades, the upper bound of the 90% confidence interval of its live expectancy (mean R) is below zero; in Probation its weight is 0 and it keeps trading in shadow.
- **Source:** BL "Sleeve lifecycle"; PL4.
- **PR:** 9, 18
- **Verification:** PBT `lifecycle.probation_trigger`, TLA `SleeveLifecycle!ProbationOnHealthZero`, PBT `router.non_live_sleeve_routes_as_shadow`
- **Acceptance:**
  - Property: for every sequence of live trade R values with n >= 50, the trigger fires iff the 90% CI upper bound of mean R is < 0, and it never fires on the expectancy rule with n < 50. Harm: a sleeve with negative edge keeps real capital, or a sound sleeve is benched on noise.
  - Property: in every reachable state, a Probation sleeve has final_weight 0 and its signals route as shadow. Harm: a failing sleeve still places real orders. The router reads the effective mode through `execution/modes.py:bot_mode`, which answers shadow for every lifecycle state `lifecycle.routes_as_shadow` names (PR 18).
- **[OWNER-PENDING]** CI method: default two-sided Student-t interval on mean R; docs silent; owner to confirm.
- The health multiplier an allocation reads is that of the sleeve's latest snapshot dated before the trade date (a new sleeve with none has the full value); a sleeve whose snapshot cannot be read fails the `allocate` job with a stable code instead of being given a default.

#### HAT-PORT-064: Retirement and re-entry
- **Statement:** A Probation sleeve is Retired when it is still failing after 3 months or after 50 more shadow trades in Probation, and a Retired sleeve re-enters only through Research and the full gate.
- **Source:** BL "Sleeve lifecycle"; PL4.
- **PR:** 9
- **Verification:** TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.retire_gate`
- **Acceptance:**
  - Property: in every reachable state, Retired has the single outgoing edge to Research, and Probation → Retired happens only after 3 calendar months or 50 shadow trades in Probation with the failure condition still true. Harm: a retired sleeve returns to live money without fresh evidence.
- **[OWNER-PENDING]** failure condition: a Probation sleeve is still failing while it has no shadow trade yet, no backtest expectancy is known, or the mean R of its shadow trades since it entered Probation is below the discounted benchmark of HAT-PORT-065; docs silent; owner to confirm.

#### HAT-PORT-065: Live results are compared against a discounted backtest
- **Statement:** The lifecycle evidence compares live expectancy against the backtest expectancy reduced by a configured decay discount, not the raw backtest.
- **Source:** BL "Sleeve lifecycle" (Why expect decay: 26% lower out of sample, 58% lower after publication).
- **PR:** 9, 12
- **Verification:** PBT `lifecycle.discounted_benchmark`
- **Acceptance:**
  - Property: for every backtest expectancy E_bt and discount δ in [0, 1], the benchmark used is E_bt × (1 − δ) and it never exceeds E_bt when E_bt >= 0. Harm: normal post-publication decay is read as failure, or real failure is hidden.
- **[OWNER-PENDING]** discount δ: docs give the 26% / 58% figures but no setting; default δ = 0.58 (both MVP methods are widely published); owner to confirm.
- **Deviation (PR 12):** δ stays the configured `DEFAULT_DISCOUNT` (0.58) rather than a value calibrated from HAT's own backtest-versus-shadow results: there are no shadow trades to calibrate against yet. The backtest supplies the expectancy it discounts (09 HAT-BT-060), and the risk-increase gate (06 HAT-GOV-091) uses the same δ. Calibration waits for the first sleeves' shadow evidence.

#### HAT-PORT-066: Lifecycle, regime and allocation changes are events
- **Statement:** Every change of a sleeve's lifecycle state, every stored regime row and every allocation row appends its event (`sleeve.lifecycle_changed`, `regime.state_recorded`, `allocation.computed`) in the transaction of the change, and only one function writes `hat_bot.lifecycle_state`, so no change can skip its guard or its event.
- **Source:** SCH §6 (event catalogue); BL "Sleeve lifecycle"; HAT-SCH-062.
- **PR:** 9
- **Verification:** CONF `lifecycle_single_writer`, PBT `lifecycle.transition_records_its_event`
- **Acceptance:**
  - Property: for every (current state, target, evidence), a transition either changes the state and appends exactly one `sleeve.lifecycle_changed` event naming from and to, or changes nothing and appends nothing. Harm: the audit trail disagrees with the state.
  - Property: no code outside the one transition function assigns `lifecycle_state`. Harm: a second writer skips the lifecycle guard.

## §7 Regime model v2 (Phase 2 spec, not built in MVP)

A statistical model replaces the hand-set thresholds and outputs probabilities instead of one label. It must beat v1 out of sample before it touches money (BL "Regime model v2"). Nothing in this section is built in the MVP; the requirements fix the contract the MVP schema already supports (`hat_regime_model_version.kind` in rules / hmm / gmm, `hat_regime_state.probabilities`).

#### HAT-PORT-070: Model family and features
- **Statement:** v2 is a hidden Markov model with 3-4 persistent states, with a Gaussian mixture as baseline, using features known at close t and applied from t+1: index returns over 1/5/21 days, 21-day realised volatility, India VIX level and 21-day change, breadth above 50 SMA, advance/decline, 52-week highs minus lows, distance from 200 SMA.
- **Source:** BL "Regime model v2"; PL3.
- **PR:** P2
- **Verification:** PBT `regime_v2.no_lookahead`
- **Acceptance:**
  - Property: for every date t, the state probabilities used on t+1 are computed from features dated <= t only. Harm: backtests of v2 read the future and overstate its value.

#### HAT-PORT-071: Training, refit and relabelling
- **Statement:** v2 trains on an expanding window from 2010, refits quarterly, and relabels states after each refit by mean return and volatility so "bear" stays "bear".
- **Source:** BL "Regime model v2".
- **PR:** P2
- **Verification:** PBT `regime_v2.relabel_stable`
- **Acceptance:**
  - Property: for every refit, the state labelled Bear has the lowest mean return among states, and labels are a deterministic function of the fitted state statistics. Harm: a refit swaps labels and bull-market multipliers apply in a bear market.

#### HAT-PORT-072: Output, bounds and smoothing
- **Statement:** v2 outputs a probability per state; the sleeve multiplier is the sum over states of probability × the sleeve's state multiplier, with the same bounds and smoothing as v1.
- **Source:** BL "Regime model v2".
- **PR:** P2
- **Verification:** PBT `regime_v2.multiplier_convex`
- **Acceptance:**
  - Property: for every probability vector (non-negative, summing to 1), the multiplier lies between the smallest and largest state multipliers, then passes the policy bounds and the 0.25-per-day smoothing of HAT-PORT-050 and HAT-PORT-054. Harm: v2 produces multipliers outside what the risk policy allows.

#### HAT-PORT-073: Acceptance gate and fallback
- **Statement:** v2 ships as a regime-model version through backtest → shadow → live only if a walk-forward test beats v1 on sleeve drawdown and risk-adjusted return; stale inputs or a failed fit fall back to v1 automatically; v2 never edits the risk policy or invariants.
- **Source:** BL "Regime model v2" (Acceptance, Fallback, Limits).
- **PR:** P2
- **Verification:** TLA `SleeveLifecycle!NoLiveWithoutEvidence`, PBT `regime_v2.fallback_to_v1`
- **Acceptance:**
  - Property: for every day with stale inputs or a failed fit, the multiplier used equals the v1 multiplier for that day. Harm: a broken model sets exposure with no rules behind it.
  - Property: in every reachable state, a v2 model version is live only after its walk-forward evidence beats v1 on both measures. Harm: an unproven model moves money.
