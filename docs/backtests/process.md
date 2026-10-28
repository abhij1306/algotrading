# Backtest process

The one way any agent runs a backtest. Code changes only when a strategy changes or a new one is
added; every run below is config and command lines. Run the commands from `backend/` with the venv
active and `HAT_DATA_ROOT` set; each reads the database in a read-only transaction. What each
result field means: `result-standard.md`.

## Before you start

- **Data root.** Export `HAT_DATA_ROOT` to the research store (the ISIN-keyed Parquet store, a
  `hat-data-isin` folder beside the main checkout), not the default `data_system/hat/`: the runs,
  `trials.json` and the data audits live under it, and `compare`, `review` and `--rerun` read
  only it. Check what a shell resolves with `python -c "from app.hat.settings import
  operator_settings; print(operator_settings().data_root)"`.
- **Strategy version `<v>`.** `python -m app.hat.admin show` lists each strategy's versions with
  their ids (`runbook.md`, Admin command line); pass the full id.
- **Research files, outside the repo** (the repo is public), beside the main checkout:
  `backtests/sweeps/<sweep_id>.yaml` the variants files, `backtests/<sweep_id>.log` each sweep's
  printed lines (`... --variants FILE | tee ../../backtests/<sweep_id>.log`), `backtests/policies/`
  candidate policy files, and `strategy-notes/<strategy>.md` each strategy's notes (rule 1),
  whose one `hat-criteria` block `review --criteria` reads.
- **Paths.** A `--variants` path and a variant's `policy:` path resolve from the working
  directory, `backend/`: from a worktree's `backend/`, use absolute paths.

## Rules

1. **Criteria first.** Before the first run of a strategy, its promotion criteria (the thresholds
   it must meet, e.g. expectancy, MAR, DSR, PBO, trades, concentration, hold-out degradation) and
   its trial budget (how many variants may be tried) are written in the strategy's notes, the
   owner's living document per strategy outside the repo, as one `hat-criteria` block (below).
   They are not changed after a run has been seen; the review pins the block's sha256.
2. **Windows.** Choose on the in-sample window; read the hold-out window once, for the one
   candidate, at the end. Trades from 2016-01-01 only (point-in-time membership starts there).
   A hold-out runs the candidate's own `config.json` (step 7) and is refused for a candidate
   that is not a completed in-sample run (`backtest_hold_out_bad_candidate`), a window that
   does not start after the candidate's (`backtest_hold_out_overlaps`), a config that is not
   the candidate's (`backtest_hold_out_config_differs`) and a second hold-out of the same
   config, its rerun included (`backtest_hold_out_repeated`; a failed one does not count).
3. **One change per variant.** Every variant names its `base` (an earlier label or a run id)
   and a one-line `change`, and differs from the base in one knob (a `set` or `policy_set`
   path, a candidate `policy`, or any other knob below; a run's slippage, cost model and
   cooldown count too). A sweep refuses a variant that changes more
   (`backtest_sweep_not_one_change`) unless it declares `bundle: true` and a one-line `reason`;
   a bundle is one candidate, not evidence for any one of its changes, and its run audit warns
   (`one_change`). A `set` knob is the path it resolves to in the version's config, so
   `exits[time_stop].enabled` and `exits[3].enabled` are one knob. A base named by run id is
   checked against the knobs its `config.json` records. Every variant of a sweep should run under its base's risk policy version: the run
   audit warns on a drift (`policy_drift`) and `compare` shows it. `binding.no_op: true` means
   the change never fired: it tells you nothing.
4. **Trials count.** Every completed in-sample run with new parameters is a trial of its family
   (strategy, universe, window: `dsr_n_trials`, `HAT_DATA_ROOT/backtests/trials.json`); stop at
   the budget. An in-sample sweep takes the notes (`--criteria`) and refuses a variant that
   would be a new trial once the family's own trials reach `trial_budget`
   (`backtest_sweep_trial_budget`); each printed line shows `trials` (own/budget) beside
   `dsr_n_trials`, which counts the prior ones too. Reruns and duplicates add none. Variants tried before the registry saw them (runs
   since deleted) are seeded as prior trials: `python -m app.hat.backtest.trials seed --strategy
   S1 --universe NIFTY500_PIT --from 2016-01-01 --to 2021-12-31 <sweep files>`; `trials show`
   prints each family's count. Never delete `trials.json`, only run folders.
