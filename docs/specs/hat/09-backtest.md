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
- **Verification:** PBT `backtest.survivorship_free`, PBT `backtest.market_point_in_time`
- **Acceptance:**
  - Property: for every generated history containing an instrument delisted at date x, the instrument is eligible on dates < x where it was a universe member, and its open positions are closed no later than its last traded bar. Harm: dead stocks vanish from the test and returns are overstated.
- **Design (PR 12):** an instrument's last traded session is its last bar on or before its `delisted_on`, and only an instrument delisted by the period's end has one (a later delisting is not visible to the run). A position still open on that session is closed at its close with exit reason `delisted`, so a delisted stock's loss stays in the results. Industry classifications are loaded with their effective dates and read as of each trade date, so a later reclassification never changes an earlier industry-cap decision. Positions still open after the period's last date close at each instrument's newest close on or before that date (an instrument need not trade on it), with exit reason `end_of_period`.

#### HAT-BT-013: A run pins one strategy version and one risk-policy version
- **Statement:** Each backtest run records and uses exactly one `strategy_version_id` and one `risk_policy_version_id` for its whole period.
- **Source:** SCH "Backtest and ops tables" (`hat_backtest_run`); BL "Summary" (Versioned).
- **PR:** 12
- **Verification:** PBT `backtest.version_pinned`
- **Acceptance:**
  - Property: for every run, every simulated signal and trade carries the run's strategy and risk-policy version IDs. Harm: evidence cannot be tied to the version being promoted.

## §2 Fill and cost model

Daily bars cannot show where in the day a pivot broke. The trigger proxy below is a documented approximation; the gap between it and live fills is measured in shadow mode (PLAN Q4; MA "Risks and open items").

Each simulated day is read as one path: **open, then low, then high, then close** (the adverse move first). At the open the nightly rules and then the live rules see the open price, so a gap through a stop exits at the open. On the way down a stop fills by HAT-BT-021; on the way up each profit rule (`partial_at_r`, `pct_gain_target`, a raised stop) fires at the first price at which `exits.evaluate` acts, as many times as the stack keeps acting; an entry breaks its level on the way up or at the open (a gap over); on the entry day the low is split around the break (HAT-BT-020 design, PR 60). The close rules then see the official close. The fill proxies are pure functions in `app/hat/backtest/fills.py`; whether an entry is taken is `evaluate_signal`'s decision and which exit fires is `exits.evaluate`'s.

