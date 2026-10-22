# 04: Criteria and Bots

Scope: the nine-stage bot model, criterion instances and their validation, setup scoring, the 25-type criteria library, playbooks, the two MVP default bot configs and config versioning. Requirement format and verification methods: see README.md and 11-verification.md.

Risk numbers (risk %, caps, ladder, kill thresholds, allocations) are not part of a bot; they live in the account risk policy (06-risk-and-governance.md). Stop-rule and exit-rule semantics are specified in 07-execution-and-brokers.md; this file carries them only as config values.

ID blocks: `HAT-CRIT-010..039` for §1-§3, `HAT-CRIT-040` for `vcp` (kept from the plan), `HAT-CRIT-100..122` for the other 23 catalogue types in library-table order, `HAT-BOT-050..089` for §5-§8.

## §1 Nine-stage model

A bot version is nine stages evaluated in a fixed order (BL "Configuration model").

| # | Stage | Runs | Holds | Output |
| --- | --- | --- | --- | --- |
| 1 | Universe | Nightly | Criteria (liquidity, series, bands) + include/exclude lists | Eligible symbols |
| 2 | Regime gate | Nightly + live | One regime rule per bot | Longs allowed yes/no |
| 3 | Setup | Nightly | Criteria (hard or scored) | Watchlist rows: score, pivot, planned stop |
| 4 | Trigger | Live | Criteria (price, volume, time window) | Trigger event |
| 5 | Entry filters | Live, at trigger | Criteria (ADTR, max chase, circuit) | Pass / skip with reason |
| 6 | Sizing | Risk manager | Account risk policy: risk %, caps, exposure ladder | Quantity |
| 7 | Stop | Risk manager | One stop rule | Stop price |
| 8 | Exit stack | Live + close | Ordered exit rules | Exit orders |
| 9 | Execution mode | Router | shadow / confirm / auto | Alert or order |

