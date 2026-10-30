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
  10% drawdown kills (account and bot) on equity marked to market intraday; no position under
  2% of the sleeve's equity (`min_position_pct`: a crumb position dilutes expectancy and holds
  a slot a real signal needs; a policy document without the field reads as 2%). Bots hold no
  risk numbers, so no strategy can loosen risk.
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
  stocks; their price data stays. Index membership is stored as NSE publishes it, REIT and InvIT
  units included wherever HAT has an instrument for them (EMBASSY and BIRET in the NIFTY 500
  from 2026-09-30; BAGMANE has none, as it trades only in the RR series the instrument scan does
  not read), and the universes drop them by ISIN.
- **S4 research tests two exits (owner 2026-10-25).** S4's research must test both a close below
  the 10 or 20 EMA exit (`close_below_ema`) and the seed's close below the SMA50 before either is
  chosen; the seed keeps the SMA50 until then.
- **S1 research is config only (owner 2026-10-25).** Every parameter the S1 research varies is
  settable in the version YAML or by `--set` (runbook, Research variants): the VCP's swing width,
  minimum base, dry-up baseline, noise floor and score floor, the step trail, the close-below-EMA
  exit, the entry timing with its breakout volume, and the regime without a slope test. The VCP is
  judged on its most recent contractions (at most `contractions[1]`), so the final contraction,
  pivot and dry-up are current.
- **S1 book rules (owner 2026-10-27).** The book's base is the hard criterion `book_vcp`
  (`engine/criteria/book.py`: H0, zigzag swings, contraction geometry and bands, base length,
  time compression, dry-up, relative correction against NIFTY 500 closes, base count), each rule
  its own parameter and failure reason, publishing `pivot` and `final_contraction_low` as `vcp`
  does; `vcp` stays for the stored configs. `rs_rank` `basis: eligible` ranks `rs_weighted`
  within the members passing the bot's universe stage that night, 1..99 (an approximation of the
  IBD RS rating, not from the book). The base count's stage-2 start reads the eight price checks
  of the trend template, without the RS rank: a criterion sees no earlier night's ranks. A run
  keeps each instrument's earlier-date geometry across its nights (`EvalContext.memo`), so the
  count is not recomputed nightly; a live scan computes it afresh.
