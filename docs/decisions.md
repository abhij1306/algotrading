# Decisions in force

Owner decisions that bind the code, each with its reason. A reversed decision is deleted. The
rules they produce are in `invariants.md`; the numbers live in the code and the risk policy.

## Product

- **Narrow scope.** NSE cash equities, one user, Fyers only (HAT's own `HatBroker` adapter), one
  allowlisted Telegram chat, long only, delivery (CNC).
- **Two trading sleeves.** S1 `stage2-contraction-breakout` (Minervini-style) and S2
  `momentum-breakout-ep` (Qullamaggie-style breakout and episodic pivot): well-documented styles
  that can be written as rules. S3 `darvas-box-breakout`, S4 `trend-pullback-ema` and S5
  `rsi2-uptrend-reversion` are Research only (Strategy and risk).
- **No manual trading.** No order ticket, order route or manual exit button: discipline is the
  product; `/flatten` is the one emergency exit, recorded as a breach.
- **Modes: shadow, confirm, auto.** A bot runs shadow until its sleeve is Live and every account
  and policy gate passes; confirm sends a card per entry; auto places only high-confidence signals
  and is a personal switch, not a product feature. Medium and low go to the owner.
- **Confidence is evidence.** A signal's label is fixed at trigger from its bucket (playbook,
  setup-score band) in the bot's latest evidence run: high needs 30+ trades with a positive 90%
  lower bound on mean R. Labels must come from measured results, not opinion.
- **Earn real money in stages.** At least five years of backtest on real data, 20 sessions of
  shadow, then owner-approved trades; promotion needs deflated-Sharpe probability ≥ 0.95 and 200
  trades, and every backtest run counts as a trial. Research -> Backtested also needs the family's
  probability of backtest overfitting (PBO, stored at the run's completion) below 0.5 (QA6); an
  unscored PBO (fewer than two distinct variants) fails.

## Strategy and risk

- **Criteria are data.** A parameterised criteria library; strategy versions are immutable JSONB
  with YAML import/export, so a change is a new version with its own evidence.
- **Risk lives in one versioned policy.** 0.25% of bot equity per trade from the stop distance;
  position caps S1 20%, S2 25%; ADV cap 1%; global open risk 1.5%; 6 positions per bot, 2 per
  industry, 3 entries and 3 stop-outs a day; an exposure ladder stepping on wins and losses;
  10% drawdown kills (account and bot) on equity marked to market intraday. Bots hold no risk
  numbers, so no strategy can loosen risk.
- **Pre-trading policy reset (owner 2026-10-26).** Until anything trades for real (no real
  order, position or closed trade in the database), the owner replaces the policy at once with
  `admin set-policy --file` (outside market hours), with no cooling-off or evidence gate: there
  is no live record to protect, and the seed's numbers are too low for a swing strategy. The
  first real order ends it; every change after takes the gated path.
- **The risk-increase gate (QA8).** Besides live expectancy against the discounted backtest
  expectancy, each sleeve's evidence run needs five years of sessions, no known gap naming an
  enabled playbook and ambiguous days within the cap, the Research -> Backtested conditions. A
  decrease restarting the trade window is intended: each step up earns its own evidence.
- **S3 `darvas-box-breakout` is Research only.** Darvas-style (`darvas_box`: by the seed's settings,
  which a version may change, the box top is the 252-session high and the box at most 60 sessions
  old; break of the box top on volume, stop under the box bottom). It holds the strictest
  existing numbers and no capital: seed `p_max` 20%, base weight 0, `max_chase` 2%. Nothing turns
  it on; trading it needs the owner's policy version and the lifecycle gates.
- **S4 `trend-pullback-ema` is Research only.** Trend pullback (an RS leader, `rs_rank` 80, above
  a rising SMA50 and SMA200, pulls back 2-10 sessions on lighter volume to a rising 10 or 20 EMA,
  `ema_pullback`; entry on the next session's break of the pullback day's high, stop under the
  pullback low). Same footing as S3: seed `p_max` 20%, base weight 0, `max_chase` 2%.
- **S5 `rsi2-uptrend-reversion` is Research only.** Connors-style mean reversion, a return driver
  apart from the momentum sleeves: a close above the SMA200 with RSI(2) under 10 (`rsi_below`),
  entry on the next session's trade back above the signal close, out on a close above the SMA5
  (`close_above_sma`) or after 10 sessions. Connors uses no stop; the wide 3 x ATR(14) hard stop is a HAT
  choice, not the textbook.
  Same footing as S3: seed `p_max` 20%, base weight 0, `max_chase` 2%.
- **Stocks only (owner 2026-10-25).** HAT trades ordinary equity shares only: ETFs, fund units,
  InvIT and REIT units and rights entitlements never enter any strategy's universe, live, gap
  scan, nightly scan or backtest alike (`watchlist._tradable_members`, by ISIN:
  `isin.ordinary_equity`). They reach the store because NSE lists them in the EQ series beside
  stocks; their price data stays.
- **S4 research tests two exits (owner 2026-10-25).** S4's research must test both a close below
  the 10 or 20 EMA exit (`close_below_ema`) and the seed's close below the SMA50 before either is
  chosen; the seed keeps the SMA50 until then.
- **S1 research is config only (owner 2026-10-25).** Every parameter the S1 research varies is
  settable in the version YAML or by `--set` (runbook, Research variants): the VCP's swing width,
  minimum base, dry-up baseline, noise floor and score floor, the step trail, the close-below-EMA
  exit, the entry timing with its breakout volume, and the regime without a slope test. The VCP is
  judged on its most recent contractions (at most `contractions[1]`), so the final contraction,
  pivot and dry-up are current.
- **Breadth regime rule (owner 2026-10-26).** A strategy's regime gate may read market breadth
  instead of an index (`breadth_above`: `min_pct` 50, optional `off_below_pct` hysteresis,
  `smooth_days` 1): S1's longest drawdown ran through the 2018-2020 midcap bear while NIFTY 500
  kept rising, so no index rule switched it off. Breadth is the features job's `breadth_pct`, the
  share of that day's NSE ordinary equity rows (delisted names included) whose close is above
  their SMA50, back to 2011. The gate for D reads the sessions through D-1, as the index rules do;
  live and backtest walk the same last 250 sessions, and a session without a value turns it off.
  The regime's `index` still names the market a backtest loads.
- **A pivot is used once (2026-10-25).** The nightly scan, live and backtest alike, publishes
  no row for an instrument the bot holds (an open position in any state, virtual or real), and
  none for a setup a position of the bot was already opened on: a re-entry needs a new setup.
  A setup is named by (instrument, `pivot_date`): the date of the latest bar whose high is the
  setup's `pivot`, stored in `criteria_results` (`scan.pivot_date`, `scan.setup_taken`). A date,
  not the level, because a dividend rescales the history the level is read on; a new final
  contraction or box has a new pivot bar. The rule spans playbooks and versions of the bot. A
  setup whose pivot is no bar's high (`rsi_below`'s close) has no `pivot_date` and keeps only the
  held rule. Without it a held name was republished on its stale pivot (refused as
  `add_to_loser`) and re-entered on it after the exit. No parameter.