#### HAT-BT-020: Entry fill on daily bars
- **Statement:** On daily bars, an entry fills at the open when the day opens above the trigger level (gap over); otherwise, under the default rule `penetration_1_tick`, at the first price of the instrument's tick grid above the level (level + tick for a level on the grid) when the day's high reaches it, and under the variant `touch` at the level when the high reaches the level; otherwise no entry.
- **Source:** PLAN Q4; BL "Criteria library" (`price_breaks_level`, `max_chase`).
- **PR:** 12, 60, 62, 63
- **Verification:** PBT `backtest.entry_fill_proxy`, PBT `backtest.entry_fill_proxy_chase`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.fill_conventions_recorded_digest`, PBT `backtest.stop_on_instrument_tick`, PBT `backtest.pre_break_low`, PBT `backtest.entry_day_stop`, PBT `backtest.contraction_stop_too_wide`
- **Acceptance:**
  - Property: for every (open, high, level, tick), the entry price is open if open > level; otherwise, under `penetration_1_tick`, the smallest tick-grid price above the level (in (level, level + tick]) if the high reaches it, and under `touch` level if high >= level; else no entry. Penetration never fills where touch does not, nor below touch's price, and without a tick there is no penetration entry. In a run, under either rule, the entry is still subject to max-chase, so a gap-over open above level × (1 + pct/100) is skipped. Harm: the backtest buys breakouts live never sees trade, at a better price than live pays.
  - Property: every run's metrics record `fill_path` (`open_low_high_close`), `entry_fill_rule`, `gap_through_rule` (`min_open_stop`), `trigger_proxy` (`daily_bar_level_break`), `slippage_bps` (normalised, so 20 and 20.0, or -0 and 0, record alike), `cost_model`, `slot_order` (HAT-BT-026, PR 63) and `for_promotion`, and `config_digest` is the canonical hash (`confighash.config_hash`) of exactly the first seven; two runs' digests are equal iff their entry rule, slippage, cost model and slot order are. Harm: runs under different fill conventions read as comparable evidence.
- **Decision (PR 62, O5; owner defaults 2026-10-22, D154):** the daily-bar trigger proxy (`daily_bar_level_break`) is the convention for S1 and S2; the episodic-pivot playbook is backtested on 5-minute bars only (`bars_5m`, HAT-DATA-119), with at least five years of them (Fyers Prime, from 2017-07-03; D154 drops the one-year EP exception O5 had proposed); the discrepancy from live is measured in shadow. The entry rule is `penetration_1_tick` by default: live's `price_breaks_level` needs a trade strictly above the level (HAT-CRIT-119) and enters at that price, so the backtest's first such price is the first price on the instrument's tick grid (HAT-EXEC-053, the tick on the trade date) above the level: level + tick for a level on the grid, the next grid price for an adjusted level between two, and the high must reach it. `touch` (the PR 12 rule) stays as a variant for sensitivity runs (`--entry-fill touch`, `RunSpec.entry_fill_rule`); a `touch` run adds `entry_fills_on_touch` to its `known_gaps`. An instrument with no close before the trade date has no tick, so a day whose high goes above the level (a trigger that would need a penetration) is skipped (`stop_input_missing`, as its stop needs the tick too); a day whose high stays at or below the level is no trigger and counts no skip. The entry-day split (PR 60) breaks at the entry price: the first 5-minute bar whose high reaches it. Under `touch` an open exactly at the level stays a gap over, as PR 60 defined it (the level breaks on the first tick); under penetration such an open is not one (the high must still clear the level).
- **Deviation (PR 12):** the trigger is the break of the level alone. `volume_pace` and the opening range need intraday bars and are not modelled (`adtr_filter` is, since PR 61: HAT-BT-025), and the episodic-pivot playbook, built live from the pre-open gap scan, is not backtested (its rows are skipped): the PR 12 loop runs on daily bars only, and the minute-history loader exists for a later EP backtest. Both are listed in `metrics.known_gaps`.
- **Design (PR 12):** the initial stop is planned with the instrument's NSE tick on the trade date (`data.ticks.tick_for_date`, HAT-EXEC-053), as the live trigger plans it; an instrument with no close before the trade date is skipped (`stop_input_missing`). The backtest reads the tick from the adjusted closes it runs on, while live reads the unadjusted ones: across a corporate action the two can sit in different price slabs, which moves a stop by less than the larger of the two ticks. Property `backtest.stop_on_instrument_tick`: every opened entry's stop is the live plan on the instrument's tick of the trade date, with prices in more than one slab. Harm: backtest stops sit on a grid the live trigger never uses.

- **Design (PR 60):** the entry day is split at the break (`engine.backtest.pre_break_low`). A low-of-day stop (HAT-EXEC-051) is planned from the day's low before the break, never the whole day's low, which live cannot know when the trigger fires:
  - With the session's 5-minute bars (`MarketData.intraday`, from `bars_5m`, on the daily bars' scale): the lowest low of the bars up to and including the first one whose high reaches the level (the trigger bar's low counts before the break, since the stop is planned from it); the lowest low of the bars after it is the low after the entry, and a day low the bars miss counts as after it.
  - Without them: the convention `max(day low, min(open, level × (1 - adr20/100)))`, with adr20 at the previous close; the day low counts as after the entry only when it is below that estimate. With no adr20 a low-of-day stop has no estimate and the trigger is skipped (`stop_input_missing`); other rules then take the whole day's low as before the break. `metrics.entry_day_low` records the convention (`bars_5m_else_max_low_min_open_level_less_adr20`).
  - A gap over (an open at or above the level, an open exactly at it included: the level breaks on the first tick, so nothing comes before the break) enters at the open, so its stop needs the opening range's low: without 5-minute bars a gap-over entry under a low-of-day stop is skipped (`gap_over_no_intraday`); with them the low before the break is the first bar's and the low after it the later bars'.
  - After the entry the position sees the low after the break (for a gap over under any other rule, the whole day's low) before the high: a stop it reaches fills that day by HAT-BT-021 (recorded at 09:45, after the 09:30 entry), and it seeds the MAE.
  - A contraction stop the scan left empty (the stop at the pivot is too wide, or there is no contraction low) skips the trigger as `stop_too_wide`, not `stop_input_missing`.
  - Property `backtest.pre_break_low`: the split on generated 5-minute bars and on the daily estimate. Property `backtest.entry_day_stop`: under a low-of-day stop, with and without generated 5-minute bars, every entry's stop is the live plan from the low before the break, gap-over skips are counted, and an entry-day low at or below the stop exits that day at -1 R (MAE -1), while a higher one bounds the MAE. Property `backtest.contraction_stop_too_wide`: an empty contraction plan skips as `stop_too_wide`. Harm: an S2 stop sits at a low the market made after the entry, and the entry day's loss is never seen.
- **Deviation (PR 60):** the backtest market loader (`backtest.market.load_market`) does not read `bars_5m` yet, so a CLI run has no 5-minute bars and every S2 gap-over entry is skipped; loading them is a follow-up. HAT-EXEC-051 (spec 07) is unchanged: the backtest reads it as the lowest low of the bars up to the entry bar.

#### HAT-BT-021: Stop and close-based exit fills
- **Statement:** A stop fills at the stop price, or at the open when the day opens below the stop; close-based exits (`close_below_sma`, `time_stop`, `partial_after_days`, `trailing_swing_low`) fill at the day's official close.
- **Source:** BL "Stops and exits"; MA "Failure handling" (close vs live price); BL "Defaults that are assumptions" (exit at 15:25 vs official close); PLAN Q10.
- **PR:** 12, 62
- **Verification:** PBT `backtest.exit_fill_proxy`, PBT `backtest.exit_fill_proxy_close`, PBT `backtest.fill_conventions_recorded`
- **Acceptance:**
  - Property: for every (open, low, stop) on a held day, the stop fill price is min(open, stop) when low <= stop, and the fill is never better than the stop. Harm: gap-down losses are understated.
  - Property: for every close-based exit, the fill price equals that day's close, and the difference from a 15:25 live price is recorded as a known gap, not corrected. Harm: the measured gap between backtest and live exits is hidden.
- **Decision (PR 62, O5):** a stop gapped through fills at min(open, stop) (`gap_through_rule` = `min_open_stop`, recorded in every run). The day's path is open, then low, then high, then close (`fill_path` = `open_low_high_close`), the adverse move first.
- **Design (PR 62, N14):** the search for the first price on the day's way up at which a live rule acts (`engine.backtest.first_trigger`) is a bisection over the tick grid, which assumes a monotone rule stack: above the stop, a rule that acts at a price acts at every higher one. The built-in rules are thresholds (profit targets, R levels, breakeven moves); `partial_after_days` and the breakeven moves of the close phase see one price. A custom rule that acts only inside a band of prices would need a linear scan instead.

#### HAT-BT-022: Slippage
- **Statement:** Every simulated fill is moved against the trade by a configured slippage in basis points.
- **Source:** PLAN PR 12 (fill and cost model: slippage bps); BL "Defaults that are assumptions" (liquidity cap validated by slippage in shadow mode).
- **PR:** 12, 62
- **Verification:** PBT `costs.slippage_adverse`, PBT `costs.slippage_adverse_net`, PBT `costs.slippage_adverse_bounds`, PBT `backtest.fill_conventions_recorded`
- **Acceptance:**
  - Property: for every fill price p > 0 and slippage s with 0 <= s < 10000 bps (a configured value outside this range is rejected when the run starts), a buy fills at p × (1 + s/10000) rounded up to 4 places and a sell at p × (1 − s/10000) rounded down to 4 places, never below 0.0001, so net P&L is monotone non-increasing in s. Harm: the backtest assumes perfect fills and overstates edge.
- **Design (PR 12):** prices are stored to 4 places, and rounding never favours the trade: a buy's slipped price is rounded up, a sell's down, and a sell never fills below 0.0001. The slippage is a Decimal checked when the run starts (`cost_bad_slippage` otherwise); the command line takes `--slippage-bps` (default 20, `costs.DEFAULT_SLIPPAGE_BPS`) and every run's metrics record it.
- **Decision (PR 62, O5):** the slippage defaults to 20 bps, recalibrated from shadow-mode slippage (PR 40) through a new default, never per run silently. A run flagged for promotion (`--for-promotion`, `RunSpec.for_promotion`) is refused at a slippage of 0 (`backtest_promotion_without_slippage`) and under the optimistic `touch` rule (`backtest_promotion_on_touch`): `engine.backtest.check_conventions` is called by `backtest_bot` before the run is recorded (recording nothing) and by `run_backtest`. A slippage of -0 records and hashes as 0. `publish_version` takes only a run whose metrics say `for_promotion: true` (HAT-BT-061), so a version is never promoted on perfect fills. Property `backtest.fill_conventions_recorded`.

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

#### HAT-BT-025: Entry filters at the trigger
- **Statement:** At each trigger the loop evaluates the playbook's entry filters as live does (`trigger.evaluate_entry_filters`, with `max_chase` always present): `adtr_filter` on the planned stop and ADTR(20) as of the previous session, and `max_chase` with the upper-circuit state. A trigger whose filter fails is recorded Rejected with the filter's type as the reason and buys nothing.
- **Source:** BL "Criteria library" (`adtr_filter`, `max_chase`); HAT-CRIT-121, HAT-CRIT-122; audit 2026-10-22 G05, G10.
- **PR:** 61
- **Verification:** PBT `backtest.adtr_filter_applied`, PBT `backtest.upper_circuit_inferred`, PBT `backtest.upper_circuit_skip`
- **Acceptance:**
  - Property: with an `adtr_filter`, a trigger whose stop is wider than max_multiple × ADTR(20) as of the previous session, or that has no ADTR, is Rejected `adtr_filter` and buys nothing; one within it is never refused for that reason. Harm: the backtest takes wide-stop entries live refuses.
  - Property: an entry price is at the upper circuit when it is the day's high, above the previous close, and within one tick of the upper price (`trigger.upper_circuit_price`) of an NSE band (2, 5, 10 or 20%) whose upper price is above the previous close; an entry below the high, at or below the previous close, or between bands is not; with no previous close or tick the state is unknown. Harm: the backtest buys a stock locked at its upper band.
  - Property: in a run, a trigger whose entry is at the band, on a bar pinned there all day or one that opened at the band and faded, is Rejected `max_chase` and buys nothing, and with a wide chase no other trigger is. Harm: the backtest buys locked gap-ups live never gets.
- **Design (PR 61):** the run has no price-band table, so the upper circuit is inferred from the entry price and the bar (`engine.backtest.upper_circuit_locked`, with live's `trigger.upper_circuit_price` per band). Live refuses any entry priced at the band (HAT-CRIT-122), so a gap over that opens at the band and then fades is refused too. A band whose floored upper price is not above the previous close (a low-priced stock) is skipped. A name with a wider band entered at exactly a narrower band's price reads as locked; this is listed (`upper_circuit_inferred_from_bar`, HAT-BT-070). A filter rejection is a Rejected signal, as live writes it, and is counted in `metrics.rejections`; a stop-rule skip stays a loop skip (`metrics.skips`).
- **Deviation (PR 61):** the filter loop was moved from `trigger.plan_entry` into `trigger.evaluate_entry_filters` (with `trigger.entry_filter_context`) so the backtest calls the code live calls without importing `engine.criteria` (HAT-BT-010); `plan_entry` is unchanged in behaviour.

#### HAT-BT-026: Slot order on a capped day
- **Statement:** On each simulated day the watchlist rows reach the trigger, and so `evaluate_signal` and its position caps, in the run's slot order. The default `time` is the estimated trigger order: every gap over (an entry at the open: an open above the level, or at it under `touch`, HAT-BT-020) first, then the rows whose level breaks during the day in ascending (level − open) / open, ties in score order (score descending, then playbook, then instrument). `score` (the PR 12 order) and `random:K` (the score order shuffled by a generator seeded by K and the day) are variants for sensitivity runs. On a capped day (more triggers than open slots) the earlier triggers take the slots.
- **Source:** audit 2026-10-22 G11 (strategy research report, finding 11: live resolves a day's competition for slots by trigger time, the backtest by score).
- **PR:** 63
- **Verification:** PBT `backtest.slot_order_rank`, PBT `backtest.slot_order_refused`, PBT `backtest.slot_order_applied`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.fill_conventions_recorded_digest`
- **Acceptance:**
  - Property: for any candidates (open, level) in score order and either entry rule, `time` puts every gap over before every in-day break, those in ascending (level − open) / open, a row with no level or no positive open last, and keeps the score order among ties; `score` keeps the order; `random:K` is a permutation that the same K and day reproduce. Harm: on a capped day the backtest gives the slots to the best-scored setups, which live gets only when they also break first, so the evidence is better than live can trade.
  - Property: a slot order other than `time`, `score` or `random:K` (K a whole number below 10^9 in ASCII digits, no leading zeros) is refused (`backtest_bad_slot_order`) before the run, so one rule has one spelling and one digest, and a run flagged for promotion is refused under any order but `time` (`backtest_promotion_off_time_order`). Harm: two spellings of one rule hash apart, an unknown order reads as the default, or a version is promoted on a slot allocation live cannot get.
  - Property: in a run with one open slot, each day's triggers reach `evaluate_signal` in the run's slot order computed from the day's watchlist and bars, so no trigger takes the slot after an earlier one that day was refused `max_positions`; the run records the rule (`metrics.slot_order`) and `config_digest` hashes it. Harm: the backtest fills a capped day's slot with a later break than live would take.
