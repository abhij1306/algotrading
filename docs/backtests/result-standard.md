# Backtest Result Standard v1.5

Sections 1 to 6 and the rules are v1.0; the sections v1.1 to v1.5 changes amend them, a
later one winning where they differ. `format_version` is `1.5` (`run.schema.json`,
`example_run.json`). How runs are made and checked: `process.md`.

Every backtest run writes one folder. No exceptions, no partial runs.

```
runs/<run_id>/
  run.json        # identity, config, headline metrics, breakdowns  (schema: run.schema.json)
  trades.csv      # one row per closed trade, with setup + stock features at entry
  equity.csv      # one row per session
  refusals.csv    # one row per blocked entry, with the same features
  signals.csv     # every setup that fired, taken or not
  stocks.csv      # one row per symbol: how S1 did on that stock
  ep_candidates.csv  # pre-open playbook runs only: each gap-scan candidate-day
  overlay.csv     # ETF overlay runs only: one row per session of the sleeve
  rebalances.csv  # rotation runs only: each rebalance's orders and holds
```

Why four files: headline metrics say *whether* a variant is better. Trades, equity and refusals say *why*, and that is what tweak suggestions are built from.

---

## 1. run.json

### identity
- `run_id`: unique, sortable (current hex ids are fine)
- `strategy`: `S1` (Minervini), `S2` (Qullamaggie), and so on
- `strategy_version`: code hash of the strategy (e.g. `aac4fda7`)
- `engine_commit`: git commit of the repo
- `sweep_id`, `sweep_name`, `variant`: e.g. `sweep-07`, `policy v3 on np`, `v3-np`
- `base_run_id`: run this variant is compared against (null for a baseline)
- `change`: one line, plain words (e.g. `+ RS rank ≥ 70`)
- `created_at`, `status` (`complete` | `failed` | `stopped_early`)

### data
- `universe`: e.g. `NIFTY500_PIT`
- `data_snapshot_id`: price data version; reruns must match
- `split`: `in_sample` | `hold_out`
- `period_start`, `period_end`
- `benchmark`: e.g. `NIFTY500_TRI`

### config
- `overrides`: only the parameters changed vs `base_run_id`
- `resolved`: full parameter set as actually run (setup, entry, exits, regime, risk policy)
- `resolved_hash`: hash of `resolved`; two runs with the same hash are duplicates
- `cost_model`: version id of the dated cost table
- `start_capital`

### metrics (all net of costs)
| field | definition |
|---|---|
| `trades` | closed trades |
| `signals` | setups that fired before any risk gate |
| `win_pct` | trades with net R > 0 ÷ trades |
| `avg_win_r`, `avg_loss_r` | mean net R of winners / losers |
| `expectancy_r` | mean net R over all trades |
| `avg_mfe_r`, `avg_mae_r` | mean best / worst open R reached |
| `cagr_pct` | (final ÷ start)^(365.25 ÷ calendar days) − 1 |
| `max_dd_pct` | largest peak-to-trough fall in daily closing equity |
| `mar` | cagr_pct ÷ max_dd_pct |
| `sharpe` | annualised daily, √252; `rf_pct` stated |
| `sortino` | annualised daily, downside deviation only |
| `invested_pct` | mean of daily (position value ÷ equity) |
| `avg_open_positions` | mean of daily open positions |
| `longest_dd_sessions` | peak to recovery; to period end if never recovered |
| `longest_dd_recovered` | true / false |
| `longest_dd_start`, `longest_dd_end` | dates |
| `dsr_prob` | deflated Sharpe probability |
| `dsr_n_trials` | variants counted for DSR; required, or DSR is meaningless |
| `total_return_pct`, `final_equity` | |
| `fees` | all costs in ₹ |
| `cost_share_pct` | fees ÷ gross profit, where gross = net P&L + fees; null if gross ≤ 0 |
| `benchmark_cagr_pct` | benchmark over the same period |
| `avg_hold_sessions_win`, `avg_hold_sessions_loss` | |
| `max_consecutive_losses` | |
| `symbols_traded` | distinct symbols with at least one trade |
| `top5_trades_pnl_share_pct` | net P&L of the 5 best trades ÷ total net P&L |
| `top10_symbols_pnl_share_pct` | net P&L of the 10 best symbols ÷ total net P&L |
| `cagr_ex_top5_trades_pct` | CAGR with the 5 best trades removed (their P&L set to 0) |
| `max_position_pct_adv` | largest entry size ÷ 20-day average traded value |
| `trades_over_5pct_adv` | entries above 5% of 20-day average traded value (fill not realistic) |