- **Entry timing and volume (owner 2026-10-25).** A level trigger enters on the intraday break
  (`entry_timing: intraday_break`, live's 5-minute trigger) or, as a backtest variant, at the daily
  close above the level (`daily_close`), filled at the official close as the close-phase exits are.
  A breakout volume test (`volume_vs_avg`) exists only at the close, where the day's volume is
  known; an intraday break refuses it rather than read the full day's volume. Live does not trade
  `daily_close` (the trigger fails closed) and `volume_pace` is not backtested: each is a known
  gap that names the playbook and blocks its promotion (`GAP_PLAYBOOKS`).
- **Sleeves and regime.** The unit is a sleeve (strategy version x universe version), base weights
  50/50 (S3, S4, S5 0). A rules-based regime (trend, India VIX, breadth; 4 states, 3-day confirm) scales weights
  within [0.25, 1.0] and blocks new entries only; it never promotes or retires a sleeve.
- **Exits.** Intraday stop on fill, swapped to a GTT at 15:10; MA exits 15:05-15:10 on the
  live price (a known gap to the official close, logged nightly); an ordered exit stack where
  stops only tighten.
- **Square-off before the closing auction (owner 2026-10-25).** NSE's closing auction session is
  live for every stock, so continuous trading ends 15 minutes before the session close (15:15 in
  a regular session) and HAT's square-off finishes before it: `exits.close` (real and shadow)
  15:05-15:10, the GTT swap 15:10, the trigger window ends 15:05, and no sale goes out from 15:15
  on (`SessionCalendar.continuous_end_on`; an early close or a Muhurat session moves them with its
  close). 15:15 is not the closing price: the official close is the auction's, settled around
  15:40. Every close-phase decision (MA exits, `close_above_sma`, failed-break closes, time stops)
  uses the live last trade or the last finished 5-minute bar at decision time, never the official
  close; what needs the official close (the nightly ingest, the close gaps, features) runs after
  15:40.
- **Shadow fills pay costs (QA4).** A virtual fill carries the backtest's default model: 20 bps
  slippage against the trade, then the versioned statutory charges (`costs.py`), so shadow
  results are net of what real trading pays.
- **Dated cost schedule.** A fill is charged under the file of its cost-model version in force on
  its trade date (`cost_models/cnc-delivery-1/`, from 2016-01-01, the backtest window's start);
  a run records the digest of every file in force in its window. Sources per file: STT 0.1% on
  delivery buys and sells (Finance Act 2013); NSE transaction charges 0.00325%, 0.00345% from
  2021-01-01, 0.00325% from 2023-04-01 (NSE press release 2023-03-24), 0.00322% from 2024-04-01
  (NSE, 2024-03-12), 0.00297% from 2024-10-01 (SEBI true-to-label circular, 2024-07-01); SEBI
  fee Rs 10 per crore, Rs 5 from 2020-06-01 to 2021-03-31 (SEBI PR 24/2020); service tax 14.5%,
  15% from 2016-06-01 (Krishi Kalyan cess), GST 18% from 2017-07-01; brokerage 0. Stamp duty
  before 2020-07-01 was a state levy: HAT charges the Maharashtra rate, 0.005% on both sides
  (Maharashtra Stamp Act Sch. I Art. 5); from 2020-07-01 the uniform 0.015% on buys. The DP
  charge is a flat Rs 14.75 per instrument per sell day (Rs 12.50 + 18% GST, today's tariff,
  used for the whole window): the first sale of an instrument on a trade date pays it, later
  sales that day do not (shadow: per broker account). NSE's IPFT contribution is not modelled.

## Data

- **Free official sources.** Nightly prices from the NSE MCP with the bhavcopy at 22:00 as
  fallback; history from bhavcopy archives since 2011; Fyers for live ticks and minute history.
  Research MCPs are chat-side only and never feed a scan or order.
- **Identity is the ISIN.** One instrument and one history per (market, ISIN) whatever the series;
  a new ISIN for the same shares is linked by evidence (`hat_isin_lineage`), never by name, and
  restructurings stay separate. A bad link corrupts every backtest, a missing one only shows as a
  review case.
- **Point-in-time index membership** (owner, 2026-10-25). Every backtest uses the index members
  as of each session, never today's list: a stock is a candidate from its inclusion's effective
  date until its exclusion's. NSE publishes only today's list, so the history is rebuilt outside
  the repo from the NSE Indices change notices, undone from today's list, checked against the
  member count and archived lists, and loaded as data (`hat_index_member`). A window the loaded
  history does not cover is refused, never filled with today's list. Demerger dummies (a
  resulting company held at zero price until it lists) are not modelled: they cannot be scanned.
- **5-minute history from the broker's export** (owner, 2026-10-25). `bars_5m` takes the
  broker's 5-minute history (five years, the NIFTY 500 members of 2026-10-23) through
  `bars5m_import`. Precedence: a stored (instrument, day) is kept; the import fills only the
  days the store lacks (missing, or refused by the minute download), so it overwrites nothing.
  The history labels a bar by its end through 2022-01-21 and by its start after; rows outside
  `[09:15, 15:30)` (flat pre-open rows, a post-close placeholder) are dropped, as the live
  builder drops those prints, and zero-volume rows are kept (`REJECT_RULES` has no such rule). It
  carries most splits, bonuses and rights but not most demergers (nor a few splits and bonuses),
  so each stored action is judged carried or not from the sessions before its ex-date, and every
  day must still fit the raw daily bar. Known gap: for 213 of the 497 symbols (the first
  downloaded) the days from 2026-08-03 have no 15:15-15:25 bars. Those days are written when they
  fit; the stored Fyers days cover them to 2026-10-21. A rule that needs the closing bars (the
  close's volume, a last-bar exit) must treat a day without a 15:25 bar as incomplete.
- **Backtests from 2016-01-01** (owner, 2026-10-25). The NIFTY 500 history starts 2016-01-01:
  the 2011-2015 notices are incomplete (they do not fit the archived lists of 2014 and 2015).
  Until that membership is resolved, a backtest's first session is 2016-01-01 or later; a run
  starting earlier is refused `backtest_starts_before_membership@2016-01-01`. Bars before 2016
  still warm the indicators up.
- **Industry history (orchestrator, 2026-10-26).** One taxonomy: the `Industry` column of NSE's
  current NIFTY 500 list. Each instrument chain takes its industry from the NIFTY 500 list
  snapshots that list one of its ISINs in that taxonomy (the lists from 2022; the 2010-2020
  CNX 500 lists use another), on its current ISIN: the first is in force from the chain's first
  bar, and a later snapshot under another industry appends a reclassification from its capture
  day. A chain only the old lists name takes the current label its old label maps to where the
  mapping is unambiguous (at least 90% of 3 or more chains both lists name agree), else it stays
  unknown; nothing is guessed. The industry is the company's, so a first classification is
  never dated at the load: the nightly reference load dates a new chain's first from its
  listing and appends changes from the load day. Why: a load-dated first row left every earlier
  day `unknown`, so by-industry results were empty and the per-industry cap capped the whole
  book. The data audit warns above 5% unknown member-days and fails a run whose per-industry
  cap is below its open positions while any member-day is unknown. Loaded by
  `load-industry-history` (runbook).
- **Total return for signals, price basis for money.** Signals read dividend-adjusted series so a
  dividend never looks like a fall; fills and orders use the prices the broker sees. Every
  dividend counts: leaving small ones out biases returns down.
- **Demergers by the market gap.** The book-value CoA % is a tax allocation and the ratio depends
  on a later listing; the gap is what a holder lost that morning. The NIFTY 500-neutralised gap
  waits for owner approval.
- **Exit before a demerger.** HAT learns a demerger's factor only from the gap after the event, so
  a position exits the session before the ex-date (under T+1 it gets no resulting shares, by
  intent); live sells a real one only through the owner (`/flatten`). No entry from that session
  until the factor is stored: a buy that day is sold at its close, and after the ex-date the
  history waits for the factor, as the backtest's `factor_pending` does. A notice the `bc` file
  stops listing is alerted, not retired, until archived files show the file lists a demerger
  every night up to its ex-date: a wrongly retired notice would hold a position through the
  demerger. The nightly keeps each night's `bc` file, dated, under the data root from
  2026-10-25 (owner 2026-10-25), so a later change can check that and retire moved notices.
- **The rescale waits for the fill sync.** A fill of the previous session booked after the rescale
  would land on the new scale, so `ca.rescale` waits for each real position's account to read its
  fills since the last close, until 09:00; then the job fails URGENT and the position stays
  manage-only rather than rescale on a quantity that may be stale.
- **An unexplained split-like gap waits for the owner.** A held name that opens 40% down with no
  stored action is more likely an unstored split than a crash: selling the old quantity at the
  new price against the old stop is the costly mistake, so it is manage-only plus URGENT until the
  owner stores the action or resolves it (a real crash then waits for the owner too). Owner
  2026-10-25: a rights entitlement (`-RE`) is not watched, as it trades on its own terms and gaps
  with no action (20 such gaps since 2024); a stored dividend never explains a gap, however large
  (SETCO 2026-06-02 stays flagged), until the dividend feed (CA3) is reviewed.
- **KAUSHALYA stays two histories (owner 2026-10-25).** Its 100:1 consolidation (INE234I01010 to
  INE234I01028, 2024-02-06) exceeds the stored factor cap (`FACTOR_MAX`), so the pair stays an
  unlinked lineage review row and the new ISIN starts its own history. That trips no guard and
  qualifies it for nothing: no rule reads a listing date, history-length rules fail closed on a
  short chain, its first bar was not quarantined, `lineage.review` fired once, and no strategy's
  turnover floor (10 crore over 20 sessions) admits either ISIN (best 1.1 crore).
- **A position across an ISIN change waits for the owner.** The old ISIN gets no ticks and the
  new one may hold another quantity, so the position is manage-only plus URGENT and its chain takes
  no entry; HAT never moves a position to the successor instrument by itself.
- **Point in time, not full as-of rebuild.** The series stays the hindsight view; the backtest
  skips names whose value was not yet knowable (`known_on`, versions) instead of rebuilding the
  series per date.
- **A long gap breaks the series (D5, 2026-10-25).** An instrument back after more than 92
  calendar days without a bar (`series_break.SERIES_BREAK_DAYS`, a quarter, about 63 sessions)
  starts a new series: features, criteria, entry stop and breakout-volume windows read only the
  rows since, so they are null or short until they refill (a 200-day SMA needs 200 new rows).
  Shorter halts stay one series. Counted in calendar days between the instrument's own bars, so a
  full rebuild, an incremental run and a scan read the same; a held position's exit history is
  not cut.
- **The benchmark is NIFTY 500 TRI (D6).** A run's benchmark is the total-return index, as its
  equity earns dividends; the regime reading stays on the NIFTY 500 price index. A session the
  index lacks gives no benchmark rather than the last close carried over.
- **Owner corrections over automation.** Wrong factors, amounts and dates are fixed by the owner's
  journalled corrections and reviewed lists with bar evidence; loads never overwrite them, and
  nothing is guessed.
- **Storage.** Postgres is the system of record (fresh databases, no migration tool, uuid v7 keys,
  `Numeric(18,4)`); price history is date-partitioned Parquet read through DuckDB; indicators are
  own polars code.
- **Stock data, state and configs apart (owner, 2026-10-26).** hat_live is the canonical stock
  database: it holds the data and only the data jobs and importers write it (read-only for every
  other connection). Trading and runtime state goes to hat_state, a second database on the same
  server with the same credentials and its own upkeep. The configs (strategies and their versions,
  risk policy versions, broker accounts, bots, regime models, the criterion catalogue) are
  versioned JSON revision files under `HAT_DATA_ROOT/admin/config`, never edited once written,
  with an in-force pointer per kind and its history; ids are unchanged. So a data rebuild can
  never touch a trade, every config change is a reviewable file, and a stray write to the stock
  data fails loudly. Keys across them are plain columns checked in code. The watchlist is nightly
  scan output and lives in hat_state, not in the configs. `python -m app.hat.ops.state_split`
  moved the rows once (runbook); hat_live's legacy tables stay until the owner drops them.
- **Evidence defaults (QA7).** The DSR family floors N ≥ 5 trials and trial-Sharpe variance
  V ≥ 0.25/252 (`dsr.py`); ambiguous days at most 10% of the sessions a trade was held
  (`lifecycle.py`); `max_chase` 2% by default, 5% for stage2-contraction-breakout and 2% for
  momentum-breakout-ep, darvas-box-breakout, trend-pullback-ema and rsi2-uptrend-reversion
  (`caps.py`).
- **Backtest max-chase and regime table (Q14).** A run reads `max_chase` from its pinned strategy
  version, while live reads the newest version holding it: a version's evidence stays its own.
  The regime multipliers are the stored table live allocates with, for the strategy's registered
  `regime` profile, read when the run starts and recorded in its hashed conventions.
- **Backtest gaps are listed.** Behaviours the backtest does not model are in `KNOWN_GAPS`
  (`backtest/spec.py`) and reported by every run; no fixture pins a result.
- **The backtest's close phase decides on the daily close (owner 2026-10-25).** The backtest's
  close-phase rules decide and fill on the daily close, simple and the same over all history; no
  5-minute bars and no count of estimated exits. The daily close (the auction price) stands in for
  the price live acts on before the 15:15 cut-off: a small, consistent bias, the known gap
  `close_rules_decided_on_official_close`. It is not symmetric: on an F&O monthly expiry and on
  the close before an index reconstitution takes effect the auction close can sit much further
  from the 15:05 price, so the run flags each close-priced exit on those days (`auction_risk` per
  trade in `trades.csv`) to be discounted, the known gap
  `close_bias_large_on_expiry_and_rebalance_days`. Both kinds of day are derived by rule from the
  stored sessions (`backtest/auction_days.py`, sources there).

## Operations

- **Self-hosted, one machine.** Always-on host with an ISP static IP registered with Fyers, a UPS
  and a 4G link for alerts and login only (Fyers refuses orders from an unregistered IP); secrets
  in `.env` (0600).
- **Manual daily login.** The owner logs in to Fyers each morning from a Telegram link (08:30,
  repeated 09:05); no entries before.
- **One active order broker.** `HAT_BROKER` (`fyers`, the default, or `zerodha`) names the one
  broker HAT places orders with; an unknown name refuses the start. Zerodha is an alternative to
  Fyers, never a second broker beside it: no per-account routing and no fund split; only the
  active broker's adapter is built and only its accounts can be onboarded. Market data stays on
  Fyers, so Fyers is logged in every day either way; with Zerodha active the owner also logs in to
  Kite (its token lasts until 06:00 IST) and pastes the `request_token`.
- **Zerodha stays shadow until checked live.** The Kite adapter reports no GTT delivery
  (`kite_adapter.KITE_CAPABILITIES`), so a Zerodha account runs shadow until a live check of its
  GTT stops, order tags and reply shapes (published Kite v3 shapes only so far) sets it.
- **No API auth.** Loopback bind behind the VPN, read-only routes; the broker login writes (Fyers
  and Zerodha) and the backtest job start (research runs only) need the `X-HAT-Request` header
  and a frontend `Origin` (`api/guard.py`).
- **Engine.** In-process scheduler with jobs in Postgres, one async session owner per broker
  account, one engine for backtest and live, so the backtest tests the code that trades.
- **A runtime that lost its lock writes nothing.** When another runtime holds the runtime lock,
  this one stops its jobs, its periodic order steps and its broker sessions (stops and sales
  too) and alerts `runtime.lock_lost`: the lock holder owns exits, so two runtimes never manage
  one position. While the lock is merely unreachable (the database down) it only refuses entries
  and tries again each minute.
- **Verification on real data.** See `CLAUDE.md`.
- **Backtest results are files, never database rows (owner, 2026-10-26).** Every run writes one
  folder in the Backtest Result Standard (`docs/backtests/result-standard.md`, v1.3) under
  `HAT_DATA_ROOT/backtests/runs/<run_id>/`, with `config.json` to rerun it, and the DSR's trial
  count in `backtests/trials.json` beside them; `hat_live` is read-only to every backtest, a
  promotion run too. So results compare across strategies and sweeps in one shape, a refused
  entry's cost is measured, and a backtest cannot touch the live database. Promotion reads run
  folders: a completed run whose run audit (`audit.json`) passed or warned, through its
  `evidence.json` (metrics, trade buckets), evidences `publish-version --run <run id>` (only a run
  flagged `for_promotion`), the risk-increase gate and signal confidence; a run whose audit failed
  or is missing is no evidence. A strategy version names its run by id.
- **One backtest process, audited (owner, 2026-10-26).** Every backtest follows
  `docs/backtests/process.md`: a data audit before every run (a failed one refuses the run), a run
  audit after it (`audit.json`), one change per variant against a named base within a trial
  budget, promotion criteria fixed in the strategy's notes before any run, a plateau check, an
  independent review report, the hold-out read once, then paper trading. Every knob is set from
  config or the command line, so a backtest needs a code change only when a strategy changes or is
  added.

## Execution safety

- **A refused fill is booked (Q2).** A fill the lifecycle has no step for is stored as a broker
  fact and alerted URGENT: shares the broker holds must be tracked and under a stop, never left
  unbooked. A reopened trade's result is rewritten at its second close; the ladder step of its
  first close stands.
- **Lost earlier-session sales (Q12).** Reconciliation records the owner's own shares per symbol
  (the holding baseline) and the settle nets them out, so HAT never sells the owner's shares;
  the owner books the missing fill from the contract note (`admin resolve-sale`), since no
  broker read gives an earlier session's trades.
- **No dormant real mode (Q9).** Confirm or auto for a sleeve that is not Live is refused; the
  owner asks again after promotion, so a promotion never turns on real orders by itself.
- **A strategy holds no mode (Q10).** The mode is the bot's alone (its record's `execution_mode`, by
  `/mode`); the strategy config's `execution_mode`, never read, is removed so no file suggests
  otherwise.
- **Failed-break closes survive a restart (Q7).** The day's closes are rebuilt from the stored
  `bars_5m`; the refusal fails open only when no bars are stored, since refusing every Take
  until the next close would block good entries for a data gap.
- **A spent day stops entries, never exits (Q11).** When the day's broker calls are spent (the
  budget's day cap or Fyers' `-360`), new entries and the reads that can wait stop, with one
  URGENT `broker.budget_spent` per account and day; stops and sales keep the calls left, since a
  refused entry costs a trade while a refused stop costs money.
