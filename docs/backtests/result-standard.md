# Backtest Result Standard v1.4

Sections 1 to 6 and the rules are v1.0; the sections v1.1 to v1.4 changes amend them, a
later one winning where they differ. `format_version` is `1.4` (`run.schema.json`,
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
- `by_year`: per calendar year: return %, max DD %, trades, expectancy R, benchmark return %
- `by_regime`: per regime state at entry (index on/off, breadth bucket): trades, win %, expectancy R
- `by_exit_reason`: per exit reason: trades, avg R, avg hold sessions
- `by_industry`: per industry: trades, win %, expectancy R, net P&L
- `by_cap_bucket`: `large` | `mid` | `small` | `micro` at entry: trades, win %, expectancy R
- `by_liquidity_bucket`: 20-day average traded value quintile at entry: trades, win %, expectancy R

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
rs_rank,dist_52w_high_pct,sma50_slope_20d_pct,setup_score
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
| `size_zero_risk` | `size_zero_risk`, `size_zero_position`, `size_zero_liquidity`, `allocation_exceeds_policy`, `stop_not_below_entry`, `missing_input`, `invalid_input` |
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
  a `partial_target`.
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
(both from the data audit). A run whose data audit fails does not start and writes no folder
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