### refusals
`{reason: count}`. Fixed reason codes: `max_positions`, `ladder_level`, `industry_cap`, `size_zero_cash`, `size_zero_risk`, `size_below_min`, `max_chase`, `stopout_cap`, `open_risk_cap`, `regime_off`, `kill_switch`.

### binding
Whether the change actually did anything. Required for every variant with a base.
- `trades_changed`: entries different from the base run
- `trade_list_hash`: hash of (symbol, entry_date, exit_date); same hash as base = the change never triggered
- `no_op`: true when the trade list hash matches the base

### breakdowns
- `by_year`: per calendar year: return % (mark-to-market), max DD %, benchmark return %, the
  closed trades by entry year (`trades_entered`, `expectancy_r_by_entry`) and by exit year
  (`trades_exited`, `expectancy_r_by_exit`, `realized_pnl_by_exit`), and the entries decided on
  stored 5-minute bars and on the daily estimate (`entries_5m`, `entries_daily`)
- `by_regime`: per regime state at entry (index on/off, breadth bucket): trades, win %, expectancy R
- `by_exit_reason`: per exit reason: trades, avg R, avg hold sessions
- `by_industry`: per industry: trades, win %, expectancy R, net P&L
- `by_cap_bucket`: `large` | `mid` | `small` | `micro` at entry: trades, win %, expectancy R
- `by_liquidity_bucket`: 20-day average traded value quintile at entry: trades, win %, expectancy R
- `by_playbook`: per playbook: signals, refusals by code, and its trades (trades, win %,
  expectancy R)

---

## 2. trades.csv

One row per closed trade. Partial exits: one row per leg, sharing `trade_id`.

```
run_id,trade_id,leg,symbol,industry,
signal_date,entry_date,entry_price,initial_stop,risk_per_share,qty,risk_pct_equity,
exit_date,exit_price,exit_reason,hold_sessions,
gross_pnl,fees,net_pnl,r_net,mfe_r,mae_r,mfe_date,
mcap_cr,cap_bucket,index_membership,adv_20d_cr,position_pct_adv,price,listing_age_years,
prior_trades_symbol,prior_stopouts_symbol_90d,
ladder_level,open_positions_at_entry,open_risk_pct_at_entry,
index_regime_on,breadth_pct,
vcp_contractions,base_length_sessions,base_depth_pct,final_contraction_pct,depth_ratio,
pivot_vs_base_high_pct,entry_vs_pivot_pct,breakout_vol_ratio,stop_pct,
rs_rank,dist_52w_high_pct,sma50_slope_20d_pct,setup_score,
footprint,contraction_depths,shakeout,dryup_mean_ratio,dryup_min_ratio,
tc_low_sessions,tc_after_sessions,rc_ratio,index_depth_pct,base_number,stage2_start_unknown
```

`exit_reason` codes: `initial_stop`, `trail_swing_low`, `trail_step`, `sma50_close`, `time_stop`, `partial_target`, `kill_switch`, `period_end`.

Stock fields:
- `mcap_cr`: market cap at entry, ₹ crore, point-in-time
- `cap_bucket`: from mcap rank at entry (large = top 100, mid = 101–250, small = 251–500, micro = rest)
- `index_membership`: `NIFTY50` | `NEXT50` | `MIDCAP150` | `SMALLCAP250` | `none`, point-in-time
- `adv_20d_cr`: 20-day average traded value, ₹ crore
- `position_pct_adv`: entry value ÷ `adv_20d_cr`
- `prior_trades_symbol`, `prior_stopouts_symbol_90d`: re-entry history on that stock

## 3. equity.csv

```
date,equity,cash,position_value,invested_pct,open_positions,open_risk_pct,
drawdown_pct,ladder_level,index_regime_on,breadth_pct,benchmark_tri
```

## 4. refusals.csv

Same setup-feature columns as trades.csv, plus:

```
date,symbol,reason,would_be_entry,would_be_stop,fwd_r_20d,fwd_r_60d
```

`fwd_r_*`: R the trade would have reached using the same stop. This is how a risk gate's cost gets measured (e.g. the 316 industry-cap refusals in the hold-out).

## 5. signals.csv

Every setup that fired, before risk gates. Lets taken vs refused vs ignored be compared on the same features.

```
date,symbol,setup_score,taken,refusal_reason,trade_id,<all setup + stock feature columns>
```

## 6. stocks.csv

One row per symbol that produced at least one signal.

```
symbol,industry,cap_bucket_mode,signals,trades,wins,net_pnl,total_r,expectancy_r,
best_trade_r,worst_trade_r,stopouts,avg_hold_sessions,first_trade,last_trade
```