- **An illiquid name's exits use its last trade (QA15).** A symbol silent for 30 s while the feed
  is fresh keeps its last trade of the day (inside the band) for the exit views only; entries
  still need a fresh print, since a missed entry is safe and an exit that never fires is not.
- **A re-arm keeps the drawdown peak (QA10, owner 2026-10-25).** A manual re-arm of a drawdown
  or manual kill does not restart the peak: the account and bot drawdowns and the health
  multiplier are measured from the peak before it, so 9% down, a re-arm, then another 9% trips
  the 10% kill. A re-arm that restarted the peak would let repeated re-arms walk the equity down
  without the kill ever tripping. A drawdown kill re-armed while still past its threshold trips
  again at the next point.
- **A late fill after `/flatten` is sold (Q13).** The follow-up sale goes under the original
  confirmation: the owner asked for no exposure, and the shares are protected meanwhile.

- **Equity is the available cash plus the holdings at market (Q1, owner 2026-10-25).** Read at
  the evening allocate from the broker's funds (Fyers "Available Balance", Kite `net`), its
  holdings and today's net CNC positions, each share at the broker's last price; never "Total
  Balance". After a CNC buy the cost leaves the available cash and sits in "Utilized" until
  settlement, so a total read keeps it as cash on the trade date and drops it at settlement while
  the shares were bought on the trade date; on this basis the buy turns cash into shares (today's
  position) on the trade date and settlement changes nothing (the same shares, now a holding).
  The owner's own holdings in the account count. The allocator takes the book equity: HAT's own
  open positions at their cost, since the drawdown marks them itself. A sleeve's capital is its
  weight x that equity, which already carries the real books' realised P&L, so sizing adds none;
  only a virtual (shadow) book adds its own. The drawdown adds the realised P&L of trades closed
  after that read and the open positions marked to market, and restates its peak only for a
  change of weight: a change of the book equity (a result, a deposit or a withdrawal, the owner's
  holdings moving) moves the drawdown, and a sleeve's realised result reaches its capital shared
  by weight. The first live session confirms the basis (runbook, First live session checks).