- **Design (PR 63, G11):** the order is `app.hat.backtest.slots` (`trigger_rank`, `order_slots`), applied by the loop to the day's watchlist before the triggers (`_slots`); a trigger's recorded instant follows its place in the order. The distance is relative to the open, so a day's breaks are ordered by how far each price had to travel; daily bars do not show the path, so this is an estimate, and a gap over's fill at the open is the listed gap `gap_over_fills_at_open`. Under `penetration_1_tick` an open exactly at the level is a break during the day at distance 0 (it needs a trade above the level, HAT-BT-020), so it follows every gap over; under `touch` it fills at the open and is a gap over. The order applies on every day; on a day with free slots for every trigger it changes only the instants. `--slot-order` (default `time`) and `RunSpec.slot_order` select the rule; `backtest_bot` and `run_backtest` refuse an unknown one through `check_conventions`, and the command line refuses it before loading anything. A run flagged for promotion must use `time` (`backtest_promotion_off_time_order`), and the sleeve's evidence (HAT-BT-060) reads only runs whose `metrics.slot_order` is `time`: every run before PR 63 took the slots by score, so an existing sleeve needs one `--for-promotion` run under PR 63 before its gates see evidence again. The estimate ignores the day's low: on the open → low → high path a break after a deep dip comes later than its distance from the open says. It is kept as the plan's daily-bar proxy (G11); 5-minute bars (PR 70) give the actual order.
- **Decision (PR 63):** `random:K` is one seeded permutation per day, not K runs: a sensitivity study runs K = 0, 1, … as separate runs, each a trial of its family (HAT-BT-030). The generator is seeded by the text `K:<date>`, so a day's order does not depend on the days before it and a run is deterministic (HAT-BT-050).