Used to answer: is S1 profitable across stocks, or carried by a few names? Which stocks repeatedly stop out?

---

## Rules
- Units: % as 0 to 100, R as a multiple, money in ₹ with no separators.
- Dates: ISO `YYYY-MM-DD`.
- Every number net of costs unless the field name says `gross`.
- Hold-out runs: `split = hold_out`, and each run is logged with the in-sample run it validates.
- Stock attributes (mcap, index membership, liquidity) are point-in-time, never today's values.
- Results page and analysis read only from these files.

---

## v1.1 changes

**Where.** A run's folder is `HAT_DATA_ROOT/backtests/runs/<run_id>/`. The old
`backtests/run_id=<id>/report.json` and `trades.parquet`, and every backtest row in the
database, are gone: no run of any kind (research, sweep, job, `--for-promotion`) writes to the
database. The writer builds the folder at the end of every run (command line and job) in a
hidden temporary folder, validates `run.json` against `run.schema.json` and renames it into
place, so a folder is whole or absent. A run that fails or stops early still publishes
`run.json` with its `status` and `identity.error` (the failure code), empty metrics and no CSVs.

**config.json.** Every folder also holds `config.json`: the strategy version's config document
and the risk policy document as the run used them (after overrides), the strategy, universe and
universe version ids, the period, the fills, the conventions and stored regime table, the labels
(sweep, variant, base, change, split, validates), the command-line arguments or the variants
item it came from, its known gaps, and the data snapshot (`data_snapshot`: the store, the index
membership and its cover) with its `data_snapshot_id`. `python -m app.hat.backtest --rerun
<run_id>` runs from that file alone, as a new run whose base is the old one (so its `binding`
says whether it made the same trades), and refuses (`backtest_rerun_snapshot_differs`) when the
data it loads is not the snapshot the run read.

**evidence.json.** A completed folder also holds `evidence.json`, HAT's own sidecar outside the
standard: the strategy, version, universe and membership ids, the gate metrics (with the DSR and
the completion time) and the trade buckets signal confidence reads. Promotion (`publish-version
--run`), the lifecycle gates and the risk-increase gate read it (`backtest/evidence.py`), and
only when the run's `audit.json` passed or warned: a run whose audit failed or is missing is no
evidence.

**identity.**
- `run_id`: a UUID v7 (time-sortable); the API takes only that shape.
- `strategy`: `S1` stage2-contraction-breakout, `S2` momentum-breakout-ep, `S3`
  darvas-box-breakout, `S4` trend-pullback-ema, `S5` rsi2-uptrend-reversion; another strategy by
  its name.
- `strategy_version`: the full strategy version id. `engine_commit`: `git rev-parse HEAD`, with
  `-dirty` when the tree has changes, null without a git checkout.
- `sweep_id`, `sweep_name`, `variant`, `change`: null for a run that sets none.
- Added: `error` (null unless failed or stopped early) and `for_promotion` (true for a
  `--for-promotion` run).

**data.** `universe` is `<index>_PIT` for a universe read from its index point in time (e.g.
`NIFTY500_PIT`), else the universe's name. `benchmark` is `NIFTY500_TRI`.

**metrics.**
- Added `avg_dd_pct`: the mean of each drawdown episode's deepest point.
- Drawdowns run peak to the first close at or above it; the start capital is the first peak.
  `longest_dd_sessions` counts sessions after the peak; `longest_dd_start` is the peak's
  session, `longest_dd_end` the recovery's (or the last session).
- `sharpe` and `sortino` use the population deviation of daily returns; `sortino`'s downside
  deviation is the root mean square of the negative excess returns over every session.
- `cagr_ex_top5_trades_pct` takes the five best trades' net P&L out of the final equity.
- Added `pbo` (nullable): the probability of backtest overfitting (CSCV, 8 blocks) of the run's
  family, the completed in-sample runs of its strategy on its universe and the run itself, on the
  daily returns (from `equity.csv`) of the sessions all of them share; a rerun of the same
  variant counts once. Null with fewer than 2 distinct variants or under 16 common sessions.