## Planned, not built

Regime v2 (HMM/GMM), fundamentals from XBRL, screener universes, US markets, a Kotak
adapter, Kite market data, multi-user; PDF summaries on cards and a chat
MCP server (advisory only); a CLI to re-run one job for a past trade date; a weekly timeframe;
live trading of `entry_timing: daily_close`.

## Defaults awaiting owner confirmation

Chosen where the owner has not ruled; the code implements them.

- Universe: NIFTY 500 plus liquidity criteria.
- Confidence: the 30 trades, the band edges (0.60/0.75/0.90) and the raw (undiscounted) mean.
- Regime: first day bootstraps confirmed; percentile over the trailing 252 values; precedence
  Bear > Correction > Bull volatile > Bull quiet.
- Lifecycle: Probation to Live by owner promotion only; health 0 means Probation; discount 0.58;
  two-sided Student-t CI on mean R; an undefined DSR is 0 (fail closed).
- Criteria: an empty setup score is 1.0; linear score curves; a month is 21 sessions; F&O names are
  those with no price band; volume pace baseline the 50-day average.
- Data: volume adjusts by the cumulative factor; daily-bar trigger proxy without minute history.
- Ops: MCP backoff 1 s doubling to 30 s; NTP offset at most 1 s; a 2% fallback stop; statutory
  costs as versioned config.
- Shadow results do not move the exposure ladder.
- Research variants: a trail's start, the early-failure exit and the ATR trail read the best price
  since entry from the highs of the sessions from the entry session on and the decision price; a
  run's `by_regime` reads the run's own regime rule at entry and the breadth of the session
  before (docs/backtests/result-standard.md), not the regime engine's state; `--max-positions N` sets the account
  cap and the exposure ladder's top level to N and caps its lower levels at N; `--flat-ladder`
  sets every level to the run's cap at risk multiplier 1 (owner, 2026-10-25: measure S1 at a fixed
  size, since at a ~35% win rate the ladder rarely leaves L1), the other limits as the policy says.
- VCP (S1): `min_depth_pct` off (every 3-bar swing counts) until the owner picks a noise floor;
  each tightness sub-score is `score_floor` 0.5 at its pass limit, so a pattern that only just
  passes scores 0.625, not 0.25 (the S1 threshold 0.6 then gates on RS and the tighter patterns).