## §3 Runs, family_key and trial counting

#### HAT-BT-030: Every run increments the trial count of its family
- **Statement:** Every backtest run, whatever its outcome, is counted as one trial of its `family_key` (strategy × universe), and the family's trial count is the number of runs recorded for it.
- **Design (PR 12):** a run is recorded with status `running` before the engine starts, then ends `completed` (metrics and trades written) or `failed` (`metrics = {"error": <code>}`, a fixed code, never exception text). A failed run stays a trial; the command line commits the failed row before it reports the refusal. Refusals before a run is recorded (unknown bot or version, no universe, no policy in force, a bad cost model or slippage, an unknown entry fill rule, a run for promotion at a slippage of 0 or under `touch` (PR 62), no data) record nothing.
- **Source:** BL "Sleeve lifecycle" (every backtest run is counted as a trial); SCH "Portfolio layer" (`hat_backtest_run` gains `family_key`); PL4.
- **PR:** 12, 62
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
- **Design (PR 12):** `data_snapshot` records the sessions read, the row counts, the index, the corporate-action digest and `content_digest`, a sha256 of the bars, features, index closes, membership intervals and industry history the run read, so two snapshots with equal counts but different data differ. The trades file is written atomically under `HAT_DATA_ROOT`. On completion `metrics.dsr` stores the deflated Sharpe probability at that moment as `{prob (6 places, text), sr0, reason, trials, variance}`; the lifecycle recomputes it at evaluation time (HAT-BT-060). `metrics.returns` stores the daily-return Sharpe ratio, count, skewness and kurtosis the DSR reads.
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
- **PR:** 9, 12, 62, 63
- **Verification:** PBT `lifecycle.backtest_gate`, PBT `dsr.evidence_current_trials`, PBT `dsr.evidence_promoted_only`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- **Acceptance:**
  - Property: for every evidence record, the guard result is (dsr_prob >= 0.95 and trades >= 200), and the evidence always carries the family trial count current at evaluation time. Harm: a sleeve passes the gate on stale trial counts.