5. **Audits gate.** A run refuses to start on a failed data audit
   (`backtest_data_audit_failed`). A run whose `audit.json` fails is not evidence until the
   failure is explained. The owner explains a data audit's warnings once per digest, one line
   per finding, in `backtests/data_audits/<digest>.explained.md`; the run audit's `data` check
   and the review name it.

## Steps

| # | Step | Command | Lands in |
| --- | --- | --- | --- |
| 1 | Data check | `python -m app.hat.data.audit --from 2016-01-01 --to 2021-12-31 --universe nifty500` | `backtests/data_audits/<key>.json` |
| 2 | Baseline run | `python -m app.hat.backtest --strategy-version <v> --universe nifty500 --from 2016-01-01 --to 2021-12-31 --sweep-id <s> --variant base` | `backtests/runs/<run_id>/` |
| 3 | Sweep | `python -m app.hat.backtest --strategy-version <v> --universe nifty500 --from 2016-01-01 --to 2021-12-31 --variants sweep.yaml --criteria <strategy notes>` | one folder per variant |
| 4 | Compare | `python -m app.hat.backtest.compare --sweep <s> [--criteria <strategy notes>]` (or run ids): `knobs` is the knobs changed against the base (`5b` a declared bundle), `policy` the risk policy version (`3!2`: 3 against a base under 2), `longest_dd`, `bench%`, `cagr_ex5%` and `>5%adv` the criteria's metrics, `inv%` and `inv_max%` the invested share (mean and highest session); `--criteria` adds each run's in-sample criteria, pass or fail, and the trial budget | stdout tables |
| 5 | Plateau | a sweep of the chosen variant's key parameters, each one step either side, `base` the chosen run | one folder per variant |
| 6 | Criteria and review request | `python -m app.hat.backtest.review <run_id> --criteria <strategy notes> --sweep <plateau sweep>` | `<run folder>/criteria.md`, `criteria.json`, `review_request.md` |
| 7 | Hold-out, once | `python -m app.hat.backtest --rerun <run_id> --split hold_out --from 2022-01-01 --to <last session>`: the candidate's `config.json` on the hold-out window (rule 2), then `review <hold-out run_id> --criteria <notes>` (`--validates <run_id>` names the in-sample run) | `backtests/runs/<run_id>/` |
| 8 | Paper | promote and run the sleeve in shadow (`runbook.md`, Strategy promotion) | |

Rerun any run exactly from its folder: `python -m app.hat.backtest --rerun <run_id>` (a new run
based on the old one; refused `backtest_rerun_snapshot_differs` when the data changed). With
`--split hold_out --from --to` the same replay is the run's hold-out (step 7).

Step 1 also runs inside every run (cached by data snapshot and window, so a sweep audits once);
`--refresh` recomputes it. Step 1 includes the golden sample check (`golden-sample.md`; run it
alone with `python -m app.hat.data.golden check`). When a deliberate data fix moves golden
values, rebase them with a reason (`golden rebase`); never edit a golden file. Step 6 computes every criterion from the folders (never by hand) into `criteria.md`: `pass`
only when every row passes, `fail` when one fails, `incomplete` when one is `not_run` (no plateau
sweep, no hold-out yet). Then hand `review_request.md` to an independent review agent, which
writes `audit_report.md` beside it with a `promote`, `hold` or `reject` verdict. A candidate goes
to step 7 only if its in-sample rows and plateau pass, neither audit fails (a failed or missing
audit is no evidence; the `audits` rows fail it) and each warning is explained, and the review
says `promote`.

### Criteria block