- `benchmark_cagr_pct` is NIFTY 500 TRI over the run's sessions.
- `dsr_n_trials` is the number of distinct `resolved_hash` values among the completed in-sample
  runs of the same strategy, this run's included; they are kept in
  `HAT_DATA_ROOT/backtests/trials.json` (each hash with its first run and that run's Sharpe,
  which the deflation's trial variance reads). A hold-out run counts the in-sample trials and
  adds none; a failed run adds none.

**refusals.** Every reason the risk check or an entry filter refuses a signal with maps onto one
code, and a reason with no code fails the run (`backtest_unmapped_refusal`):

| code | engine reasons |
|---|---|
| `max_positions` | `max_positions` when the policy's `max_open_positions` is full, `entry_cap`, `position_exists`, `add_to_loser` |
| `ladder_level` | `max_positions` when the current ladder level's lower cap is full |
| `industry_cap` | `industry_cap` |
| `stopout_cap` | `stopout_cap`, `cooldown` |
| `size_zero_cash` | `size_zero_cash` |
| `size_zero_risk` | `size_zero_risk`, `size_zero_position`, `size_zero_liquidity`, `allocation_exceeds_policy`, `stop_not_below_entry`, `missing_input`, `invalid_input`, `rotation_not_paper` |
| `size_below_min` | `size_below_min` (the size's value under the policy's `min_position_pct` of the sleeve's equity) |
| `max_chase` | `max_chase` (the risk check's and the entry filter's) |
| `open_risk_cap` | `global_open_risk` |
| `regime_off` | `regime_gate`, `no_allocation` |
| `kill_switch` | `kill_account`, `kill_bot`, `kill_health`, `bot_paused`, `market_closed`, `stale_feed`, `quality_failed` |

A trigger the `adtr_filter` refuses (the stop too wide in ADTR) is not a signal, as a stop the
playbook's width cap refuses is not: it is in no file.

**binding.** `trade_list_hash` is sha256 over the sorted `symbol,entry_date,exit_date` lines.
`trades_changed` counts the trades in one list and not the other. Null without a base.

**breakdowns.** `by_regime` keys are `index_on`, `index_off`, `index_unknown` (the run's regime
rule at entry) and `breadth_lt40`, `breadth_40_50`, `breadth_50_60`, `breadth_ge60`,
`breadth_unknown` (market breadth, the % of stocks above their 50-day average, of the session
before entry). `by_cap_bucket` is keyed `null` while cap is null (below).

**Stock attributes.** Point in time or null:
- `mcap_cr`, `cap_bucket`, `index_membership`: null. The data has no point-in-time market cap
  and only NIFTY 500 membership.
- `adv_20d_cr`: the 20-session average traded value before entry, ₹ crore; filled for every
  stock with that history (null only without it).
- `liquidity_bucket` (added to trades, refusals and signals): `q1` (least liquid) to `q5`, the
  quintile of `adv_20d_cr` within that day's point-in-time universe.
- `listing_age_years` from the listing date; `price` the (would-be) entry price.

**Files.**
- `trades.csv` adds `liquidity_bucket` after `adv_20d_cr`, and at the end `instrument_id`,
  `playbook`, `engine_exit_reason` (the engine's own exit reason, which the shadow-vs-proxy
  comparison matches) and `auction_risk` (the tags of an exit priced at a close far from live's
  pre-15:15 price, decisions: close phase; on the closing leg).
- Partial exits: one row per sale, `leg` 1..n under the trade's `trade_id`. Each leg has its own
  `qty`, `exit_date`, `exit_price`, `exit_reason` (`partial_target` for a partial rule's sale),
  `engine_exit_reason` and `hold_sessions`, and its share of the trade's P&L: the entry's fees and
  the dividends split by shares, `r_net` over the whole position's risk; the last leg takes the
  remainder, so a trade's legs sum to its net P&L, fees and R exactly. Entry, risk, features,
  `mfe_r` and `mae_r` are the trade's on every leg. Metrics, breakdowns, `stocks.csv` and
  `binding` count trades by `trade_id` (a trade's exit date is its last leg's).
- `exit_reason` adds `trailing_atr`, `breakeven_stop`, `raised_stop` (a hard stop above the
  initial one that no trail is credited with) and the engine's other exits as it names them
  (`early_failure`, `demerger`, `delisted`, ...): `max_hold` is `time_stop`, `pct_gain_target`
  a `partial_target`, a close below the SMA `sma<n>_close` with the strategy's own SMA length
  (`sma20_close`, `sma50_close`; `sma_close` when the length is unknown).
- `refusals.csv`: `fwd_r_20d_gross`, `fwd_r_60d_gross` are the R at the close 20 and 60 sessions
  after the would-be entry day from `would_be_entry` and `would_be_stop`; −1 if a daily low
  reaches the stop first; null when the data ends first; costs excluded (hence `gross`).
- `signals.csv`: `refusal_reason` is the refusal code; `trade_id` is the trade a taken signal
  opened.
- A watchlist row's `criteria_results` now carries each criterion's `extras` (the VCP's
  contractions, base length and depths), which the setup columns read.

**Sweeps.** A variants file is a mapping: `sweep_id`, optional `sweep_name` (the id when absent)
and `variants`, each with a unique `label`, optional `base` (an earlier label of the file or a
run id) and `change` (one line), and its overrides. A single run takes the same labels as
`--sweep-id`, `--sweep-name`, `--variant`, `--base <run_id>`, `--change`, and `--split hold_out
--validates <run_id>`.

**API.** `GET /api/hat/backtests/runs?strategy=&sweep_id=&split=` (each run's identity, data,
metrics, refusals and binding, newest first), `GET /api/hat/backtests/runs/{run_id}` (the whole
`run.json`) and `GET /api/hat/backtests/runs/{run_id}/files/{name}` (`trades`, `equity`,
`refusals`, `signals`, `stocks` as `text/csv`).

---

## v1.2 changes

**Audits.** Every folder also holds `data_audit.json`, the data audit of the market the run read
(`app/hat/data/audit.py`), and a completed run's `audit.json`, its run audit
(`app/hat/backtest/run_audit.py`): each a document with `status` (`pass` | `warn` | `fail`), its
checks and their findings, and a `digest` (sha256 of the rest). The checks and their thresholds:
`process.md`. `run.json` `data.audit` is `{digest, status}` of the data audit, and `config.json`
`data_audit` the same; `data.industry_unknown_pct` is the share of member-days with no industry
and `data.undated_member_demergers` the member demergers in the period with no `announced_on`
(both from the data audit); `data.verified` is whether the data verification report covering the
window (`python -m app.hat.data.verify`) covers the run's window with every blocking check, none
of them failed there, on data unchanged since (its snapshot), and `data.verify_report` that report's digest (null without one). A run whose data audit fails does not start and writes no folder
(`backtest_data_audit_failed`).

**Start capital.** `config.json` `conventions.start_capital` is the run's start capital (₹10,00,000
unless `--start-capital` or a variant's `start_capital` sets another); a run off the default
records it in `config.resolved.conventions.start_capital`, so it hashes as its own trial.

**API.** A list item adds `audit` (the run audit's `status` and `digest`, null without one);
`GET /api/hat/backtests/runs/{run_id}/audits/{name}` returns `audit` or `data_audit`.

**Sweep discipline.** `config.json` adds `knobs` (each knob the run was asked for, by name:
`set:<path>`, `policy_set:<path>`, `policy` by digest, the conventions, slippage, cost model and
cooldown; a rerun or hold-out carries the replayed run's) and `labels.bundle`. `run.json`
`identity` adds `changed_knobs` (the knobs that differ from the base's, null without a base or
when the base's `config.json` has no `knobs`) and `bundle` (a declared bundle's reason, else
null); `config` adds `risk_policy_version` and `base_risk_policy_version` (the base's, null
without one). Each is optional in `run.schema.json`. A hold-out made with `--rerun <id> --split
hold_out` records `asked.hold_out_of` and the candidate's `config.overrides`.

---

## v1.3 changes

**Trials.** `trials.json` (registry version 2) keys trials by family: strategy, universe and
window (`S1/NIFTY500_PIT/2016-01-01..2021-12-31`). `dsr_n_trials` is the number of distinct
`resolved_hash` values among the family's completed in-sample runs, this run's included, plus
the family's prior trials: variants tried before the registry saw them, seeded from their sweep
files (`python -m app.hat.backtest.trials seed`), each counted without a Sharpe ratio. A
hold-out run counts the family of the run it validates and adds none. A reset of the run folders
never deletes the registry.

**metrics.**
- Added `dsr_n_trials_effective`: the trial count the DSR was scored at, `dsr_n_trials` floored
  at 5 (the trial-Sharpe variance is likewise floored at 0.25/252, an annualised spread of 0.5).
  Both floors only deflate more.
- `pbo` reads the family's completed in-sample runs on the run's universe and window; null with
  fewer than 4 distinct variants (with 2 or 3, CSCV's rank is a coin toss).
- `avg_hold_sessions_win`, `avg_hold_sessions_loss`, `by_exit_reason.*.avg_hold_sessions`,
  `stocks.csv` `avg_hold_sessions` and `trades.csv` `hold_sessions` count the run's sessions after
  the entry up to the exit (a leg's own exit for `hold_sessions`), not calendar days.

**audit.json.** The `data` check's finding names the data audit's checks that did not pass
(`checks`) and the owner's explanation of that audit digest (`explained`: the path of
`backtests/data_audits/<digest>.explained.md`, null without one).

---

## v1.4 changes

**refusals.** Added `size_below_min`: an entry whose sized value is under the risk policy's
`min_position_pct` of the sleeve's equity is refused rather than opened as a crumb position.

**audit.json.** `equity_jumps` counts a position opened and closed in the same session (a
same-day round trip, at its entry value) as held that session.

---

## v1.5 changes

**Portfolio and overlay runs.** A run of several strategies (`process.md`, How to define a
backtest) names each playbook `<bot>:<playbook>` in every file; `config.json` adds `co_sleeves`,
and the conventions and `config.resolved` add them only when set, so a one-strategy run keeps its
hashes. An overlay run adds `run.json` `overlay` (its config, data proxy ranges, counts,
`net_flow` = the sleeve's trades less all tax, tax by financial year, by-year costs, turnover and
switches, events, and the B1/B2/TRI benchmarks), `overlay.csv` (`overlay.OVERLAY_CSV_HEADER`) and
the refusal code `insufficient_same_day_credit` (a cash refusal while the sleeve held units);
`equity.csv` `position_value` includes the sleeve, and the run audit's P&L check adds `net_flow`.

**Rotation runs** (a strategy with a `rotation` block, `decisions.md`). `rebalances.csv`
(`report.REBALANCE_COLUMNS`) holds one row per name a rebalance touched: `date` (the execution
open), `decision_date` (the close it ranked on), `strategy`, `instrument_id`, `symbol`, `action`
(`buy`, `top_up`, `trim`, `sell`, `hold`), `reason` (a sell's exit reason; `cash` for a buy
the cash cut; a blocked order's block, `no_bar`, `lower_circuit`, `upper_circuit`,
`demerger_pending`, `factor_pending`, with `qty` 0), `rank` and `score` on the decision close,
`qty`, `price`, `value` and `target_value`; a sell outside a rebalance (demerger, delisting, a
sell carried to the next open it can fill) has an empty `decision_date`. A rotation trade's
playbook is `rotation`, `initial_stop` is 0 and `r_multiple` is its net P&L over the first buy's
price times the shares bought (no stop to risk against); trades.csv ends with `rank_entry`, `score_entry`,
`rank_exit` and `score_exit` (empty for other playbooks' trades and for an exit the ranking did
not decide). Exit reasons: `rotation_out` (ranked below the buffer), `rotation_not_eligible`
(failed the universe stage or unscored), `rotation_market_filter`, `rotation_trim` (a trim's
partial exit). Every rotation order fills at 09:20 (`engine/fill_0920.py`: the 09:15 bar's
close, else the daily open moved by the measured median to 09:20); `run.json` metrics count them,
`rotation_fills_0920` and `rotation_fills_estimated`. Known gaps
`rotation_fills_0920_estimated_without_5m` and `rotation_sized_on_decision_close`; were the
median unmeasured, `fill_0920_estimate_provisional` (also for a run whose daily-estimate
entries gapped over their level).

**by_year.** `trades` and `expectancy_r` (the trades exiting that year, beside a mark-to-market
return, with no label) are replaced by fields named by their basis: `trades_entered` and
`expectancy_r_by_entry` (the closed trades entered that year), `trades_exited`,
`expectancy_r_by_exit` and `realized_pnl_by_exit` (those whose last sale was that year).
`return_pct` stays mark-to-market, so a trade held over a year end marks both years' return,
counts by entry in the first and by exit in the second.

**conventions.** `slot_order` may be `rs`; `volume_pace: full_day` is recorded only when a run
asks for it (a default run keeps its digest). Known gap `volume_pace_on_full_day_volume`.

**funnel.** `run.json` adds `funnel`: `regime_off_nights` (nights the regime gate kept the scan
off) and `by_playbook`, per playbook the count passing each gate in order, read as the run goes
(no replay). Scan gates count stock-days: `evaluated` (members with a bar that night),
`universe`, the setup's hard criteria (the trend template's checks as one gate `trend_template`,
`rs_rank`, `book_vcp` as `vcp_geometry`, `time_compression`, `dry_up`, `relative_correction`,
`base_count`, others by type), `setup` (the score threshold) and `watchlist` (published). Day
gates count watchlist rows: `trigger` (the level broke; on a 5-minute session the daily high
above the level), `breakout_volume` (the trigger's volume pace, on the full day's volume
under `volume_pace: full_day`), `stop` (a stop could be planned) and `max_chase`.

**trades.csv, refusals.csv, signals.csv.** `book_vcp`'s log per signal (empty for other setups):
`footprint` (`<weeks>W <Depth_1>/<Depth_final> <count>T`, weeks the base's sessions / 5, depths
in whole %), `contraction_depths` (each depth %, oldest first, `/`-joined), `shakeout` (a
contraction low under an earlier one), `dryup_mean_ratio` and `dryup_min_ratio` (the final
contraction's mean and lowest volume over the 50-session mean ending on its first session),
`tc_low_sessions` and `tc_after_sessions` (H0 to the deepest low, that low to the signal date),
`rc_ratio` (Depth_1 over the NIFTY 500's largest close-to-close fall since H0, `index_depth_pct`;
empty when the index did not fall), `base_number`, `stage2_start_unknown`. `rs_rank` is the
setup's `rs_rank` criterion value when it has one (under `basis: eligible` the 1..99 percentile
within the bot's universe stage that night: an approximation of the IBD RS rating, not from
the book), else the stored `rs_pct`.

**Episodic pivot (a run whose config has a pre-open playbook).** trades.csv and signals.csv end
with the EP log of each pre-open signal (empty for other playbooks'): `catalyst_type`
(`earnings` with a results filing public in (the previous session's 09:15, the trigger],
`other_announcement` when only `catalyst_attached` matched, which no stored past announcement
allows today, else `none`), `results_public_at`, `sales_yoy_pct`, `eps_yoy_pct` and
`turnaround` of that filing as known then, `gap_pct`, `trigger_time`, `vol_vs_adv20` (the day
volume at the trigger over ADV(20)), `range_126d_pct` (max high / min low - 1 of the 126
sessions to the previous one), `adr20_pre_gap_pct` (the repo's ADR(20) to the previous session)
and `ret_63d_pct`. `ep_candidates.csv` holds one row per candidate-day in a pre-open playbook's
gap scan: `basis`, `reached` (the last gate passed), `failed_gate`, `reason`, `signal_state` and
the same columns. run.json `breakdowns.episodic_pivot.<playbook>` holds the playbook's trades
`by_catalyst_type`, `by_sales_yoy` (lt25, 25_50, 50_100, gt100), `by_gap` (lt10, 10_15, 15_25,
gt25), `by_trigger_time` (0920_0945, 0945_1100, after_1100), `by_adr20` (lt3, 3_4, 4_6, gt6),
`by_range_126d` (lt30, 30_60, gt60) and `by_exit_reason` with the mean hold; run.json
`funnel.by_playbook.<playbook>` counts candidates through `gap_scan`, `gap_up`, the hard setup criteria,
`earnings_catalyst`, `early_volume`, `price_breaks_level`, the entry filters and `filled`.

## Accuracy checks and the fills' approximations

**Ledger.** Every run reconciles its money from its fills and the market data, never from the
engine's accounts (`backtest/reconcile.py`): each fill's charges recomputed from the dated
schedule (the DP charge once per instrument per sell day), each fill's price inside its day's
bar (slippage moves it against the trade, so a buy prints up to the high after slippage and a
sale down to the low after slippage; a sale without a bar between the suspended mark and the
last traded close), each session's dividends from the shares the fills held into the ex-date,
the cash at each close from those (never below zero), the shares held by instrument, each
held instrument's mark from its bar's close or its last traded close (marked down after
`suspension_sessions`), the ETF sleeve from the overlay's own fills, tax and distributed units,
each session's position value and equity against the engine's, each trade's P&L from its entry,
legs, charges and dividends, and each instrument's P&L against its fills, all within ₹1 (a
charge exactly). A mismatch fails the run (`backtest_ledger_mismatch`, `ledger.json` in the
failed folder); a complete run writes `ledger.json` and the run audit's `ledger` check
(`process.md`).

**As of.** The engine's market reads go through an as-of clock (`data.MarketView`): a decision
reads only sessions whose close is public, a fill only the session in progress; a read past it
fails the run (`backtest_lookahead`). The close phase decides on the session's own close
(decisions.md). Under the clock too: the results filings (`MarketView.results_on`, those public
by the session's end; the trigger reads only those public by its instant), the 5-minute store
(the session in progress, its bars replayed in order; a session's bars are used when the stored
file holds every bar of its 09:15-15:25 grid and its first open is within 5% of the daily open,
never by its close), the rotation's total-return closes
(through the decision's close) and the ETF overlay's prices, closes, band states and
LIQUIDBEES' index (`Overlay.clock`).

The guard's limit: the clock is per session, not per instant. From the session's open,
`MarketView.bar` returns the whole daily bar (open, high, low, close), so the guard cannot
refuse a read of the day's high, low or close at the open. The reads that rely on it are the
daily bar's simulation of the day, not decisions on its outcome: the exits' path through the bar
(`FILL_PATH`: a stop at the low, a target at the high, the lower band locked all day as open =
high = low = close), a resting buy-stop's break (the high reaching the level), the entry day's
low before the break (`entry_day_low_estimated`) and the upper band at the entry (the high). One
decision reads the close: the EP daily estimate fires only when the close is in the upper half
of the day's range (`EP_ESTIMATE.fires`, calibrated on 5-minute sessions); each such read is
counted `ep_estimate_close`. An entry decided on a 5-minute session reads none of these: the
replay sees each bar only once it has ended (the exits still take the daily bar's path).

The reads the run makes past the clock on purpose go through `MarketView.hindsight` and are
counted by reason (`BacktestResult.hindsight`); the run audit's `lookahead` check warns
`lookahead_hindsight` with the counts of those a decision made: `ep_estimate_close` (above),
`ep_estimate_volume` (the day's volume for the EP daily estimate, `episodic_pivot_trigger_estimated`; a daily bar has no earlier
volume, so there is no point-in-time alternative), `volume_pace_full_day` (a session paced on
its full day's volume: on a daily estimate always, the same reason; on 5-minute bars only under
the opt-in `volume_pace: full_day` convention, the default `live` paces on the bars) and
`delisted_exit` (each exit at a delisting's last close: which session was the last is known
from the registry and the later bars, not on the day, and the data has no point-in-time
delisting notice). The descriptive `breakout_vol_ratio` column is counted, not warned. What
the run also knows ahead of time, by design: the sessions within `JUMP_SKIP_SESSIONS` of an
unexplained price jump (skipped either side, a data-quality quarantine), the adjusted price
basis (later splits and bonuses scale earlier bars; a ratio, so no rule reads the future through
it), and the industry of each instrument (`hat_industry` holds one classification per
instrument, today's, from its `effective_from`: the industry cap reads it for earlier sessions
too). The stored features and breadth are point in time: the data audit's `lookahead` check
recomputes a sample from the bars dated on or before each day.
`scripts/research/bt_truncation.py` checks a run to T against one to T + N on real data: the
watchlists, signals, skips and funnel by session, EP log, rotation orders and fills up to T must
agree; a delisting the registry dates after T is reported apart (`delisting_known_later`):
the runs are compared exactly up to the session before its exit and reported diverged from it.

**Fills: what a daily bar cannot show and the run assumes.**

| Event | The run's rule |
| --- | --- |
| The path through a daily bar | open, low, high, close (`FILL_PATH`): a stop and a target in one bar is a stop |
| A buy-stop entry | the high breaking the level, one tick in (`level_break_estimated`); a gap over the level at the open moved by the median to 09:20, at most the day's high (`gap_over_fills_0920_estimate`), refused beyond max chase |
| The entry day's low before the break | max(low, min(open, level x (1 - ADR20/100))) (`entry_day_low_estimated`) |
| A stop gapped through | the open |
| A stop raised on the way up | holds through that day's dip unless the close is below it (`ambiguous: optimistic`, the default; counted in `ambiguous_days`) |
| A 5-minute entry | the LIMIT at the trigger bar's close fills only if a later 5-minute bar trades at or below it (a daily low no bar shows does not count; else skip `limit_not_reached`), never above the limit after slippage; after the trigger the low comes before the high |
| Pre-open (EP) entries on daily bars | the EP estimate (`EP_ESTIMATE`) |
| `volume_pace` | on the full day's volume (`volume_pace_on_full_day_volume`) |
| Close rules and close entries | the official close for live's pre-15:15 price (`close_rules_decided_on_official_close`); F&O expiry and index rebalance days tagged `auction_risk` (exits only) |
| Circuits | read off the bar, no band table: an entry at the high within a tick of a 2/5/10/20% band is locked; no sale on a day locked down all day (O = H = L = C at the band), nor at a close that is the low at the band |
| Suspension, delisting | no fill without a bar; the mark is the last close for 5 sessions, then 5% lower each; a delisting exits at its last close |
| Rotation | fills at 09:20: the 09:15 bar's close, else the open moved by the median to 09:20 (`rotation_fills_0920_estimated_without_5m`) |
| Slippage | a flat 20 bps against every fill, no spread or impact model; entries above 5% of ADV are flagged, not refused |
| Settlement | a sale's proceeds fund buys at once, at 100%, whatever the order of the day's fills (no T+1 hold; a broker gives same-day credit for delivery sales but the run does not time it within the day) |
| Costs | the dated schedule, `costs.md` |