- **[A]** DSR >= 0.95 and >= 200 trades: defaults; validated by owner review of the first sleeves' evidence.
- **Design (PR 12):** `app.hat.backtest.runs.backtest_evidence` is the lifecycle's `BacktestEvidence` source (05 §6): the newest completed run of the sleeve's live version on its family, with the DSR recomputed at the family's trial count and Sharpe variance now (failed runs and other versions of the strategy count), its trade count and expectancy, as `BacktestFacts`. With no such run there is no evidence and Research → Backtested stays closed. The same runs feed the risk-increase gate (06, HAT-GOV-091): `runs.gate_evidence` builds `gate_evidence` from the user's closed trades against the trade-weighted mean expectancy of the sleeves' newest completed runs, discounted by the lifecycle's backtest discount (`DEFAULT_DISCOUNT` 0.58, HAT-PORT-065, owner-pending); with no completed run it gives none and the increase is refused.

- **Design (PR 62, O5):** the evidence, for the lifecycle (`backtest_evidence`) and for the risk-increase gate (`gate_evidence`, HAT-GOV-091), is read only from runs flagged for promotion (`metrics.for_promotion`): a sensitivity run (`touch`, 0 bps, a sweep) or a run recorded before the flag existed is never a sleeve's evidence; since PR 63 neither is a run whose `metrics.slot_order` is not `time` (HAT-BT-026), which covers every run recorded before PR 63. After this PR an existing sleeve needs one `--for-promotion` run before its gates see evidence again (they fail closed until then). Property `dsr.evidence_promoted_only`. Harm: a sleeve is promoted, or its risk raised, on perfect or optimistic fills.