One fenced block tagged `hat-criteria` in the strategy's notes, in YAML; every key but
`strategy`, `universe`, `in_sample` and `trial_budget` is optional, and an unknown key is
refused.

```yaml
strategy: S1
universe: NIFTY500_PIT                          # run.json data.universe
in_sample: {from: 2016-01-01, to: 2021-12-31}   # the run's window must equal it
hold_out: {from: 2022-01-01, to: 2026-10-23}
trial_budget: 30          # the family's own trials; prior (seeded) trials count only in the DSR
checks:                   # in-sample, each read from run.json (max_pct_adv from trades.csv)
  mar_min: 0.6
  longest_dd_sessions_below: 500
  trades_min: 100
  expectancy_r_min: 0.2
  cagr_above_benchmark: true        # cagr_pct > benchmark_cagr_pct
  cagr_ex_top5_min_share: 0.5       # cagr_ex_top5_trades_pct >= 0.5 x cagr_pct (cagr > 0)
  max_pct_adv: 5                    # every entry's position_pct_adv; an entry without one fails
  binding_required: true            # binding.no_op false
  dsr_prob_min: 0.95                # the DSR at the family's trials today, not at completion
  pbo_max: 0.5                      # the PBO over the family's runs today
plateau:                  # rows from the --sweep runs that differ from the candidate in one path
  mar_min: 0.5
  parameters:             # a config.resolved path as config.overrides names it
    - {path: strategy.exits.4.lookback_days, step: 5}   # value - 5 and value + 5
    - {path: strategy.exits.2.enabled}                  # a boolean: its negation
hold_out_checks:          # the complete hold-out run validating the in-sample run, read once
  expectancy_min_share: 0.5         # >= 0.5 x in-sample expectancy_r (in-sample > 0)
  mar_min: 0.4
```

## Reading a run

In `HAT_DATA_ROOT/backtests/runs/<run_id>/`, at `GET /api/hat/backtests/runs/<run_id>`, or on its
page `/hat/backtests/<run_id>` (both audits' findings above the metrics; the runs list flags a run
whose audit warns or fails):

- `audit.json` first (`GET .../audits/audit`): `status` and each check's findings. Then
  `data_audit.json` (`GET .../audits/data_audit`), the data audit the run read.
- `run.json`: `metrics` (whether; `known_gaps` names only the estimates the run took),
  `binding` (did the change fire), `breakdowns` (by year with the entries decided on 5-minute
  bars and on the daily estimate, regime, exit, industry, liquidity, playbook with its signals
  and refusals), `refusals` (what the risk gates blocked).
- `trades.csv`, `equity.csv`, `refusals.csv` (with forward R of each blocked entry),
  `signals.csv`, `stocks.csv`: why.