#### HAT-CRIT-010: Stages evaluate in the fixed order 1 to 9
- **Statement:** The engine evaluates a bot version's stages in the order universe, regime gate, setup, trigger, entry filters, sizing, stop, exit stack, execution mode, and a symbol reaches a later stage only after passing every earlier one.
- **Source:** BL "Configuration model"; B3, B4, T12.
- **PR:** 8, 11
- **Verification:** PBT `scoring.stage_order`, PBT `scan.watchlist_gates`, PBT `scan.no_universe_publishes_nothing`, DB `runtime.publish_serialised`
- **Acceptance:**
  - Property: for every bot config and every generated feature frame, no symbol has a setup result unless it passed the universe stage and the regime gate allowed longs, and no trigger is evaluated for a symbol absent from that day's watchlist (except the episodic-pivot playbook, HAT-BOT-052). Harm: a stock that fails liquidity or regime rules could still become a trade.
  - Property: for every symbol, the stage outputs recorded in `criteria_results` appear in stage order and stop at the first failing stage. Harm: rejection reasons point at the wrong stage and the digest misleads the owner.
  - Property: the nightly scan evaluates only members that have a bar on the scan date; a member whose last bar is older (suspended, no data) is not evaluated on stale bars. Harm: a stock that did not trade is proposed from old prices.
  - Property: a bot with no universe publishes nothing and records no `watchlist.published`, so a run on the same day after a universe is assigned still publishes; two concurrent publishes of one bot and day write one event (the bot's lock serialises them). Harm: an empty or duplicate watchlist blocks or doubles the day's real one.

#### HAT-CRIT-011: Only stages 1, 3, 4 and 5 hold library criteria
- **Statement:** Stages 1, 3, 4 and 5 hold criteria from the library; stages 2, 6, 7, 8 and 9 hold rules with parameters, and stage 6 holds no bot-level values at all.
- **Source:** BL "Configuration model"; BL "Summary"; B3.
- **PR:** 8
- **Verification:** PBT `config.stage_slots`
- **Acceptance:**
  - Property: for every generated config, validation rejects a criterion whose type's library stage is not one of universe, setup, trigger or entry_filter, and rejects any sizing value (risk %, max position share, positions cap) inside a strategy version. Harm: a strategy edit could quietly raise risk, which the governance split forbids.

## §2 Criterion instance and schema validation

| Field | Meaning |
| --- | --- |
| `type` | Library criterion, e.g. `sma_above`, `vcp`, `gap_up` |
| `stage` | universe / setup / trigger / entry\_filter |
| `mode` | `hard` (must pass) or `score` (contributes 0-1) |
| `weight` | Weight within the setup score; ignored for hard |
| `params` | Typed parameters validated against the type's schema |
| `enabled`, `order` | Switch off without deleting; evaluation order |

#### HAT-CRIT-020: A criterion instance carries exactly the seven documented fields
- **Statement:** Every criterion instance in a config has `type`, `stage`, `mode` (`hard` or `score`), `weight`, `params`, `enabled` and `order`, with `weight` ignored for `hard`.
- **Source:** BL "Configuration model / A criterion instance"; B3, B4.
- **PR:** 8
- **Verification:** PBT `config.instance_fields`, CONF `criterion_instance_schema`
- **Acceptance:**
  - Property: for every generated instance, the loaded model either has all fields with `mode` in {`hard`, `score`} and, when `mode = score`, `weight > 0`, or the load is rejected with a field-level reason. Harm: a malformed criterion silently changes which stocks qualify.
  - Property: for every config, changing the `weight` of a `hard` criterion does not change any evaluation result. Harm: the weight of a hard rule would leak into the score.

#### HAT-CRIT-021: Params are validated against the type schema; invalid configs never save
- **Statement:** Each criterion's `params` are validated on save against its type's JSON Schema stored in `hat_criterion_type.param_schema`, and an invalid config is never saved.
- **Source:** BL "Configuration model / Editing rules"; SCH "Strategy, risk policy and bots" (`hat_criterion_type`); B3, T10.
- **PR:** 8
- **Verification:** PBT `config.invalid_never_saves`, CONF `criterion_type_registry`
- **Acceptance:**
  - Property: for every generated params object that violates its type's schema (wrong type, out-of-range, unknown key), saving the version raises and no `hat_strategy_version` row is written. Harm: the engine would run a config whose meaning nobody checked.
  - Property: for every type in the registry, the stored `param_schema` equals the JSON Schema generated from the type's parameter model, and the registry holds exactly the 25 types of §4. Harm: app and catalogue validation disagree, so a config valid in one place fails or misbehaves in the other.

#### HAT-CRIT-022: `enabled` and `order` control evaluation without deleting
- **Statement:** A disabled criterion is skipped entirely, and enabled criteria within a stage evaluate in ascending `order`.
- **Source:** BL "Configuration model / A criterion instance"; B3.
- **PR:** 8
- **Verification:** PBT `scoring.disabled_is_noop`
- **Acceptance:**
  - Property: for every config and data frame, the results with a criterion disabled equal the results with that criterion removed (except `max_chase`, HAT-CRIT-122). Harm: switching a rule off would still change who qualifies.
  - Property: for every permutation of stage criteria, the hard-failure reason recorded is the one from the lowest `order` among failing hard criteria. Harm: the stored reason is not reproducible, so replays and digests disagree.

## §3 Score combination and threshold

A symbol makes the watchlist when every hard criterion passes and the weighted setup score clears the bot's threshold (BL "How criteria combine"):

```
setup_score = sum_i(w_i * s_i) / sum_i(w_i),   s_i in [0, 1]
```

#### HAT-CRIT-030: Setup score is the weighted mean of scored criteria
- **Statement:** The setup score equals sum(w_i * s_i) / sum(w_i) over enabled `score`-mode setup criteria, each s_i in [0, 1].
- **Source:** BL "How criteria combine"; B4.
- **PR:** 8
- **Verification:** PBT `scoring.weighted_mean`
- **Acceptance:**
  - Property: for every list of (w_i > 0, s_i in [0,1]), the computed score equals the formula (exact Decimal or within 1e-12 float), lies in [min s_i, max s_i] and is invariant to the order of criteria and to scaling all weights by the same positive factor. Harm: the watchlist would rank or admit stocks by a number that does not mean what the config says.
  - Property: for every config with no enabled score-mode setup criteria, the setup score is defined as 1.0 and only hard criteria decide. Harm: a setup with only hard rules (EP playbook) would divide by zero and drop every candidate.
- **[OWNER-PENDING]** empty-score case: default setup_score = 1.0 when no scored criterion is enabled (docs silent); owner to confirm.

#### HAT-CRIT-031: Default setup threshold 0.6
- **Statement:** A bot's setup threshold defaults to 0.6 and a symbol passes setup only if setup_score >= threshold.
- **Source:** BL "How criteria combine"; BL "Defaults that are assumptions"; B4.
- **PR:** 8
- **Verification:** PBT `scoring.threshold_gate`
- **Acceptance:**
  - Property: for every score and threshold in [0, 1], the symbol passes iff all hard criteria pass and score >= threshold. Harm: stocks below the bar reach the watchlist, or valid ones are dropped.
- **[A]** setup threshold: default 0.6; validated by backtest of signal count vs expectancy (BL "Defaults that are assumptions").

#### HAT-CRIT-032: Hard criteria short-circuit and store the first failure reason
- **Statement:** Evaluation stops at the first failing hard criterion and stores its reason.
- **Source:** BL "How criteria combine"; B4.
- **PR:** 8
- **Verification:** PBT `scoring.hard_short_circuit`
- **Acceptance:**
  - Property: for every config and frame where some hard criterion fails, no criterion after it (by `order`) is evaluated and the stored result names that criterion with its reason and value. Harm: rejected symbols carry wrong or missing reasons, and expensive criteria run for nothing.

## §4 Criteria catalogue

25 criterion types cover both MVP bots (BL "Criteria library"; `close_vs_pivot` added in PR 54). Each is a pure function over daily bars, features or live bars with a typed parameter schema, and returns `(passed, score in [0,1], value, reason)`. M = Minervini-style, Q = Qullamaggie-style breakout, Q-EP = episodic pivot. Feature columns (`sma*`, `high_52w`, `low_52w`, `adr20`, `adtr20`, `adv20`, `turnover20`, `ret*`, `rs_pct`, `mom_rank`) are defined in 02-data.md §7.

| Type | Stage | Parameters (default) | Data | Used by |
| --- | --- | --- | --- | --- |
| `series_in` | Universe | series list (EQ, BE) | Symbol master | Both |
| `min_turnover` | Universe | avg days (20), ₹ cr (10) | Daily bars | Both |
| `min_price` | Universe | ₹ (50, *assumption*) | Daily bars | Both |
| `price_band_allowed` | Universe | allowed bands (all) | Band master | Both |
| `adr_min` | Universe | days (20), % (4, *assumption*) | Daily bars | Q |
| `sma_above` | Setup | price or SMA(a) > SMA(b) | Daily bars | M |
| `sma_rising` | Setup | SMA length (200), lookback days (21) | Daily bars | M |
| `pct_from_52w_low` | Setup | min % (30) | Daily bars | M |
| `pct_from_52w_high` | Setup | max % below (25) | Daily bars | M |
| `rs_rank` | Setup | weights 3/6/9/12 m (0.4/0.2/0.2/0.2), min percentile (70) | Universe returns | M |
| `momentum_rank` | Setup | windows (21, 63, 126 days), top % (2), any/all (any) | Universe returns | Q |
| `vcp` | Setup | contractions (2-6), depth ratio max (0.75), final depth max % (10), lookback days (120), volume dry-up × 50-day avg (0.7) | Daily bars | M |
| `consolidation` | Setup | length days (5-15), max range % (15), higher lows (on) | Daily bars | Q |
| `ma_surf` | Setup | MAs (10, 20), max distance % (5) | Daily bars | Q |
| `prior_move` | Setup | min % (30) within days (21-63) | Daily bars | Q |
| `earnings_growth` | Setup | EPS YoY % (20), sales YoY % (20), accelerating (on); **off by default** | Fundamentals (later) | M |
| `close_vs_pivot` | Setup | max % above the pivot of an earlier criterion (0) | Daily bars | M |
| `gap_up` | Trigger | min gap % (10) vs prior close | Pre-open + live | Q-EP |
| `volume_pace` | Trigger | × 50-day avg, projected to full day (1.5) | Live bars | M, Q |
| `early_volume` | Trigger | cumulative by entry ≥ × 20-day ADV (0.5, *assumption*) | Live bars | Q-EP |
| `catalyst_attached` | Trigger | announcement within hours (24); required in confirm mode | NSE announcements | Q-EP |
| `price_breaks_level` | Trigger | level (pivot or opening-range high), bar (5-min) | Live bars | M, Q |
| `neglect` | Trigger | 63-day return max % (30, *assumption*) | Daily bars | Q-EP |
| `adtr_filter` | Entry filter | (entry − stop) ≤ × ADTR (1.0; EP 1.5) | Daily bars | Q |
| `max_chase` | Entry filter | % above trigger level (M 5, Q 2); also skips a stock locked at upper circuit | Live | Both |

RS rank is a proxy built from universe-wide returns, not IBD's proprietary rating. Score curves the docs do not define are `[OWNER-PENDING]` with a linear default; their boundary properties below are binding.

Common properties for every catalogue type (apply to each requirement below; verified once per type by `PBT criteria.<type>_contract`):

- Purity: for every input frame, the result depends only on rows dated ≤ the evaluation date (no look-ahead) and repeated calls return identical results.
- Range: `score` is in [0, 1] for every input; in `hard` mode, `passed` is the documented condition.
- Missing data: when a required input is null or absent, the criterion returns `passed = false` with reason `no_data` (fail closed).

#### HAT-CRIT-040: `vcp` scored volatility-contraction detector
- **Statement:** `vcp` detects a base of 2 to 6 contractions within the last 120 trading days where each contraction's depth is at most 0.75 × the previous one, the final contraction's depth is at most 10%, and volume in the final contraction is at most 0.7 × the 50-day average; it outputs a score in [0,1], the contraction count, the pivot and the final contraction low.
- **Source:** BL "Criteria library" (`vcp`); BL "Minervini-style bot"; B7; PLAN §4.1 (port of `services/vcp/pattern.py`).
- **PR:** 8
- **Verification:** PBT `criteria.vcp_contract`, PBT `criteria.vcp_parity`
- **Acceptance:**
  - Property: for every generated bar series, whenever `vcp` reports a base, the reported contraction count n satisfies 2 <= n <= 6, every consecutive depth ratio d_{k+1}/d_k <= 0.75, the final depth <= 10%, the final contraction's mean volume <= 0.7 × the 50-day average volume, and all contractions lie within the last 120 trading days. Harm: loose bases enter the watchlist and the stop under the final contraction is meaningless.
  - Property: for every base reported, the exposed final contraction low is the minimum low of the final contraction and is below the exposed pivot, so the `below_final_contraction_low` stop rule (07 §5) always has a level below entry. Harm: a stop could be placed at or above the entry price.
  - Property: for every series where the existing `services/vcp/pattern.py` detector and the port share definitions, both report the same contraction count and final contraction low. Harm: the port silently changes which bases are found compared to the tested detector.
  - Property: the score is monotone non-increasing in final depth and in the dry-up ratio when all else is held equal. Harm: tighter, quieter bases would score worse than loose ones.
- **[A]** depth ratio / final depth / dry-up: defaults 0.75 / 10% / 0.7×; validated by backtest and manual review of 50 flagged bases (BL "Defaults that are assumptions").
- **[OWNER-PENDING]** pivot and score curve: default pivot = highest high of the final contraction; default score = mean of four linear sub-scores (count in range, depth-ratio margin, final-depth margin, dry-up margin), each 1 at the tightest observed value and 0 at the parameter limit. Docs silent; owner to confirm.

#### HAT-CRIT-100: `series_in`
- **Statement:** `series_in` passes iff the instrument's series is in the configured list (default EQ, BE).
- **Source:** BL "Criteria library"; B22.
- **PR:** 8
- **Verification:** PBT `criteria.series_in_contract`
- **Acceptance:**
  - Property: for every instrument and every series list, `passed` equals membership of the instrument's series (from the instrument master, 02 §2) in the list. Harm: trade-for-trade or other series slip into the universe.

#### HAT-CRIT-101: `min_turnover`
- **Statement:** `min_turnover` passes iff the 20-day average turnover is at least ₹10 crore (₹10 × 10^7).
- **Source:** BL "Criteria library"; B22; DS "Source map" (turnover = close × volume when traded value is null).
- **PR:** 8
- **Verification:** PBT `criteria.min_turnover_contract`
- **Acceptance:**
  - Property: for every bar series and parameters (days, inr_cr), `passed` iff mean over the last `days` sessions of (traded_value, or close × volume where traded_value is null) >= inr_cr × 10^7. Harm: illiquid stocks reach sizing, where fills and slippage break the risk math.

#### HAT-CRIT-102: `min_price`
- **Statement:** `min_price` passes iff the latest close is at least ₹50.
- **Source:** BL "Criteria library".
- **PR:** 8
- **Verification:** PBT `criteria.min_price_contract`
- **Acceptance:**
  - Property: for every close and threshold, `passed` iff close >= inr. Harm: penny stocks with erratic ticks enter the universe.
- **[A]** minimum price: default ₹50; validated by backtest (BL marks it *assumption*).

#### HAT-CRIT-103: `price_band_allowed`
- **Statement:** `price_band_allowed` passes iff the instrument's current price band (2/5/10/20/none) is in the allowed set (default all bands).
- **Source:** BL "Criteria library"; DS "Reference data" (price band).
- **PR:** 8
- **Verification:** PBT `criteria.price_band_allowed_contract`
- **Acceptance:**
  - Property: for every band history and evaluation date, `passed` iff the latest `hat_price_band` row with `effective_from` <= date has a band in the allowed set, and with the default set every instrument with a band row passes. Harm: an outdated band admits stocks the owner excluded, or excludes valid ones.

#### HAT-CRIT-104: `adr_min`
- **Statement:** `adr_min` passes iff ADR over 20 days is at least 4%.
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8
- **Verification:** PBT `criteria.adr_min_contract`
- **Acceptance:**
  - Property: for every bar series, `passed` iff `adr20` (02 §7) >= pct. Harm: slow stocks enter a strategy built for fast movers.
- **[A]** ADR minimum: default 4%; validated by backtest across 3-5% (BL "Defaults that are assumptions").

#### HAT-CRIT-105: `sma_above`
- **Statement:** `sma_above` passes iff value(a) > value(b), where each of a, b is `close` or an SMA length.
- **Source:** BL "Criteria library"; BL "Minervini-style bot" (Trend Template).
- **PR:** 8
- **Verification:** PBT `criteria.sma_above_contract`
- **Acceptance:**
  - Property: for every bar series and (a, b), `passed` iff value(a) > value(b) strictly, using the SMA columns of 02 §7 on adjusted bars, and `passed` is false when either value is null (fewer bars than the SMA length). Harm: newly listed stocks without 200 days of history would pass the Trend Template.

#### HAT-CRIT-106: `sma_rising`
- **Statement:** `sma_rising` passes iff SMA(200) is higher than it was 21 trading days earlier.
- **Source:** BL "Criteria library"; BL "Minervini-style bot".
- **PR:** 8
- **Verification:** PBT `criteria.sma_rising_contract`
- **Acceptance:**
  - Property: for every series and (sma, lookback_days), `passed` iff SMA_sma(t) > SMA_sma(t − lookback_days) with both defined. Harm: stocks in a flat or falling long-term trend pass the Stage 2 filter.
- **[OWNER-PENDING]** "rising" definition: default point-to-point comparison with the value `lookback_days` sessions earlier (docs give only the parameters); owner to confirm.

#### HAT-CRIT-107: `pct_from_52w_low`
- **Statement:** `pct_from_52w_low` passes iff close is at least 30% above the 52-week low.
- **Source:** BL "Criteria library".
- **PR:** 8
- **Verification:** PBT `criteria.pct_from_52w_low_contract`
- **Acceptance:**
  - Property: for every series, `passed` iff (close / low_52w − 1) × 100 >= min_pct. Harm: stocks barely off their lows pass as leaders.

#### HAT-CRIT-108: `pct_from_52w_high`
- **Statement:** `pct_from_52w_high` passes iff close is at most 25% below the 52-week high.
- **Source:** BL "Criteria library".
- **PR:** 8
- **Verification:** PBT `criteria.pct_from_52w_high_contract`
- **Acceptance:**
  - Property: for every series, `passed` iff (1 − close / high_52w) × 100 <= max_pct_below. Harm: deeply broken stocks pass as Stage 2 candidates.

#### HAT-CRIT-109: `rs_rank`
- **Statement:** `rs_rank` uses `rs_pct` (universe percentile of the 0.4/0.2/0.2/0.2-weighted 3/6/9/12-month returns); hard mode passes iff rs_pct >= 70, score mode scores 0 at or below `score_from` and 1 at or above `score_full`.
- **Source:** BL "Criteria library"; BL "Minervini-style bot" (`min_percentile: 70`, `score_from: 70`, `score_full: 95`).
- **PR:** 8
- **Verification:** PBT `criteria.rs_rank_contract`
- **Acceptance:**
  - Property: for every universe return matrix, `rs_pct` is in [0, 100], is invariant to the order of instruments, and is monotone non-decreasing in an instrument's weighted return holding the others fixed. Harm: relative strength ranks would depend on row order or reward weaker stocks.
  - Property: in hard mode `passed` iff rs_pct >= min_percentile; in score mode score = 0 when rs_pct <= score_from, 1 when rs_pct >= score_full, and monotone non-decreasing between. Harm: weaker stocks outrank stronger ones in the watchlist.
- **[OWNER-PENDING]** score curve between `score_from` and `score_full`: default linear; owner to confirm.

#### HAT-CRIT-110: `momentum_rank`
- **Statement:** `momentum_rank` passes iff the instrument's return is in the top 2% of the universe over any (default) or all of the 21, 63 and 126-day windows.
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8, 11
- **Verification:** PBT `criteria.momentum_rank_contract`, PBT `scan.rank_within_members`
- **Acceptance:**
  - Property: for every universe return matrix, with `match: any` `passed` iff some window w has the instrument within the top `top_pct`% by `ret_w`, and with `match: all` iff every window does; the passing count per window is at most ceil(top_pct/100 × universe size) plus ties. Harm: the breakout playbook trades stocks that are not the market's strongest movers.
  - Property: in the nightly scan the universe is the bot's members on the scan date: feature rows of instruments outside it never move the cutoff. Harm: another universe's leaders push this bot's stocks out of its top band.

#### HAT-CRIT-111: `consolidation`
- **Statement:** `consolidation` passes iff the most recent 5 to 15 sessions form a range no wider than 15% with higher lows (when on).
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8
- **Verification:** PBT `criteria.consolidation_contract`
- **Acceptance:**
  - Property: whenever `passed`, there is a window length L in [5, 15] ending at the evaluation date with (max high / min low − 1) × 100 <= max_range_pct, and with `higher_lows: true` the window's lows satisfy the higher-lows rule; the exposed `consolidation_high` is that window's max high. Harm: wide, sloppy ranges qualify and the breakout level is wrong.
  - Property: the score is in [0, 1] and monotone non-increasing in the range %. Harm: looser consolidations rank above tighter ones.
- **[OWNER-PENDING]** window choice, higher-lows rule and score curve: default = the tightest qualifying window; higher lows = the minimum low of the second half of the window >= the minimum low of the first half; score = 1 − range% / max_range_pct. Docs silent; owner to confirm.

#### HAT-CRIT-112: `ma_surf`
- **Statement:** `ma_surf` passes iff close is within 5% of the 10- and 20-day SMAs.
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8
- **Verification:** PBT `criteria.ma_surf_contract`
- **Acceptance:**
  - Property: for every series, `passed` iff for each SMA m in `mas`, |close / SMA_m − 1| × 100 <= max_distance_pct; score in [0,1] is monotone non-increasing in the largest distance. Harm: extended stocks far above their averages rank as orderly surfers.
- **[OWNER-PENDING]** distance definition and score curve: default absolute distance as above, score = 1 − max distance / max_distance_pct (floored at 0). Docs silent; owner to confirm.

#### HAT-CRIT-113: `prior_move`
- **Statement:** `prior_move` passes iff the stock rose at least 30% within some window of 21 to 63 sessions.
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8
- **Verification:** PBT `criteria.prior_move_contract`
- **Acceptance:**
  - Property: for every bar series, `passed` iff for some d in [21, 63] there are sessions i < j inside the trailing d-session window with (high_j / low_i − 1) × 100 >= min_pct; the result is monotone in min_pct (raising it never turns a fail into a pass). Harm: stocks with no prior thrust are treated as momentum leaders.
- **[OWNER-PENDING]** measurement of the move inside `within_days: [21, 63]`: default = largest low-to-high rise (low before high) inside the trailing d-session window for any d in [21, 63]; docs give only the parameters; owner to confirm.

#### HAT-CRIT-114: `earnings_growth` registered, disabled by default
- **Statement:** `earnings_growth` (EPS YoY >= 20%, sales YoY >= 20%, accelerating on) is registered with its schema but disabled in every MVP config and has no data source until Phase 2.
- **Source:** BL "Criteria library"; BL "Minervini-style bot" (`enabled: false`); B6; PL5; I5.
- **PR:** 8
- **Verification:** PBT `criteria.earnings_growth_contract`, CONF `bot_seed_s1_matches_spec`
- **Acceptance:**
  - Property: for every MVP config, the `earnings_growth` instance has `enabled: false`, and if it is ever enabled without fundamentals rows with `filed_at` <= the evaluation date it returns `passed = false` with reason `no_data`. Harm: an enabled filter with no data either admits everything or uses numbers the market had not seen.

#### HAT-CRIT-115: `gap_up`
- **Statement:** `gap_up` passes iff the open (or pre-open indicative price) is at least 10% above the prior close.
- **Source:** BL "Criteria library"; DS "Live data"; B10.
- **PR:** 8, 15
- **Verification:** PBT `criteria.gap_up_contract`
- **Acceptance:**
  - Property: for every (open, prev_close > 0), `passed` iff (open / prev_close − 1) × 100 >= min_pct, with prev_close taken from the adjusted daily bars of the previous session. Harm: small gaps trigger the episodic-pivot playbook, or a split makes a fake gap.

#### HAT-CRIT-116: `volume_pace`
- **Statement:** `volume_pace` passes iff the day's volume projected to a full session is at least 1.5 × the 50-day average volume. At a trigger the volume and the elapsed fraction are both read at the bar's final instant (end + 2 s).
- **Source:** BL "Criteria library" (`× 50-day avg, projected to full day (1.5)`); BL bot configs (`vs_avg50: 1.5`); DS "Bar builder rules"; B8.
- **PR:** 8, 15
- **Verification:** PBT `criteria.volume_pace_contract`
- **Acceptance:**
  - Property: for every cumulative volume V >= 0 and elapsed session fraction f in (0, 1], projected volume = V / f and `passed` iff V / f >= vs_avg50 × avg50 volume; projected volume is monotone non-decreasing in V and non-increasing in f. Harm: breakouts on thin volume trigger entries.
  - Property: before the first 5-minute bar is final (f undefined or 0) the criterion does not pass. Harm: division by a near-zero fraction fires a trigger at the open on noise.

#### HAT-CRIT-117: `early_volume`
- **Statement:** `early_volume` passes iff cumulative volume at entry time is at least 0.5 × ADV(20).
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot".
- **PR:** 8, 15
- **Verification:** PBT `criteria.early_volume_contract`
- **Acceptance:**
  - Property: for every (V, adv20 > 0), `passed` iff V >= vs_adv20 × adv20. Harm: episodic pivots without heavy opening volume are traded.
- **[A]** early volume: default 0.5× ADV20; validated in shadow mode over one earnings season (BL "Defaults that are assumptions").

#### HAT-CRIT-118: `catalyst_attached`
- **Statement:** `catalyst_attached` passes iff an NSE announcement matched to the instrument was published within the last 24 hours; it is required in the modes its `required_in` lists (default confirm) and informational in the others. A trigger-stage instance is always `hard` (the `score` mode exists only in the setup stage, HAT-CRIT-020), so `required_in` alone decides where it blocks: listing all three modes makes it block everywhere. [Revised in PR 15a: the earlier "unless marked hard" could not be told apart from the default `mode: hard` every stored instance carries.]
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot" (`required_in: [confirm]`); DS "Announcements for EP catalysts"; B10, I6.
- **PR:** 8, 15
- **Verification:** PBT `criteria.catalyst_attached_contract`
- **Acceptance:**
  - Property: for every set of announcements and trigger time T, `passed` iff some announcement with matching `instrument_id` has T − 24h <= published_at <= T. Harm: stale or unrelated news is shown as the reason for a trade.
  - Property: for every mode, the criterion blocks the trigger only when the bot's mode is in `required_in`; otherwise its result is recorded but does not block. Harm: auto mode would skip valid EPs on a feed gap, or confirm mode would show cards with no catalyst.

#### HAT-CRIT-119: `price_breaks_level`
- **Statement:** `price_breaks_level` fires when live price trades above the configured level (pivot, opening-range high, or the maximum of consolidation high and opening-range high), with the opening range defined by the first 5-minute bar.
- **Source:** BL "Criteria library"; BL bot configs; MA "Daily cycle" steps 4-5; DS "Live data"; B8, B9.
- **PR:** 8, 15
- **Verification:** PBT `criteria.price_breaks_level_contract`
- **Acceptance:**
  - Property: for every tick sequence, the criterion fires at most once per (bot, playbook, instrument, trade date), only at a tick with price > level, and never before 09:20 IST when the level uses the opening range (the 09:15-09:20 bar must be final). Harm: entries fire before the opening range is known or fire repeatedly.
  - Property: the trigger price recorded equals the level. Harm: the max-chase check and sizing use the wrong reference price.
- **[OWNER-PENDING]** break semantics: default = the first final 5-minute bar whose final instant (bar end + 2 s) lies in 09:20-15:20 IST and whose high is strictly above the level, so a tick traded above the level inside that bar; triggers evaluate only on final bars (HAT-EXEC-120), and the entry price is the bar's close. The docs give only "level" and "bar (5-min)". Owner to confirm. [Reconciled in PR 15a with HAT-EXEC-120, which had evaluation on final bars while this default read "first tick".]

#### HAT-CRIT-120: `neglect`
- **Statement:** `neglect` passes iff the 63-day return is at most 30%.
- **Source:** BL "Criteria library" (stage Trigger); BL "Qullamaggie-style bot" (`max_return_63d_pct: 30`, used in the EP setup).
- **PR:** 8, 15
- **Verification:** PBT `criteria.neglect_contract`
- **Acceptance:**
  - Property: for every series, `passed` iff ret63 × 100 <= max_return_63d_pct, computed on adjusted bars up to the previous session. Harm: already-extended stocks are treated as neglected EP candidates.
- **[A]** neglect: default 30%; validated in shadow mode over one earnings season (BL "Defaults that are assumptions").

#### HAT-CRIT-121: `adtr_filter`
- **Statement:** `adtr_filter` passes iff (entry − stop) <= max_multiple × ADTR(20), with max_multiple 1.0 for breakouts and 1.5 for episodic pivots.
- **Source:** BL "Criteria library"; BL "Qullamaggie-style bot"; MA "Risks" (skip any trade whose stop is wider than ADR).
- **PR:** 8, 15
- **Verification:** PBT `criteria.adtr_filter_contract`
- **Acceptance:**
  - Property: for every (entry > stop, adtr20 > 0, m), `passed` iff entry − stop <= m × adtr20. Harm: trades with stops wider than the stock's normal range are taken, so size shrinks and stops get hit by noise.

#### HAT-CRIT-122: `max_chase` always applies
- **Statement:** `max_chase` skips an entry when price at entry is more than pct% above the trigger level (Minervini 5, Qullamaggie 2) or the stock is locked at its upper circuit, and it applies even when the user deletes or disables it.
- **Source:** BL "Criteria library"; BL "Risk management / Invariants" (6); MA "Execution modes"; B25.
- **PR:** 8, 15
- **Verification:** PBT `criteria.max_chase_contract`, TLA `ExecutionRouter!MaxChaseAlwaysApplied`
- **Acceptance:**
  - Property: for every (price, level > 0, pct), `passed` iff price <= level × (1 + pct/100) and the stock is not locked at upper circuit. Harm: the bot buys far above its planned entry, breaking the planned risk.
  - Property: for every config, including ones where `max_chase` is deleted or `enabled: false`, the evaluated entry-filter list contains `max_chase` with the playbook's last configured pct, or the strategy's default (M 5, Q 2) when none was ever configured. Harm: a config edit removes invariant 6.

#### HAT-CRIT-123: `close_vs_pivot` refuses a base that already broke out
- **Statement:** `close_vs_pivot` passes iff the scan-day close is at most `max_above_pct` (default 0) percent above the pivot an earlier criterion of the same stage exposed (`vcp`'s pivot, HAT-CRIT-040); without a pivot it fails with `no_pivot`. A criterion reads the numeric levels (`extras`) the passed criteria evaluated before it in its stage reported, each key from the first to report it, and only those; the stage's levels are the ones the scan publishes with the watchlist row. A config with an enabled `close_vs_pivot` and no enabled `vcp` before it in the same setup is refused (`close_vs_pivot_needs_vcp`), and so is a `close_vs_pivot` in score mode (`close_vs_pivot_not_hard`: a gate that only lowers the score would let a stale breakout publish). In S1 this makes a VCP base required: a symbol whose `vcp` finds none stops at `close_vs_pivot` with `no_pivot`.
- **Source:** Plan audit 2026-10-22 N2 and §6 item 12 (owner default: close at or below the pivot, hard; a later sweep tests 0/1/2%).
- **PR:** 54
- **Verification:** PBT `criteria.close_vs_pivot_contract`, PBT `criteria.close_vs_pivot_stage`, PBT `scoring.stage_levels`, PBT `scoring.numeric_levels`, PBT `config.close_vs_pivot_needs_vcp`
- **Acceptance:**
  - Property: for every close C and pivot P > 0, `passed` iff C × 100 <= P × (100 + max_above_pct), exact in decimals (equality passes); the value is (C / P − 1) × 100; without a pivot level, or with P <= 0, the result fails with `no_pivot`, and without the date's bar with `no_data`. Harm: a name that broke out days ago is published with its stale pivot and bought far above the base.
  - Property: in a stage, `close_vs_pivot` ordered after `vcp` reads the pivot `vcp` reported for that instrument and date; ordered before it, or after a `vcp` that found no base, it fails with `no_pivot`; the shared evaluation context is never changed, so no level reaches another stage or playbook. Harm: the gate checks a level from another symbol, date or playbook.
  - Property: for every sequence of criteria in a stage, each sees exactly the finite numeric levels the passed criteria before it reported (first reporter per key), and the stage's levels equal the levels the scan publishes; a failed result adds none, and a non-numeric or non-finite extra is never a level and never raises. Harm: the gate approves one level while the watchlist row and the trigger use another, or one odd extra aborts the scan.
  - Property: a setup with an enabled hard `close_vs_pivot` is accepted iff an enabled `vcp` precedes it, and a scored one is refused. Harm: a config whose gate can never pass is saved and the playbook silently stops trading.
- **[A]** max_above_pct: default 0 (owner, §6 item 12); a backtest sweep over 0/1/2% decides it.

## §5 Playbooks

A **playbook** is a named set of setup, trigger, entry filters and stop; a bot can hold several. Playbooks share one regime gate, capital pool, sizing and exit stack (BL "Qullamaggie-style bot").

#### HAT-BOT-050: A playbook bundles setup, trigger, entry filters and stop
- **Statement:** A bot holds one or more named playbooks, each with its own setup, trigger, entry filters and stop rule, while regime gate, sizing (risk policy) and exit stack are bot-level and shared.
- **Source:** BL "Configuration model" (stages 3-5 and 7 repeat as playbooks); BL "Qullamaggie-style bot".
- **PR:** 8, 11
- **Verification:** PBT `config.playbook_shape`, PBT `trades.shared_bot_cap`
- **Acceptance:**
  - Property: for every valid config, each playbook has a unique name within the bot and exactly one stop rule, and no playbook carries a regime rule, sizing value or exit stack. Harm: playbooks drift into separate risk or exit behaviour the owner did not approve.
  - Property: for every bot, signals from all playbooks count against the same per-bot caps and capital (06 §3). Harm: two playbooks double the bot's risk budget.

#### HAT-BOT-051: One signal per bot, playbook, instrument and day
- **Statement:** A playbook produces at most one signal per instrument per trade date, keyed (bot, playbook, instrument, trade_date).
- **Source:** SCH "Runtime" (`hat_signal` unique); MA "Failure handling" (duplicate trigger).
- **PR:** 8, 15
- **Verification:** DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- **Acceptance:**
  - Property: for every interleaving of trigger events, at most one `hat_signal` row exists per (bot_id, playbook, instrument_id, trade_date). Harm: a repeated trigger buys the same stock twice.

#### HAT-BOT-052: Episodic-pivot setup is built live from the pre-open scan
- **Statement:** The `episodic_pivot` playbook's setup runs live on candidates from the 09:16 gap scan, not on the nightly watchlist, and candidates come only from 10%/20%-band stocks and F&O names.
- **Source:** BL "Qullamaggie-style bot" (EP setup comment; India-specific consequence); DS "Live data"; B10.
- **PR:** 14, 15
- **Verification:** PBT `criteria.ep_candidate_bands`
- **Acceptance:**
  - Property: for every gap-scan result, every EP candidate has a price band of 10% or 20% or is an F&O name, gapped >= 10%, and passes the bot's universe criteria. Harm: the scan subscribes stocks that cannot gap 10%, wasting the symbol budget and producing impossible signals.

#### HAT-BOT-053: Strategy regime rules are evaluated as configured
- **Statement:** The Minervini-style regime rule `close_above_sma_and_rising` passes iff the NIFTY 500 close is above its 50-day SMA and that SMA is higher than 10 sessions earlier; the Qullamaggie-style rule `sma_fast_above_slow_both_rising` passes iff the index SMA(10) > SMA(20) and both are rising.
- **Source:** BL "Minervini-style bot" and "Qullamaggie-style bot" (`regime:` blocks); B11, B12.
- **PR:** 8, 11
- **Verification:** PBT `regime.strategy_rules`
- **Acceptance:**
  - Property: for every index series, `close_above_sma_and_rising(sma=50, rising_days=10)` is true iff close_t > SMA50_t and SMA50_t > SMA50_{t−10}; `sma_fast_above_slow_both_rising(fast=10, slow=20)` is true iff SMA10_t > SMA20_t, SMA10_t > SMA10_{t−1} and SMA20_t > SMA20_{t−1}. Harm: the gate lets entries through in a falling market or blocks them in a rising one.
- **[A]** Minervini regime rule: default NIFTY 500 > rising 50-day SMA; validated by backtest with vs without gate (BL "Defaults that are assumptions"). Qullamaggie index NIFTY 500 (Nifty Smallcap 250 is the alternative): validated by backtest.
- **[OWNER-PENDING]** "both rising" lookback for the Qullamaggie rule: default 1 session; docs silent; owner to confirm.

## §6 S1 default config: `stage2-contraction-breakout`

End-of-day scan for Stage 2 stocks with a tightening base, entry on an intraday pivot break with volume, stop under the last contraction. Trend Template rules are hard; RS and VCP quality are scored, and the scan-day close may not be above the VCP pivot (HAT-CRIT-123). The seed is this YAML, verbatim from BL "Minervini-style bot - default config" except the `close_vs_pivot` line (PR 54, plan audit N2):

```yaml
bot: stage2-contraction-breakout      # Minervini-style
version: 1
market: NSE
execution_mode: shadow                # shadow | confirm | auto
# capital allocation: set in the account risk policy

universe:
  - {type: series_in, params: {series: [EQ, BE]}}
  - {type: min_turnover, params: {days: 20, inr_cr: 10}}
  - {type: min_price, params: {inr: 50}}

regime:
  index: NIFTY 500
  rule: close_above_sma_and_rising    # assumption; Minervini states no fixed index rule
  params: {sma: 50, rising_days: 10}

setup:
  threshold: 0.6
  criteria:
    - {type: sma_above, mode: hard, params: {a: close, b: 150}}
    - {type: sma_above, mode: hard, params: {a: close, b: 200}}
    - {type: sma_above, mode: hard, params: {a: 150, b: 200}}
    - {type: sma_rising, mode: hard, params: {sma: 200, lookback_days: 21}}
    - {type: sma_above, mode: hard, params: {a: 50, b: 150}}
    - {type: sma_above, mode: hard, params: {a: 50, b: 200}}
    - {type: sma_above, mode: hard, params: {a: close, b: 50}}
    - {type: pct_from_52w_low, mode: hard, params: {min_pct: 30}}
    - {type: pct_from_52w_high, mode: hard, params: {max_pct_below: 25}}
    - {type: rs_rank, mode: hard, params: {min_percentile: 70}}
    - {type: rs_rank, mode: score, weight: 1, params: {score_from: 70, score_full: 95}}
    - {type: vcp, mode: score, weight: 2, params: {contractions: [2, 6], depth_ratio_max: 0.75,
        final_depth_max_pct: 10, lookback_days: 120, dryup_vs_avg50: 0.7}}
    - {type: close_vs_pivot, mode: hard, params: {max_above_pct: 0}}   # close at or below the pivot
    - {type: earnings_growth, mode: hard, enabled: false, params: {eps_yoy: 20, sales_yoy: 20}}

trigger:
  - {type: price_breaks_level, params: {level: pivot, bar: 5m}}
  - {type: volume_pace, params: {vs_avg50: 1.5}}

entry_filters:
  - {type: max_chase, params: {pct: 5}}

stop: {rule: below_final_contraction_low, buffer_pct: 0.5, max_pct: 8}   # skip trade if > 8%

# sizing: from the account risk policy (0.25% risk, 20% max position, 6 positions)

exits:                                  # evaluated top to bottom
  - {rule: hard_stop}
  - {rule: move_stop_to_breakeven, at_r: 2}
  - {rule: partial_at_r, at_r: 3, sell_fraction: 0.33}
  - {rule: close_below_sma, sma: 50, sell_fraction: 1.0, when: "15:25"}
  - {rule: time_stop, enabled: false, days: 15, min_r: 1}
```

#### HAT-BOT-060: S1 seed equals the documented config
- **Statement:** The S1 seed `stage2-contraction-breakout` v1 loads, validates and equals the YAML above field for field.
- **Source:** BL "Minervini-style bot - default config"; B3-B7, B14, PL1; plan audit 2026-10-22 N2 (`close_vs_pivot`).
- **PR:** 8, 54
- **Verification:** CONF `bot_seed_s1_matches_spec`
- **Acceptance:**
  - Property: the parsed seed file and the YAML block in this section are equal as data (every key, value, order of list items and `enabled` flag), and both validate against the criterion schemas. Harm: the bot that runs is not the bot the owner approved.
- **[A]** breakeven / partial / trail: defaults 2R / 3R / 50-day SMA; validated by backtest variants (BL "Defaults that are assumptions").

#### HAT-BOT-061: New bots ask for confirm mode and run shadow until Live
- **Statement:** Both seeded bots, and any newly created bot, start with `execution_mode: confirm` in research, so they run shadow (HAT-PORT-063, HAT-GOV-085). The seed configs' `execution_mode: shadow` is not read for a bot's mode (07 HAT-EXEC-011 note).
- **Source:** BL bot configs; MA "Execution modes"; B2 (revised); D130, D132.
- **PR:** 8, 45
- **Verification:** CONF `bot_seed_default_mode`, PBT `config.new_bot_mode_confirm`
- **Acceptance:**
  - Property: for every bot created through the config service with no explicit mode, the stored mode is `confirm` and the lifecycle state research, any other requested mode is refused, and both seeds declare `shadow`. Harm: a new bot could place real orders before it has a shadow record.

#### HAT-BOT-062: S1 skips trades whose stop is more than 8% below entry
- **Statement:** With `below_final_contraction_low`, the stop is the final contraction low less a 0.5% buffer, and the trade is skipped if that stop is more than 8% below entry.
- **Source:** BL "Minervini-style bot" (`stop:` line); BL "Stops and exits / Stop rules".
- **PR:** 11
- **Verification:** PBT `stops.final_contraction_low`
- **Acceptance:**
  - Property: for every (entry, final contraction low L < entry), stop = L × (1 − 0.005) and the signal is skipped with a reason iff (entry − stop) / entry > 0.08. Harm: the bot takes trades whose stop is too wide for the 0.25% risk budget to buy a meaningful position.

## §7 S2 default config: `momentum-breakout-ep`

One bot, two playbooks (breakout and episodic pivot) sharing one regime gate, capital pool, sizing and exit stack. Both enter above the 5-minute opening-range high with the stop at the low of the day, and skip any trade whose stop is wider than the stock's average daily range. Parabolic short is excluded (long only). The seed is this YAML, verbatim from BL "Qullamaggie-style bot - default config":

```yaml
bot: momentum-breakout-ep             # Qullamaggie-style
version: 1
market: NSE
execution_mode: shadow
# capital allocation: set in the account risk policy

universe:
  - {type: series_in, params: {series: [EQ, BE]}}
  - {type: min_turnover, params: {days: 20, inr_cr: 10}}
  - {type: adr_min, params: {days: 20, pct: 4}}

regime:
  index: NIFTY 500                    # assumption; Nifty Smallcap 250 is the alternative
  rule: sma_fast_above_slow_both_rising
  params: {fast: 10, slow: 20}

playbooks:
  - name: breakout
    setup:
      threshold: 0.6
      criteria:
        - {type: momentum_rank, mode: hard, params: {windows: [21, 63, 126], top_pct: 2, match: any}}
        - {type: prior_move, mode: hard, params: {min_pct: 30, within_days: [21, 63]}}
        - {type: consolidation, mode: score, weight: 2, params: {days: [5, 15], max_range_pct: 15, higher_lows: true}}
        - {type: ma_surf, mode: score, weight: 1, params: {mas: [10, 20], max_distance_pct: 5}}
    trigger:
      - {type: price_breaks_level, params: {level: "max(consolidation_high, opening_range_high)", bar: 5m}}
      - {type: volume_pace, params: {vs_avg50: 1.5}}
    entry_filters:
      - {type: adtr_filter, params: {max_multiple: 1.0}}
      - {type: max_chase, params: {pct: 2}}
    stop: {rule: low_of_day}

  - name: episodic_pivot
    setup:                             # built live from the pre-open scan, not the nightly watchlist
      criteria:
        - {type: neglect, mode: hard, params: {max_return_63d_pct: 30}}
    trigger:
      - {type: gap_up, params: {min_pct: 10}}
      - {type: early_volume, params: {vs_adv20: 0.5}}
      - {type: catalyst_attached, params: {within_hours: 24, required_in: [confirm]}}
      - {type: price_breaks_level, params: {level: opening_range_high, bar: 5m}}
    entry_filters:
      - {type: adtr_filter, params: {max_multiple: 1.5}}
      - {type: max_chase, params: {pct: 2}}
    stop: {rule: low_of_day}

# sizing: from the account risk policy (0.25% risk, 25% max position, 6 positions)

exits:
  - {rule: hard_stop}
  - {rule: partial_after_days, days: 3, sell_fraction: 0.33, only_if_profit: true}
  - {rule: move_stop_to_breakeven, after: partial}
  - {rule: close_below_sma, sma: 10, sell_fraction: 1.0, when: "15:25"}   # 20 for slower stocks
```

#### HAT-BOT-070: S2 seed equals the documented config
- **Statement:** The S2 seed `momentum-breakout-ep` v1 loads, validates and equals the YAML above field for field, with playbooks `breakout` and `episodic_pivot`.
- **Source:** BL "Qullamaggie-style bot - default config"; B3-B5, B8-B10, PL1.
- **PR:** 8
- **Verification:** CONF `bot_seed_s2_matches_spec`
- **Acceptance:**
  - Property: the parsed seed file and the YAML block in this section are equal as data, including the `level: max(consolidation_high, opening_range_high)` expression and `required_in: [confirm]`, and both validate. Harm: the bot that runs is not the bot the owner approved.
- **[A]** ADR 4%, neglect 30%, early volume 0.5×: validated as listed in HAT-CRIT-104, HAT-CRIT-120 and HAT-CRIT-117.

#### HAT-BOT-071: The breakout level expression resolves to the larger level
- **Statement:** The breakout playbook's trigger level is max(consolidation_high, opening_range_high), and it is not defined before the opening range is final at 09:20.
- **Source:** BL "Qullamaggie-style bot" (breakout trigger); MA "Daily cycle" step 4.
- **PR:** 15
- **Verification:** PBT `criteria.level_expression`
- **Acceptance:**
  - Property: for every (consolidation_high, opening_range_high), the resolved level equals the larger of the two, and for every time before 09:20 IST on the trade date the level is undefined and no trigger fires. Harm: the bot buys inside the opening range, under its own breakout level.

#### HAT-BOT-072: Strategy configs hold no risk numbers
- **Statement:** Neither seed, nor any saved strategy version, contains risk %, position share, position count or capital; the comments in the YAML point at the risk policy (0.25% risk; 20% max position and 6 positions for S1; 25% max position and 6 positions for S2).
- **Source:** BL "Summary"; BL "Risk policy is account-level"; SCH "Strategy, risk policy and bots"; B13-B15, B21.
- **PR:** 8, 10
- **Verification:** PBT `config.no_risk_keys`, CONF `risk_policy_seed_bot_limits`
- **Acceptance:**
  - Property: for every generated strategy config containing any risk key, validation rejects it. Harm: a strategy edit could raise risk without the cooling-off gate.
  - Property: the initial risk-policy seed (06 §1) holds p_max 0.20 for S1 and 0.25 for S2 and max positions 6 for both. Harm: the bots size against limits that differ from the approved design.

## §8 Versioning, config_hash, YAML import/export

#### HAT-BOT-080: Every save creates a new version; published versions are immutable
- **Statement:** Saving a strategy config creates a new `hat_strategy_version` row (draft) with `parent_version_id` set, and a version whose status is not `draft` never changes its `config`.
- **Source:** BL "Configuration model / Editing rules"; BL "Summary" (Versioned); SCH "Governance enforced in the database"; B5, T10.
- **PR:** 2, 8
- **Verification:** PBT `config.save_creates_version`, DB `governance.version_config_immutable`
- **Acceptance:**
  - Property: for every sequence of saves, the version numbers of a strategy are 1, 2, 3, ... without gaps or duplicates, and each new row's `parent_version_id` is the version it was edited from. Harm: two configs share a version number and trades cannot be explained.
  - Property: for every non-draft version, an UPDATE of `config` is rejected by the database. Harm: a live strategy changes under open trades.

#### HAT-BOT-081: `config_hash` is the sha256 of canonical JSON
- **Statement:** `config_hash` is the sha256 of the config serialised as canonical JSON (sorted keys, no insignificant whitespace), so equal configs hash equally.
- **Source:** SCH "Summary and conventions" (Config); T10.
- **PR:** 8
- **Verification:** PBT `config.hash_canonical`
- **Acceptance:**
  - Property: for every config and every reordering of its mapping keys, the hash is unchanged; for every two configs that differ in any value or list order, the hashes differ (up to sha256 collision). Harm: identical strategies look different, or a changed strategy looks unchanged, breaking trial counting and audit.

#### HAT-BOT-082: YAML import and export round-trip losslessly
- **Statement:** Exporting a version to YAML and importing it back yields an equal config and the same `config_hash`.
- **Source:** MA "Decisions register" (T10 criteria versioned in DB, YAML import/export).
- **PR:** 8
- **Verification:** PBT `config.yaml_round_trip`
- **Acceptance:**
  - Property: for every valid config, import(export(config)) == config and hash(import(export(config))) == hash(config). Harm: editing a bot through YAML silently alters it.

#### HAT-BOT-083: Signals and trades record the version they ran under
- **Statement:** Every watchlist row and signal records its `strategy_version_id`, and open trades keep managing under the version they entered with after a new version goes live.
- **Source:** BL "Summary" (Versioned); BL "Editing rules"; SCH "Runtime"; B5.
- **PR:** 11, 15
- **Verification:** PBT `config.trade_keeps_entry_version`, TLA `TradeLifecycle!VersionPinned`
- **Acceptance:**
  - Property: for every sequence of promotions during an open trade, the exits and stop rules applied to that trade are those of the signal's `strategy_version_id`. Harm: a config change rewrites the exit plan of a trade already open.

#### HAT-BOT-084: Strategy edits happen outside market hours as drafts through the release gate
- **Statement:** A strategy change can be saved only outside market hours as a draft, and it reaches live only through backtest, then 20 trading days of shadow, then promotion.
- **Source:** BL "Governance - who can change what"; BL "Editing rules"; SCH "Governance enforced in the database"; B5; design review 2026-10-22 item 8 (nothing moved a version past `backtested` or set `hat_bot.live_version_id`).
- **PR:** 8, 9, 12, 35
- **Verification:** TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`, PBT `config.release_gate`
- **Acceptance:**
  - Property: in every reachable state, a version with status `live` has a non-null `backtest_run_id` and `shadow_started_at`, and the app records at least 20 trading days between `shadow_started_at` and `promoted_at`. Harm: an untested edit trades real money.
  - Property: for every save request whose timestamp falls inside the market session of a trading day, the save is refused. Harm: live behaviour changes mid-session.
- **[A]** shadow period: default 20 trading days; validated by live review (BL "Governance").
- **Implementation (PR 35):** `engine/strategy/versions.py` holds the gate. `start_shadow` moves a backtested version (one with its run) to `shadow` and sets `shadow_started_at`; `promote_version` makes a shadow version `live` with `promoted_at`, only outside market hours and after at least 20 trading sessions of `hat_trading_calendar` after the shadow start's trade date; it retires the strategy's previous live version first, moves the sleeves that ran it onto the new one, and records `strategy.promoted` (`replaced`, `bots`, the owner's chat id, `via = cli`). `assign_version` sets a sleeve's `live_version_id` to a released version of its strategy; a Live or Probation sleeve takes only a live version. A refusal changes nothing. The owner runs them through `python -m app.hat.admin` (10 §9, HAT-OPS-091).
  - Property (PBT `config.release_gate`): for every lifecycle state of a sleeve, shadow length and time of day, with or without a live version to replace, promotion happens exactly when the version is in shadow, the market is closed and 20 sessions have passed; then the strategy has one live version, the replaced one is retired, its sleeves run the new one and one `strategy.promoted` is written; otherwise nothing changes. A Live or Probation sleeve is never pointed at a version that is not live. Harm: an untested edit trades real money, or two live versions split the sleeves.