#### HAT-BT-061: Strategy versions record their backtest
- **Statement:** A strategy version leaves `draft` for `backtested` only with a `backtest_run_id` of a completed run of that version flagged for promotion (PR 62: its metrics say `for_promotion: true`, so its slippage is above 0), and status `live` requires both `backtest_run_id` and `shadow_started_at`.
- **Source:** SCH "Governance enforced in the database" (no promotion without evidence); BL "Governance" (draft → automatic backtest → shadow → promote); B5.
- **PR:** 2, 12, 62
- **Verification:** DB `governance.live_requires_evidence`, PBT `backtest.version_status_requires_run`
- **Acceptance:**
  - Property: for every version with status other than `draft`, `backtest_run_id` references a completed run whose `strategy_version_id` is that version and which was flagged for promotion; a running, failed, foreign or unflagged run is refused. Harm: a version is promoted on another version's evidence, or on perfect fills.
  - Property: for every row with status `live`, the database rejects it unless `backtest_run_id` and `shadow_started_at` are not null. Harm: an untested strategy trades.

## §7 Command line and known gaps

`python -m app.hat.engine.backtest --bot <id> --from YYYY-MM-DD --to YYYY-MM-DD [--version <id>] [--slippage-bps N] [--cost-model NAME] [--cooldown-sessions N] [--entry-fill penetration_1_tick|touch] [--slot-order time|score|random:K] [--for-promotion]` backtests a sleeve's live strategy version (or `--version`, another version of the same strategy) on the sleeve's universe, under the risk-policy version in force now, over the Parquet store under `HAT_DATA_ROOT` (the adjusted `bars_daily` view, `features_daily`, the regime index closes and point-in-time membership from the database). It records the run as a trial, writes the trades file and prints the run id, status, trade count and DSR; a refusal prints a fixed code and exits non-zero.

