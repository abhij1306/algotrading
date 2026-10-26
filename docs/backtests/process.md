# Backtest process

The one way any agent runs a backtest. Code changes only when a strategy changes or a new one is
added; every run below is config and command lines. Run the commands from `backend/` with the venv
active and `HAT_DATA_ROOT` set; each reads the database in a read-only transaction. What each
result field means: `result-standard.md`.

## Rules

1. **Criteria first.** Before the first run of a strategy, its promotion criteria (the thresholds
   it must meet, e.g. expectancy, MAR, DSR, PBO, trades, concentration, hold-out degradation) and
   its trial budget (how many variants may be tried) are written in the strategy's notes, the
   owner's living document per strategy outside the repo. They are not changed after a run has
   been seen.
2. **Windows.** Choose on the in-sample window; read the hold-out window once, for the one
   candidate, at the end. Trades from 2016-01-01 only (point-in-time membership starts there).
3. **One change per variant.** Every variant names its `base` (an earlier label or a run id)
   and a one-line `change`, and differs from the base in that one change. `binding.no_op: true`
   means the change never fired: it tells you nothing.
4. **Trials count.** Every completed in-sample run with new parameters is a trial
   (`dsr_n_trials`, `HAT_DATA_ROOT/backtests/trials.json`); stop at the budget. Reruns and
   duplicates add none.
5. **Audits gate.** A run refuses to start on a failed data audit
   (`backtest_data_audit_failed`). A run whose `audit.json` fails is not evidence until the
   failure is explained.

## Steps

| # | Step | Command | Lands in |
| --- | --- | --- | --- |
| 1 | Data check | `python -m app.hat.data.audit --from 2016-01-01 --to 2021-12-31 --universe nifty500` | `backtests/data_audits/<key>.json` |
| 2 | Baseline run | `python -m app.hat.backtest --strategy-version <v> --universe nifty500 --from 2016-01-01 --to 2021-12-31 --sweep-id <s> --variant base` | `backtests/runs/<run_id>/` |
| 3 | Sweep | `python -m app.hat.backtest --strategy-version <v> --universe nifty500 --from 2016-01-01 --to 2021-12-31 --variants sweep.yaml` | one folder per variant |
| 4 | Compare | `python -m app.hat.backtest.compare --sweep <s>` (or run ids) | stdout table |
| 5 | Plateau | a sweep of the chosen variant's key parameters, each one step either side, `base` the chosen run | one folder per variant |
| 6 | Review request | `python -m app.hat.backtest.review <run_id> --criteria <strategy notes>` | `<run folder>/review_request.md` |
| 7 | Hold-out, once | `python -m app.hat.backtest ... --from 2022-01-01 --to <last session> --split hold_out --validates <run_id>` (same overrides) | `backtests/runs/<run_id>/` |
| 8 | Paper | promote and run the sleeve in shadow (`runbook.md`, Strategy promotion) | |

Rerun any run exactly from its folder: `python -m app.hat.backtest --rerun <run_id>` (a new run
based on the old one; refused `backtest_rerun_snapshot_differs` when the data changed).

Step 1 also runs inside every run (cached by data snapshot and window, so a sweep audits once);
`--refresh` recomputes it. Step 6: hand `review_request.md` to an independent review agent, which
writes `audit_report.md` beside it with a `promote`, `hold` or `reject` verdict. A candidate goes
to step 7 only if it meets every criterion of rule 1, its audits pass or each warning is
explained, its plateau holds, and the review says `promote`.

## Reading a run

In `HAT_DATA_ROOT/backtests/runs/<run_id>/`, at `GET /api/hat/backtests/runs/<run_id>`, or on its
page `/hat/backtests/<run_id>` (both audits' findings above the metrics; the runs list flags a run
whose audit warns or fails):

- `audit.json` first (`GET .../audits/audit`): `status` and each check's findings. Then
  `data_audit.json` (`GET .../audits/data_audit`), the data audit the run read.
- `run.json`: `metrics` (whether), `binding` (did the change fire), `breakdowns` (by year,
  regime, exit, industry, liquidity), `refusals` (what the risk gates blocked).
- `trades.csv`, `equity.csv`, `refusals.csv` (with forward R of each blocked entry),
  `signals.csv`, `stocks.csv`: why.
- `config.json`: everything the run used, enough to rerun it.

### Data audit (`data_audit.json`, `app/hat/data/audit.py`)

| Check | Fails | Warns |
| --- | --- | --- |
| `calendar` | a stored day that is no NSE session, or a session with no bars | period ends after the data |
| `missing_sessions` | members miss > 2% of their member sessions overall | a member misses > 5% |
| `survivorship` | a leaver with no bars, or no leaver in a window of a year or more | a leaver with bars on < 95% of its member days |
| `corporate_actions` | an adjustment factor that is not a positive number | an adjusted move > 20% on an ex-date; an action-shaped raw move (a split or bonus ratio) with no factor |
| `lookahead` | a stored feature or breadth of D that differs from its recount on bars <= D | a session whose previous session has no index close or breadth (the gates read D-1) |
| `unique_keys` | a duplicate bar, features row or index close | |
| `bars_5m` | (when the run reads them) bars off the IST 5-minute grid or outside the daily bar | the same, while no run reads them |

### Run audit (`audit.json`, `app/hat/backtest/run_audit.py`)