- `config.json`: everything the run used, enough to rerun it.
- A `book_vcp` run's detector audit: `python scripts/research/s1_book_audit.py --run-id <id>
  [--seed N]` replays the run's nightly scan (no day loop, its funnel checked against
  `run.json`) and writes its entries, seeded near-misses of the base geometry with charts, and
  the Depth_1 gate table to `reports/<variant>/audit/` (gitignored; needs matplotlib).
- A run with a pre-open playbook also writes `ep_candidates.csv` and the
  `breakdowns.episodic_pivot` tables (result-standard.md). Its audit: `python
  scripts/research/s2_ep_audit.py --run-id <id> [--seed N]` samples 20 EP entries and 10 near
  misses (seed printed) with a 5-minute and a daily chart each, to `reports/s2-ep/audit/`. The
  intraday-results report: `python scripts/research/s2_ep_intraday_results.py
  --strategy-version <id> --universe <name> [--from D] [--to D]` (in-sample only), to
  `reports/s2-ep/b7/`.

### Data audit (`data_audit.json`, `app/hat/data/audit.py`)

| Check | Fails | Warns |
| --- | --- | --- |
| `calendar` | a stored day that is no NSE session, or a session with no bars | period ends after the data |
| `missing_sessions` | members miss > 2% of their member sessions overall | a member misses > 5% |
| `survivorship` | a leaver with no bars, or no leaver in a window of a year or more | a leaver with bars on < 95% of its member days |
| `corporate_actions` | an adjustment factor that is not a positive number | an adjusted move > 20% on an ex-date; an action-shaped raw move (a split or bonus ratio) with no factor |
| `lookahead` | a stored feature or breadth of D that differs from its recount on bars <= D; a sampled member day with a bar and no feature row | a session whose previous session has no index close or breadth (the gates read D-1) |
| `unique_keys` | a duplicate bar, features row or index close | |
| `bars_5m` | | over the whole 5-minute store: a day with bars off the IST 5-minute grid, without a daily bar, or whose last close is > 5% off the daily close (`bar_5m_adjusted_basis` when the ratio is the stored factors after the day); a run decides such a day on the daily estimate (`intraday.on_basis`) |
| `industries` | | > 5% of the member-days have no industry (`unknown`); a run (a sweep variant alone) whose policy caps positions per industry below its open positions while any member-day has no industry is refused `backtest_industry_cap_without_industries` |
| `demergers` | | a member demerger whose ex-day open and close gaps differ > 15% and whose factor is not the close gap; the summary counts the period's undated member demergers (no `announced_on`) |
| `golden` | a golden sample value drifted or missing; an unreadable golden file | an unresolvable key; a recorded mismatch still open; no golden file yet |

### Run audit (`audit.json`, `app/hat/backtest/run_audit.py`)

| Check | Fails | Warns |
| --- | --- | --- |
| `binding` | | `no_op`, or a base without a binding; `one_change`: more than one knob changed against the base (a declared bundle's `reason` beside it; a base that predates knobs is counted in changed resolved paths); `policy_drift`: a risk policy version other than the base's |
| `fills` | a fill outside its day's low-high (widened by slippage); an entry without a bar | |
| `sequence` | signal > entry, or an exit before the entry | |
| `pnl` | net != gross - fees; R != net / initial risk; trades, P&L vs final equity, fees, expectancy or win % disagree with `run.json` | |
| `costs` | a trade without fees | cost share > 50% of gross profit |
| `liquidity` | `position_pct_adv` wrong | an entry > 5% of 20-day traded value |
| `concentration` | top-5 share disagrees | top 5 trades > 50% of net P&L; CAGR not positive without them |
| `equity_jumps` | a > 5% session move with no position held (one opened and closed that session counts) | a move beyond 20% of the value held |
| `refusals` | `refusals.csv` counts differ from `run.json` | |
| `trials` | no `dsr_n_trials` | no DSR |
| `data` | no data audit | the data audit warned (naming its checks and the owner's explanation, if any) |

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
  - label: exits-b
    base: base
    change: SMA50 close exit and no time stop
    bundle: true                     # more than one knob: rule 3
    reason: the pair the owner trades as one exit set
    set: ['exits[close_below_sma].sma=50', 'exits[time_stop].enabled=false']
```

A variant's keys: `label`, `base`, `change`, `bundle` and `reason` (rule 3), `validates`
(hold-out), and any knob below. The command line's options apply to every variant; a variant's
keys replace them for that variant. A variant's knobs are its own and the command line's: a
variant does not inherit its base variant's `set`s, so a chain repeats them.

## Knobs

`set` paths edit the strategy version's config document (`engine/strategy/config.py`): a list
item is named by index, by its `rule`/`name`/`type`, `#n` for the n-th of a type held twice
(`criteria[rs_rank#1]`), `[+]` appends; a missing or ambiguous name, or a key the config model
does not know, is refused. `policy_set` paths edit the risk policy (`risk/policy.py`). A sweep
with a `set` or `policy_set` path that names nothing fails as a whole before any variant runs. Sweep column: the variants-file key; the command
line flag sets the same for one run or every variant.