#### HAT-BT-070: Every unmodelled behaviour is a listed gap
- **Statement:** Every run's `metrics.known_gaps` lists, by fixed code and in the order of the table below, each way the daily-bar loop differs from live trading. The gaps are measured in shadow mode (PLAN Q4), not corrected; the lifecycle gate (PR 69) and the confidence label (D132) read the codes.
- **Source:** PLAN Q4; audit 2026-10-22 N9, G32, N8.
- **PR:** 12, 61, 62
- **Verification:** CONF `known_gaps_listed`, PBT `backtest.deterministic`, PBT `backtest.fill_conventions_recorded`
- **Acceptance:**
  - Property: the codes of the table below equal `engine.backtest.KNOWN_GAPS`, in order, and every run's metrics carry that list (a `touch` run appends `entry_fills_on_touch`). Harm: a gap the loop has is not reported, so the evidence reads as more faithful than it is.

| Code | What the loop does instead of live | Closed or measured by |
|---|---|---|
| `trigger_is_level_break_only` | the trigger is the break of the level; `volume_pace` and the opening range need intraday bars | PR 70 (`bars_5m`) |
| `episodic_pivot_not_backtested` | the EP playbook's rows are skipped | PR 70 |
| `close_exits_at_official_close` | close exits fill at the official close, not the 15:25 price | shadow (PR 40) |
| `kill_switches_not_simulated` | kill switches never trip (their re-arm is manual); the exposure ladder is simulated | none (by design) |
| `gap_over_fills_at_open` | a gap over fills at the open; live fills at the first tick after it | shadow (PR 40) |
| `upper_circuit_inferred_from_bar` | the upper circuit is an entry at the day's high within a tick of an NSE band's upper price (HAT-BT-025); no band table | a band loader |
| `lower_circuit_not_modelled` | a stop on a locked lower band fills at min(open, stop) | PR 64 |
| `multipliers_fixed_at_one` | regime and health multipliers are 1 when sizing | PR 68 |
| `suspension_not_marked` | a position with no bar for some sessions that is not delisted is carried at its last close | PR 64 |
| `entry_day_low_estimated` | without `bars_5m` the pre-break low is the adr20 estimate and low-of-day gap overs are skipped (HAT-BT-020) | PR 70 |

- **Note (PR 62):** `entry_fills_on_touch` (a high at the level fills at the level; live needs a trade above it) left the table when the default entry rule became `penetration_1_tick` (HAT-BT-020). A run under the `touch` variant still reports it after the table's codes (`engine.backtest.TOUCH_GAP`).
- **Note (N8):** young listings are excluded by design, not by a gap: the 52-week criteria need 252 bars and RS needs 253, so a stock listed under a year ago is never an S1 or S2 candidate, live or in the backtest. It is not a listed gap because live behaves the same way.