| Check | Fails | Warns |
| --- | --- | --- |
| `binding` | | `no_op`, or a base without a binding |
| `fills` | a fill outside its day's low-high (widened by slippage); an entry without a bar | |
| `sequence` | signal > entry, or an exit before the entry | |
| `pnl` | net != gross - fees; R != net / initial risk; trades, P&L vs final equity, fees, expectancy or win % disagree with `run.json` | |
| `costs` | a trade without fees | cost share > 50% of gross profit |
| `liquidity` | `position_pct_adv` wrong | an entry > 5% of 20-day traded value |
| `concentration` | top-5 share disagrees | top 5 trades > 50% of net P&L; CAGR not positive without them |
| `equity_jumps` | a > 5% session move with no position held | a move beyond 20% of the value held |
| `refusals` | `refusals.csv` counts differ from `run.json` | |
| `trials` | no `dsr_n_trials` | no DSR |
| `data` | no data audit | the data audit warned |

## Variants file

```yaml
sweep_id: s1-exits-07
sweep_name: S1 exit stack            # optional
variants:
  - {label: base}
  - label: trail20
    base: base                       # an earlier label of this file, or a run id
    change: swing-low trail 20 days  # one line
    set: ['exits[trailing_swing_low].lookback_days=20']
```

A variant's keys: `label`, `base`, `change`, `validates` (hold-out), and any knob below. The
command line's options apply to every variant; a variant's keys replace them for that variant.

## Knobs

`set` paths edit the strategy version's config document (`engine/strategy/config.py`): a list
item is named by index, by its `rule`/`name`/`type`, `#n` for the n-th of a type held twice
(`criteria[rs_rank#1]`), `[+]` appends; a missing or ambiguous name is refused. `policy_set`
paths edit the risk policy (`risk/policy.py`). Sweep column: the variants-file key; the command
line flag sets the same for one run or every variant.

| Knob | Variants file | Command line |
| --- | --- | --- |
| Setup criteria and params, mode, weight, on/off, add | `set: ['playbooks[default].setup.criteria[vcp].params.contractions=[3,6]']`, `...criteria[vcp].enabled=false`, `...setup.threshold=0.7` | `--set` |
| Universe filters | `set: ['universe[min_turnover].params.inr_cr=20']` | `--set` |
| Trigger and entry timing, breakout volume | `set: ['playbooks[default].trigger[price_breaks_level].params.entry_timing=daily_close']`, `...params.volume_vs_avg=1.5` | `--set` |
| Entry filters | `set: ['playbooks[default].entry_filters[max_chase].params.pct=2']`, `...[adtr_filter].params.max_multiple=1` (a very wide chase, e.g. 100, is off) | `--set` |
| Initial stop and its cap | `set: ['playbooks[default].stop={"rule": "atr_multiple", "days": 14, "multiple": 2, "max_pct": 8}']`, `...stop.max_pct=10` | `--set` |
| Exits (partial, breakeven, step trail, SMA/EMA close, swing low, ATR trail, time stop, early failure, max hold) | `set: ['exits[step_trail].step_r=1.5', 'exits[time_stop].enabled=false', 'exits[+]={"rule": "early_failure", "days": 5}']` | `--set` |
| Regime gate: rule, params, index | `set: ['regime={"index": "NIFTY 500", "rule": "breadth_above", "params": {"min_pct": 55}}']`, `regime.params.sma=100` | `--set` |
| Regime sizing (1 or the live regime engine) | `regime_mult: policy` | `--regime-mult` |
| Risk per trade | `risk_pct: 0.005` | `--risk-pct` |
| Sleeve weight | `sleeve_weight: 0.5` | `--sleeve-weight` |
| Max positions (and the ladder's top level) | `max_positions: 10` | `--max-positions` |
| Per-industry cap | `max_per_industry: 2` | `--max-per-industry` |
| Ladder off (one size from day one) | `flat_ladder: true` | `--flat-ladder` |
| Ladder levels, open risk, cluster cap, entries and stopouts per day, cooldown, ADV cap, multiplier bounds | `policy_set: ['exposure_ladder.L2.max_positions=6', 'global_open_risk_pct=0.06', 'cooldown_sessions=5']` | `--set-policy`, `--cooldown-sessions` (wins over the policy's) |
| A whole candidate policy | `policy: cand.yaml` | `--policy` |
| Slot order of a capped day | `slot_order: score` | `--slot-order` |
| Entry fill rule, ambiguous days, suspension markdown | `entry_fill: touch`, `ambiguous: pessimistic`, `suspension_sessions: 3` | `--entry-fill`, `--ambiguous`, `--suspension-sessions` |
| Start capital | `start_capital: 2500000` | `--start-capital` |
| Slippage, cost model | one sweep per value | `--slippage-bps` (20 by default), `--cost-model` |
| Universe, window | one sweep per value (a sweep reads one market) | `--universe`, `--from`, `--to` |

Not knobs without a code change (strategy or engine changes, shared with live): the live regime
engine's thresholds and multiplier table (`engine/portfolio/regime.py`, under `regime_mult:
policy`), the allocator's smoothing, the breadth basis (% above SMA50), the RS weights and other
feature windows (a features rebuild), the VCP score blend, a breakeven offset, and simulated kill
switches and health (known gaps: `policy_set` on them has no effect).