- **S1 setups (owner 2026-10-30, Phase 1 of `docs/plans/s1-minervini-setups.md`).** Beside
  `book_vcp` (Setup 1, unchanged) the hard criteria `book_cheat` (cheat_3c / low_cheat),
  `book_cup_handle`, `book_power_play` and `book_primary_base` (`engine/criteria/setups.py`), each
  rule its own parameter and failure reason and its own funnel gates, on `book`'s zigzag,
  contractions, geometry rules, template and base count. Their tight area dries up on vr, the
  volume over the mean of the 50 sessions before it (`book_vcp` keeps its fixed 50-session base
  ending on the final contraction's first session). Shared criteria placed after a detector read
  the levels it published: `stop_cap` (stop the playbook's stop buffer under the tight area's low,
  at most 10% under the pivot), `overhead_supply` (rule B over 326-699 sessions back: the scan
  window is 700 sessions live and in the backtest, `scan.scan_window`) and `setup_tags`
  (shakeout, ath_pivot over the window, early_leader, stage; never fails; stage 3 and the cheat's
  H0 uptrend read the template's price checks, no earlier RS rank being in the history). Power
  play T is the highest high of the last 60 sessions; the detectors count the base number on the
  book's default params (Setup 1 is dropped, so no S1 `book_vcp` to follow); the primary base's
  age is the scan calendar's sessions since the instrument's first bar, gaps included, and a
  first bar on the window's first session or before is no recent listing. A tight final
  contraction confirms only once price leaves it, as in `book_vcp`. The plan's [mine] values are
  parameter defaults, logged per signal (`mine` in each criterion's extras).
- **S1 validity window (owner 2026-10-30).** A setup of a playbook with `valid_sessions` N,
  named by (playbook, instrument, `pivot_date`), is valid D+1..D+N from the night D it was first
  found (`hat_setup_window`, one row per setup and strategy version). Re-detection never extends
  it, and once closed it is never armed again: expired, triggered (any signal of its row:
  entered, skipped by the chase or gap rule, declined; or held; in the backtest also any skip
  after the level broke but a volume or pace one, `loop.consumes`), cancelled (a close under the
  final contraction's low less the stop buffer, a confirmed swing high above the pivot, no bar,
  out of the universe) or replaced by a newer pivot. A regime-off night ends every open window,
  live and in the backtest, and a setup found after it starts anew. An open setup not found again
  is carried (levels re-read through its pivot bar, tags and RS rank evaluated again).
  `one_setup_per_instrument` publishes one row per instrument (the first playbook in config
  order) and every row records `setup_tags`; the other setups keep their windows. The window and
  `valid_until` count the calendar's sessions. Each row's `criteria_results.setup` holds the
  plan's watchlist fields; `setup_type` and `valid_until` are columns. Slot order `leader_rs`:
  an early leader first, then `rs`. Entry trigger, chase and volume are the existing ones
  (`price_breaks_level`, `max_chase` 2% on the 09:20 price, `volume_pace` 1.5).
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
  `daily_close` (the trigger fails closed): a known gap that names the playbook and blocks its
  promotion (`GAP_PLAYBOOKS`).
- **`volume_pace` in the backtest (owner 2026-10-27, S1 book decision 7).** A 5-minute session
  replays live's pace. The daily estimate judges an enabled `volume_pace` on the full day's volume
  >= `vs_avg50` x the 50-session mean through the previous session (skips `volume_pace_low`,
  `volume_pace_missing`); `--volume-pace full_day` applies that rule to the 5-minute sessions
  too. Both read volume live does not know at the trigger: the known gap
  `volume_pace_on_full_day_volume` names the playbook and blocks its promotion.
- **RS slot order (owner 2026-10-27, S1 book).** `slot_order: rs` gives a capped day's surplus
  triggers the open slots by the signal night's `rs_weighted`, highest first. A promotion run may
  use it as well as `time`: no invariant ties evidence to the trigger-time order, and it is the
  book's sizing rule. Live still takes triggers as they fire, so on a capped day such evidence can
  differ from live until live ranks the same way.
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
  its trade date (`cost_models/cnc-delivery-2/`, from 2016-01-01, the backtest window's start);
  a run records the digest of every file in force in its window. The rates, their dates and
  sources: `docs/backtests/costs.md`. Stamp duty before 2020-07-01 was a state levy: HAT charges
  the Maharashtra rate. The DP charge is flat per instrument per sell day: the first sale of an
  instrument on a trade date pays it, later sales that day do not (shadow: per broker account).
- **Earnings catalyst (S2 EP, owner 2026-10-28).** `earnings_catalyst` (HAT-CRIT-127, trigger
  stage, mode hard) passes when a results filing became public in (the previous session's 09:15
  IST, the trigger instant] and its quarter's growth as known then (`hat_financial_growth`) has
  sales YoY of at least 25% and EPS YoY of at least 25%, or a turnaround, or sales YoY of at
  least 50%; a filing after the trigger is never read, undefined sales growth fails. Reported
  growth only: no analyst estimates are stored, so a run using it records
  `analyst_beat_not_evaluated`, and `catalyst_not_evaluated` stops applying to that playbook. A
  run whose config enables it is refused `backtest_results_missing` without stored filings. Every
  EP signal carries `catalyst_type` (`earnings`, `other_announcement`, `none`) and its sales and
  EPS growth, logged on every evaluation. Off in the stored S2 seed until the plan's Part C runs.
- **Time-anchored early volume (owner 2026-10-28).** `early_volume` `mode: time_anchored` needs
  ADV20 x min(1, minutes / 30), the minutes from 09:15 to the close of the trigger bar (the
  owner's formula: `vs_adv20` is not read in this mode), so a 09:20 break needs a sixth of the
  09:45 volume; `at_trigger` (the default, left out of the canonical form) keeps the flat
  `vs_adv20` multiple, so stored configs keep their hash.
- **Neglect direction (owner 2026-10-28).** `neglect` `direction: up_only` fails only a rise past
  the 63-session limit, so a stock that fell hard can still be a neglected gapper; `both`, the
  default, is unchanged. Every EP signal logs the 126-session range (max high / min low - 1).
- **Per-playbook universe and exits (owner 2026-10-28).** A playbook may carry its own
  `universe` and `exits`; without them it uses the bot's (`StrategyConfig.universe_for`,
  `exits_for`), so the breakout playbook's resolved config and exits are unchanged (the stored
  S2 version's canonical JSON and hash are byte-identical). A playbook's exit stack needs one
  `hard_stop`. A pre-open candidate must pass its playbook's effective universe at the gap scan,
  live and in the backtest (before this the scan judged universe membership only, never its
  criteria). `close_below_sma` `above_stop: true` acts only once the SMA is above the current
  stop; a close under such an SMA is also under the stop, which the hard stop has already taken,
  so the activation changes outcomes only where the stop moved above the SMA. Every EP signal
  logs its pre-gap ADR(20) (the repo's formula).
- **Momentum rotation as a strategy type (owner 2026-10-29; research `momentum/REPORT.md`).** A
  strategy config with a `rotation` block (`strategy.config.Rotation`) has no playbook or exit
  stack: on each calendar rebalance close (`month_end`, `fortnight`, `weekly`) it ranks the
  universe stage's passers (point-in-time members with a bar that close, at least `min_bars` bars
  in the last `long_sessions`) by a z-scored score on dividend-adjusted closes (`riskadj`: the
  mean of the cross-sectional z-scores of the 6- and 12-month returns over annualised volatility;
  or `r12_1`, `r6`, `hi52`), keeps holdings ranked within `buffer_rank`, fills to `top_n` best
  first and trades at the next open: equal target weights of the sleeve's capital, order diffs
  under `min_change_pct` of the target skipped (`reweight: false` never tops up or trims);
  `market_filter: true` sells everything and holds cash while the regime gate is off.
  Exits are by rank only (plus demerger, delisting and suspension as for every trade); no stop,
  slot, per-trade risk or open-risk cap applies. Rules: `engine/rotation.py` (pure, shared); the
  books: `backtest/rotation.py` and, live, `engine/rotation_live.py`. **Live on paper only (owner
  2026-10-29, W3):** the nightly scan ranks on a rebalance close (`rotation.planned`) and
  `rotation.rebalance` (09:20) selects at the open on the live prices, sizes on the book's equity
  at those prices (cash plus the holdings marked, the backtest's basis), sells and trims as
  virtual sales of the holding's lots (newest first; a sale's gain or loss reaches the shadow
  cash at once) and buys each name as a shadow signal through `evaluate_signal` (`rotation_buy`,
  refused unless the rotation playbook's and virtual: the kills, a pause, the market, feed and
  quality checks, the free cash; a top-up is a lot of its own). A name it may buy with no tick
  yet, or a stale feed, holds the whole rebalance back until 09:45 (with the open; retried each
  minute, alerted `rotation.unpriced`) rather than buy a lower-ranked name; after it a name still
  unpriced is passed over as the backtest passes over a name with no bar and the rest trades; every buy not made is alerted
  `rotation.buy_refused`; a waiting sale of a name back in the target is off. A bot whose mode is confirm or auto does not trade
  and is alerted `rotation.shadow_only`: real rotation orders need a stopless protective path and
  a sale card, not built. A rotation version may be a promotion run, so it can be frozen and run
  its trial on paper. The seed
  `momentum-rotation` is the research pick (riskadj, top 20, monthly, buffer 40, ADV20 >= Rs 10
  cr, close >= 50, 240 bars). Configs without the block keep their canonical JSON and hash.

- **Orders at 09:20, never at the open (owner 2026-10-30: "keep 9.20 at least").** No
  backtest order fills at the 09:15 open. A rotation's orders (buys, sells, trims, a waiting
  sale) trade at 09:20, as live's `rotation.rebalance` does, and a daily-estimate entry that
  gaps over its level fills at 09:20 too (the 5-minute replay and live never trigger before
  09:20:02, the first bar's close). The price at 09:20 is the last trade before it: the close of
  the 09:15 five-minute bar (live's last price at 09:20 is that close; the 09:20 bar's open is a
  trade after it). Without that bar (before the 5-minute store's 2021-10-26 start, a name
  without bars, a session the backtest refuses) it is the daily open moved against the order by
  the median move from the open to 09:20, buy side and sell side, measured on NIFTY 500
  member-days liquid as a rotation buys them (ADV20 >= Rs 10 cr, close >= 50) over
  2021-10-26..2024-12-31 (`scripts/research/open_to_0920.py`). One rule for all
  (`engine/fill_0920.py`), read by the backtest and the shadow-day replay; circuit, demerger and
  factor-pending refusals are judged at that price (the 09:15 bar, else the day as before). A
  run counts its rotation fills at real 09:20 prices and estimated ones (`rotation_fills_0920`,
  `rotation_fills_estimated`). **Measured 2026-10-30** on m3 (read-only, at 984e097):
  `venv/bin/python scripts/research/open_to_0920.py` (NIFTY 500 point in time,
  2021-10-26..2024-12-31, ADV20 >= Rs 10 cr, close >= 50, EQ/BE, sessions the backtest admits).
  All member-days: n = 310,189, median move from the open to 09:20 -0.0307% (q1 -0.479%, q3
  +0.429%, median |move| 0.455%), suggesting buy 0, sell 0.03%. Rebalance sessions (each
  month's first): n = 14,777, median +0.0235% (q1 -0.449%, q3 +0.540%, median |move| 0.491%),
  suggesting buy 0.02%, sell 0. Set conservatively per side, the larger of the two:
  `OPEN_TO_0920` buy 0.02%, sell 0.03% (`OPEN_TO_0920_MEASURED = True`). The ~0.46% median
  absolute move is noise around the open in either direction, which a constant cannot capture;
  it is not added as extra cost (the run's slippage applies as on every fill). Hard-stop exits
  that gap through at the open still fill at the open (`fills.stop_fill`), not part of this
  rule.

## Data

- **Tradable series: EQ and BE.** BE (trade-for-trade, delivery only) suits CNC swing trades, so
  it is tradable beside EQ (`broker_symbols.FYERS_SERIES`, the seeds' `series_in`); BZ (companies
  in breach of listing rules) never is.
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
  It keeps only days the store can vouch for: it refuses to run while a stored day is off the
  raw daily bar (`bars5m_repair` first).
  The history labels a bar by its end through 2022-01-21 and by its start after; rows outside
  `[09:15, 15:30)` (flat pre-open rows, a post-close placeholder) are dropped, as the live
  builder drops those prints, and zero-volume rows are kept (`REJECT_RULES` has no such rule). It
  carries most splits, bonuses and rights but not most demergers (nor a few splits and bonuses),
  so each stored action is judged carried or not from the sessions before its ex-date, and every
  day must still fit the raw daily bar. The export's prices carry the later actions but its
  volume does not (it is the bhavcopy's traded quantity as it was), so the import converts the
  prices only; days imported before 2026-10-30 had their volume cut by the factor and are
  restored from the export by `bars5m_volume` (each day only when the export's bars, the bhavcopy's volume and
  one common factor across the day's bars prove it). For 219 instruments the export ends at the 15:10 bar on
  every session of 2026-08-03..2026-10-28, and in 2026 its 09:15 bar opens at the pre-open auction
  price, often outside the bar's own high and low, so the import refuses it (`bars_5m_rejects`, rule
  `ohlc`). Such short days are refetched from Fyers (`bars5m_refetch`, owner 2026-10-30), each
  replaced whole only when Fyers' day passes the admission every 5-minute day meets (raw daily bar,
  replay grid, 09:15 open at NSE's daily open to a tick, 15:25 bar, no refused bucket missing,
  volume against the bhavcopy); a day that does not pass keeps its stored bars. On 2026-07-21 and
  2026-07-23 Fyers' 09:15 bar opens at the first continuous trade, not the auction: those 144
  written days keep their bars and are refused to the replay by `open_not_auction` gap rows
  (orchestrator, 2026-10-30; no restore, no price patch). Most vendor days before November 2025
  also open at the first trade (D4's `first_open_vs_nse`); that history is not refused. The known coverage gaps left are listed by vendor, span and ISIN
  in `bars5m_gaps.csv` (`app/hat/data/bars5m_gaps.py`), each with the reason it stays short.
- **A 5-minute session is replayed from its open** (orchestrator, 2026-10-30, replacing the
  edges rule). A run replays a session's stored bars (`intraday.on_basis`) when its 09:15 bar is
  there, every bar is on the IST 5-minute grid once, the 09:15 open is within 5% of the daily
  open, no bucket the import refused is missing (`intraday.lacks_refused`: a refused bucket did
  trade) and the session lies in no known coverage gap (`bars5m_gaps`: the Kotak 15:10 sessions
  of 2026-08-03..2026-10-28 the Fyers refetch could not make whole, the refetched sessions whose
  09:15 bar opens off NSE's daily open, and NSE's special sessions of 2024-03-02 and 2024-05-18); other sessions are decided on the daily estimate. A missing bucket, interior or
  trailing, is a window without a trade, as live's bar builder sees it (`data.bars` makes no bar
  for one), so the replay trades nothing there: of the empty buckets the broker export also
  covers, it has a traded bar for 0.4%, all on six dates where the stored vendor folded the
  trades into the next bar. Why: a rule that read the 15:25 bar chose sessions by how they
  ended; before 2026 the sessions it refused were mostly circuit locks (no trade after the lock,
  so no last bar), which a run must see. A known vendor truncation is refused by where it is
  known to be, never by price behaviour. The rule's version is recorded in each run's
  conventions (`replay_rule`, so `config_digest` tells runs before and after it apart).
- **Backtests from 2016-01-01** (owner, 2026-10-25). The NIFTY 500 history starts 2016-01-01:
  the 2011-2015 notices are incomplete (they do not fit the archived lists of 2014 and 2015).
  Until that membership is resolved, a backtest's first session is 2016-01-01 or later; a run
  starting earlier is refused `backtest_starts_before_membership@2016-01-01`. Bars before 2016
  still warm the indicators up. That is all a run reads before its first session: the lookback
  reads bars and features, which rank all ordinary equity, never the index, so membership before
  2016 serves no backtest and the data verify (D3) warns on those sessions, never fails.
- **D2's second source is Fyers' daily history** (owner, 2026-10-30: Yahoo is not reliable; it
  refused this host for hours). The price basis' calendar-year price return is compared with
  Fyers' (split- and bonus-adjusted, no dividends) within 2 points, for 2022 to 2025: Fyers
  serves daily history about five years back. Dividends stay judged against NSE's amounts, and
  a stock Fyers does not list (delisted) is listed, not compared.
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
  book. The data audit warns above 5% unknown member-days, and a run whose per-industry cap is
  below its open positions while any member-day is unknown is refused (a sweep's uncapped
  variants still run). Loaded by
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
  scan output and lives in hat_state, not in the configs. The rows moved once and the
  owner dropped the old tables from hat_live on 2026-10-26, and the drift check no longer allows
  them: a config, backtest or state table in hat_live is schema drift.
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
- **Backtest entries on 5-minute bars, estimates named (S2).** A run decides each session's
  entries, the pre-open playbooks' gap candidates included, by the live trigger on the stored
  `bars_5m` (`entry_bars: bars_5m`, the default; `daily` is a comparison variant a promotion run
  may not use); a session without bars falls back to the daily estimate, and the run records
  only the estimates it took (`ESTIMATE_GAPS`) and its entries per year by basis
  (`entry_bars_by_year`, `entries_5m`/`entries_daily` in `run.json`). Promotion: an estimated
  pre-open trigger (`episodic_pivot_trigger_estimated`) blocks every pre-open playbook and an
  estimated level break (`trigger_is_level_break_only`) every one with `volume_pace`, so their
  evidence run lies inside the 5-minute years; `catalyst_not_evaluated` (no announcements are
  stored for the past; the gap and the early volume stand in) blocks none, so a pre-open entry
  decided on 5-minute bars is evidence. The pre-open estimate (`intraday.EP_ESTIMATE`) is
  calibrated on the 279 neglected gaps of at least 10% from 2022 with bars from 09:15: live fired
  on 24%, entering a median 2.48% over the open and 0.58% over the opening-range high after a low
  4.02% under the open; the rule high >= 1% over the open, close in the upper half of the range
  and day volume at least the `early_volume` multiple fires on 25% and agrees with live on 84%.
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

- **Financial results store (owner 2026-10-28).** NSE's quarterly results from the quarter ending
  2020-03-31, parsed from the XBRL filings only (the pre-XBRL HTML pages are not parsed; XBRL is
  complete from 2020, A0 probe), for the NIFTY 500 point-in-time members (any membership on a day
  from 2020-03-31, the first quarter; a company that changed ISIN is covered once, as its latest
  instrument), on the consolidated basis; standalone only for a quarter a company filed no
  consolidated results for (no subsidiaries), so it still has data (owner scope cut 2026-10-28: the
  EP turnover universe and both bases stay CLI options). One row per filing version, never updated:
  a revision is a new row with the next version superseding the last (`hat_financial_result`,
  created like every table, no migration tool). `public_at` is the results announcement's exchange
  dissemination time (the later of NSE's `an_dt` and `exchdisstime`, owner 2026-10-29: no figure
  is usable before the exchange published it; 'Financial Result Updates', or the board-meeting
  outcome naming the results in the Integrated Filing era; the one naming the filing's quarter end,
  never one naming only another quarter), or the XBRL filing time when that is earlier or no
  announcement matches (an integrated filing: the later of its creation and broadcast times); never the
  listing's broadcast time, a later re-broadcast for many filings before 2022, and never the XBRL
  file name's clock (12-hour, no AM/PM: only its date is read). A quarter NSE does not list, or
  whose XBRL fails, is filled from the secondary source's public company pages (no login,
  robots.txt honoured, one request every 3 s, pages cached), `source=secondary`, used only where no
  NSE row of the same (instrument, period end, basis) is public; its `public_at` is NSE's results
  announcement for the quarter, else the regulatory deadline (45 days after the quarter end, 60 for
  March, at 23:59 IST) with `public_at_estimated`; a quarter that ended before the company listed
  counts from the results that first carry it (the year-on quarter's, never before the first
  results after the listing); a quarter with no announcement by its deadline (a late filer, or a
  company that listed after it) counts from the first results announcement after the deadline,
  whose results carry it as the preceding quarter; a later change to its figures is a new version public from when the
  page was fetched (`observed`). A stored row's figures never change; its `public_at` is
  corrected when a rule fix finds another time (`parse` re-times every row): later, or earlier
  only onto an announcement's dissemination time, so a late time (an XBRL filed up to 7 days
  after the results PDF: the quarter's own results announcement counts, a 'Financial Result
  Updates' before a board outcome; never for a revision, a filing more than a day after another
  of its quarter, as NSE's reg33 listing has no revision flag) is corrected and an early one never
  made; the table's trigger refuses anything else. The secondary source shows only the latest 13
  quarters, so older gaps stay gaps. Growth compares like with like: consolidated when both
  quarters have it, else standalone, never mixed (`data/results/growth.py`); under the default
  consolidated-first scope a quarter's standalone row is read only where that quarter has no
  consolidated row, so a quarter with consolidated results never falls back to standalone.
  Parsing guards (A4 audit, owner 2026-10-28): a quarter context longer than 100 days is read as
  the quarter only when the year-to-date context is twelve months with different figures (a
  mislabelled start; the row carries `quality_flags` `quarter_start_mislabelled`), else the filing is
  excluded `quarter_context_not_a_quarter`; Q4 is derived from the year only when the quarter
  context itself spans the year. A filing whose period ends exactly a year before its listing's is
  excluded `period_year_before_listing`, never stored as a revision. An owners' profit equal (0.1%)
  to total comprehensive income, unequal to the period's profit, with no non-controlling interest,
  is replaced by the period's profit (`owners_profit_tag_swapped`).

- **Fundamentals store (owner 2026-10-28).** The full financial statements are built from the
  results data already downloaded: the NSE results XBRL and the secondary source's company pages,
  read from the cache with no new download. They are stored for later use, not read by any
  strategy. Every numeric fact of each NSE document for its period is kept long format: the
  quarter, the year to date, the half-year balance sheet and the half-year cash flow, with
  segments and other dimensional lines. The main items map to canonical names. Bank, NBFC and
  insurer taxonomies map what has a counterpart and keep the rest raw. Every figure of each page
  (quarters, ten-plus years of profit and loss, balance sheet, cash flow, ratios, shareholding)
  is kept the same way. The scope and basis are the results store's: NIFTY 500 point-in-time
  members, consolidated first. NSE wins wherever both sources give a figure. Page figures are
  restated as of the fetch and are flagged not point-in-time (`point_in_time=false`). Their
  `public_at` is NSE's for the same period, else the regulatory deadline (45 days after a
  quarter, 60 for March or a year, 21 for a shareholding pattern), estimated, and never after
  the fetch. Ratios are derived point in time: a metric as of a moment reads only filings public
  by then, valuations use the last close known then, and share counts are carried across splits
  and bonuses. Each formula is written once (`docs/fundamentals.md`). The tables are projections
  of the cache, replaced on each run, so no version history is kept beyond the cache's.

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
- **A backtest is a portfolio file (owner, 2026-10-28).** `--portfolio FILE`
  (`backtest/portfolio.py`, examples in `docs/backtests/process.md`) names the strategies, their
  weights and playbooks, the universe, window, policy overrides, conventions and the ETF overlay.
  One strategy is the command line's run, hash for hash. Several share one account's cash: one
  ledger and owner, through live's risk check, so the global open-risk cap sums them all
  (`owner_slots`) while the per-bot caps, exposure ladder and regime gate stay each strategy's
  own; each weight is that strategy's share of the account (each within the cluster cap,
  together at most 1), and its playbooks are reported as `<bot>:<playbook>`. A playbook is switched off by keeping
  the others (`playbooks`, `--playbooks`). A portfolio is its own trial family (`S1+S2`), and a
  promotion run is one strategy without an overlay, since evidence is per sleeve.
- **Idle-cash ETF overlay (owner, 2026-10-28; `docs/plans/idle-cash-etf-overlay.md` Phase 1).**
  A portfolio's `overlay` block, off by default, parks the cash the strategies do not use in
  NIFTYBEES, GOLDBEES or LIQUIDBEES under a fixed NIFTY 50 SMA200 band rule (5 confirming
  closes, ±2%, decided on the close, traded at the next open), keeping a 5% cash buffer and
  rebalancing past a 2%-of-equity gap. Entries are sized on total equity, the sleeve included, so
  a run sizes alike with and without it. A sleeve fill never takes the ETF's daily open, a thin
  pre-open auction print (2022-05-11: NIFTYBEES opened 13% below a close it then traded at): the
  open's trades fill at the close of the 09:15 5-minute ETF bar, and an entry the cash cannot
  pay sells the sleeve as it fills, at the close of the 5-minute bar that just ended (its trigger
  bar's), never a later price. Without the bar
  (5-minute bars start 2021-11) the price is the previous close, NIFTYBEES' scaled by NIFTY 50's
  open over its previous close, each such fill counted `sleeve_price_estimated`; the ETF store's
  `open_gaps` lists the opens it avoids. A funding sale credits 80% of
  the proceeds the same day before 2024-10-07 and 100% from then; an entry still short
  is sized down or refused `insufficient_same_day_credit`. ETF fills pay the stock cost model at
  their own slippage (5 bps NIFTYBEES and GOLDBEES, 1 bp LIQUIDBEES). Tax uses today's rules in
  every year (`backtest/tax.py`): FIFO lots of every fill, strategy and sleeve; set-off and
  8-year carry-forward; a slab rate of 30% by default plus 4% cess; paid on 31 March. Rules and
  defaults: `backtest/overlay.py`. The overlay reads `HAT_DATA_ROOT/etf/`, written by
  `scripts/data/overlay_etf.py store`. Simplifications, besides the known gaps a run reports:
  stock cash dividends are slab income; ETFs trade on the split-adjusted series; the first
  sleeve buy is at the first session's open.

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

Regime v2 (HMM/GMM), fundamentals beyond quarterly revenue, profit and EPS, screener universes, US markets, a Kotak
adapter, Kite market data, multi-user; PDF summaries on cards and a chat
MCP server (advisory only); a CLI to re-run one job for a past trade date; a weekly timeframe;
live trading of `entry_timing: daily_close`. Results data: ingest NSE's Annual and Others results
listings (a few year-end quarters are filed only there, excepted in D5 meanwhile); one row per
NSE sha in `results_events` (filings stored under an older ISIN of the same company before the
chain rule); no secondary rows after a company's last traded day. ETF data verification: D2 and
D6 judge stocks only, so ETF unit splits and consolidations, fund listing status and the funds'
adjusted series are stored but unverified; an ETF overlay needs them verified first. D2/D6
follow-up: read a stock's series as of each day for `series_in`, not today's. S1 setups Phase 2:
the R1-R5 runs, the top-signal exit (F) and tennis-ball stop (G) variants, the setup and tag
columns in the run's result files, the daily stage of open positions, and the shadow-day replay
of a playbook with a validity window (it scans without the stored windows).

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
- Data: volume adjusts by the cumulative factor; the daily-bar estimate of a trigger (and the
  pre-open calibration) where no 5-minute bars are stored.
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