| Knob | Variants file | Command line |
| --- | --- | --- |
| Setup criteria and params, mode, weight, on/off, add | `set: ['playbooks[default].setup.criteria[vcp].params.contractions=[3,6]']`, `...criteria[vcp].enabled=false`, `...setup.threshold=0.7` | `--set` |
| Universe filters | `set: ['universe[min_turnover].params.inr_cr=20']` | `--set` |
| Trigger and entry timing, breakout volume | `set: ['playbooks[default].trigger[price_breaks_level].params.entry_timing=daily_close']`, `...params.volume_vs_avg=1.5` | `--set` |
| Entry filters | `set: ['playbooks[default].entry_filters[max_chase].params.pct=2']`, `...[adtr_filter].params.max_multiple=1` (a very wide chase, e.g. 100, is off) | `--set` |
| Initial stop and its cap | `set: ['playbooks[default].stop={"rule": "atr_multiple", "days": 14, "multiple": 2, "max_pct": 8}']`, `...stop.max_pct=10` | `--set` |
| Exits (partial, breakeven, step trail, SMA/EMA close, swing low, ATR trail, time stop, early failure, max hold) | `set: ['exits[step_trail].step_r=1.5', 'exits[time_stop].enabled=false', 'exits[+]={"rule": "early_failure", "days": 5}']` | `--set` |
| SMA exit grace window (no SMA exit in the first N sessions after the entry session; stops still act; unset = off) | `set: ['exits[close_below_sma].grace_sessions=10']` | `--set` |
| Regime gate: rule, params, index | `set: ['regime={"index": "NIFTY 500", "rule": "breadth_above", "params": {"min_pct": 55}}']`, `regime.params.sma=100` | `--set` |
| Regime sizing (1 or the live regime engine) | `regime_mult: policy` | `--regime-mult` |
| Risk per trade | `risk_pct: 0.005` | `--risk-pct` |
| Sleeve weight | `sleeve_weight: 0.5` | `--sleeve-weight` |
| Max positions (and the ladder's top level) | `max_positions: 10` | `--max-positions` |
| Per-industry cap | `max_per_industry: 2` | `--max-per-industry` |
| Ladder off (one size from day one) | `flat_ladder: true` | `--flat-ladder` |
| Ladder levels, open risk, cluster cap, entries and stopouts per day, cooldown, ADV cap, multiplier bounds | `policy_set: ['exposure_ladder.L2.max_positions=6', 'global_open_risk_pct=0.06', 'cooldown_sessions=5']` | `--set-policy`, `--cooldown-sessions` (wins over the policy's) |
| A whole candidate policy | `policy: cand.yaml` | `--policy` |
| Slot order of a capped day: `time` (default), `score`, `rs` (the signal night's `rs_weighted`, highest first; promotion allows `time` and `rs`), `random:K` | `slot_order: rs` | `--slot-order` |
| Entry fill rule, ambiguous days, suspension markdown | `entry_fill: touch`, `ambiguous: pessimistic`, `suspension_sessions: 3` | `--entry-fill`, `--ambiguous`, `--suspension-sessions` |
| Start capital | `start_capital: 2500000` | `--start-capital` |
| Entries on the daily estimate only (a comparison, never evidence) | `entry_bars: daily` | `--entry-bars` |
| `volume_pace` on the full day's volume on 5-minute sessions too (the daily estimate always uses it; a look-ahead, known gap `volume_pace_on_full_day_volume`) | `volume_pace: full_day` | `--volume-pace` |
| Slippage, cost model | one sweep per value | `--slippage-bps` (20 by default), `--cost-model` |
| Universe, window | one sweep per value (a sweep reads one market) | `--universe`, `--from`, `--to` |

Not knobs without a code change (strategy or engine changes, shared with live): the live regime
engine's thresholds and multiplier table (`engine/portfolio/regime.py`, under `regime_mult:
policy`), the allocator's smoothing, the breadth basis (% above SMA50), the RS weights and other
feature windows (a features rebuild), the VCP score blend, a breakeven offset, and simulated kill
switches and health (known gaps: `policy_set` on them has no effect).
