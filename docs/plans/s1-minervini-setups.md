# Plan: Minervini setups as scannable rules (S1 bot)

Repo: github.com/abhij1306/algotrading. Market: NSE, long-only.
Goal: every setup is one detector function that runs identically in (a) the daily end-of-day scan that builds tomorrow's watchlist, and (b) the backtest, using only data available on that date.

All rules are fully specified here. Values marked [mine] are not from the source and must be logged per signal, not tuned. If anything is ambiguous, stop and ask.

## Ground rules
- One detector per setup, added as a playbook in the S1 bot alongside the existing VCP playbook from s1-book-01. Reuse its criteria, trigger, exits and sizing unless stated.
- Same code path for live scan and backtest. No look-ahead: swings must be confirmed by bars up to the evaluation date.
- No tuning or sweeps. Unit tests: one passing and one failing synthetic series per rule.
- Verify, don't assert: every report claim cites a file:line, test, or run ID.
- STOP at each checkpoint marked STOP.

## Shared building blocks (one implementation each, already partly in s1-book-01)
- SMA(n), ATR14, ADV50 = 50-day average volume (as of the previous session), vr = volume ÷ ADV50.
- RS rank: score = 0.4×ret(63) + 0.2×ret(126) + 0.2×ret(189) + 0.2×ret(252), percentile 1–99 across the eligible universe, point-in-time.
- Trend template (TT), all 8: close > SMA150 and SMA200; SMA150 > SMA200; SMA200 > SMA200 21 sessions ago; SMA50 > SMA150 and SMA200; close > SMA50; close ≥ 1.30 × 52-week low; close ≥ 0.75 × 52-week high; RS rank ≥ 70.
- Swings: zigzag on high/low, reversal threshold = max(3%, 1.5 × ATR14 ÷ close). Confirmed swings only.
- Contraction: from swing high Hi to next swing low Li; depth d = (Hi − Li) ÷ Hi.
- Tight area: the final contraction (or pause) of a setup. Pivot = its highest high. Stop = its lowest low − 0.5%.
- Dry-up test on a tight area: average vr < 0.7 AND at least one session with vr < 0.5.
- Stop cap: skip any setup whose stop is more than 10% below the pivot.

## Setup 1: VCP
Exactly as s1-book-01 Phase 2 sections A–G (2–6 contractions; each ≤ 0.75 × prior; first 10–35%; final ≤ 10% and ≥ 3 sessions; base 15–325 sessions; dry-up; time-compression, relative-correction and base-count rejections). No changes.

## Setup 2: Cup completion cheat (3C) and low cheat
Cup:
- H0 = highest high in the last 325 sessions; C = lowest low after H0.
- Cup depth (H0 − C) ÷ H0 between 15% and 50%.
- Cup length from H0 to evaluation date: 35–325 sessions [mine: 7–65 weeks].
- C must be at least 10 sessions before the pause starts [mine].
Pause (the cheat):
- The most recent 5–15 sessions [mine] whose range (max high − min low) ÷ max high ≤ 6% [mine].
- Position: let pos = (pause high − C) ÷ (H0 − C).
  - pos 0.33–0.67 → `cheat_3c`
  - pos < 0.33 → `low_cheat`
  - pos > 0.67 → not a cheat; evaluate as Setup 3 instead.
- Dry-up test on the pause.
Stage gate (relaxed, because a cheat forms before the stock is back at highs) [mine]:
- TT passed on H0's date (the stock was in an uptrend when the cup began).
- At evaluation: close > SMA50, SMA50 rising (SMA50 > SMA50 5 sessions ago), RS rank ≥ 70.
Pivot, stop, cap: shared definitions on the pause.

## Setup 3: Cup with handle
- Cup as in Setup 2.
- Handle = the most recent tight area with:
  - handle low ≥ C + 0.67 × (H0 − C) (upper third of the cup)
  - handle high ≤ H0
  - handle depth ≤ 12% [mine]
  - length 5–25 sessions [mine: 1–5 weeks]
  - dry-up test passes
- Full TT at evaluation.
- Pivot = handle high; stop = handle low − 0.5%; stop cap applies.

## Setup 4: Power play (high tight flag)
- Thrust: within the last 60 sessions, some high T is ≥ 2.0 × the lowest low in the 40 sessions before T (≥ 100% in under 8 weeks).
- Flag: from T to evaluation is 15–30 sessions (3–6 weeks); flag depth (T − lowest low since T) ÷ T ≤ 25%.
- Inside the flag: at least 2 contractions, each ≤ 0.75 × the prior; final contraction ≤ 8% [mine]; dry-up test on the final contraction.
- Gates: close > SMA50; RS rank ≥ 80 [mine]. Full TT not required (the thrust can be too recent for SMA150/200 to line up). No earnings requirement.
- Pivot = high of the final contraction; stop as shared; stop cap applies.

## Setup 5: Primary base (recent listings)
- Applies only when the stock has 40–500 sessions of trading history [mine: too short for a full TT].
- Base starts at the highest high since listing (H0); base depth ≤ 35%; base length ≥ 15 sessions.
- VCP contraction rules inside the base: 2–6 contractions, each ≤ 0.75 × prior, final ≤ 10%, dry-up test.
- Gates: close > SMA50 if ≥ 50 sessions of history; RS rank ≥ 70 if ≥ 252 sessions of history, otherwise skip RS.
- Pivot, stop, cap: shared definitions on the final contraction.

## Trigger (all setups)
- A setup found at end of day D is valid for entry on D+1 to D+5, and is cancelled early if price closes below its stop or a new swing high above the pivot forms without a breakout.
- Entry when price trades above the pivot, fill no more than 2% above it (skip otherwise, including gap-ups).
- Breakout volume: vr ≥ 1.5 for the day. Live: use volume pace (cumulative volume ÷ fraction of the session elapsed) ≥ 1.5 × ADV50. Backtest without intraday bars: full-day volume, flagged as look-ahead in KNOWN_GAPS.
- If several setups fire for one stock, take one entry and record all setup tags.

## Exits, sizing, portfolio
Identical to s1-book-01 for every setup: stop as defined; breakeven at +2.5R; sell 50% at +3R; remainder exits on close below SMA20; first 10 sessions ignore the SMA20 exit (squat grace). Risk 1% per trade, max position 25%, max 6 positions, rank competing signals by RS rank, re-entry allowed. Market regime filter as in s1-book-01.

## Daily scan (live)
- After market close, run all five detectors on the full eligible NSE universe.
- Write a watchlist row per hit: symbol, setup type, pivot, stop, stop %, footprint (weeks, deepest %/tightest %, contraction count), RS rank, dry-up ratios, base number, valid-until date.
- Next session: monitor pivots intraday; when trigger conditions are met, raise the signal for confirmation (no automatic orders).
- Unit test: the scan on date D and the backtest's setup list on date D must be identical for the same data.

## Phase 1: Implement and test detectors. STOP after this.
Implement Setups 2–5, the trigger validity window, the daily scan and its watchlist table. Report test coverage per rule.

## Phase 2: Backtest (in-sample 2016-01-01 to 2021-12-31; holdout only after I approve)
Runs:
- R1: VCP only (equals s1-book-01; sanity check)
- R2: each of Setups 2, 3, 4, 5 alone
- R3: all five together
Report per run and per setup tag: signals, trades, win %, avg win R, avg loss R, expectancy R, CAGR vs NIFTY 500 TRI, max DD, MAR, CAGR ex top-5, by year, by exit reason, and the funnel (stock-days passing each rule in order). STOP after this.

## Phase 3: Chart audit
For each setup, sample 15 signals and 5 near-misses (fixed seed; print it). Save a daily chart per case to `reports/s1-setups/audit/` with H0, C, contractions, tight area, pivot and stop marked, plus volume and ADV50. List every case where the marked structure doesn't match what's visible on the chart. Do not fix detectors yet.

## Decision rule (state which applies, with numbers)
- A setup with fewer than 30 trades in-sample: report as too few to judge; keep it scan-only (watchlist, no trades).
- A setup with win % ≥ 35% and expectancy > 0.3R: keep it in the combined bot.
- Otherwise: treat it as a detection problem first; the Phase 3 audit decides the fix.

## Addendum: rules from the remaining book patterns

Implement with the setups plan. Every rule here is either a tag (logged and reported, no effect on trades) or a fixed variant in Phase 2. Values marked [mine] are not from the source. No tuning.

### A. Shakeout (tag)
Inside any base (Setups 1–5): a swing low that undercuts an earlier swing low in the same base by 0–5% [mine], followed by a close back above that earlier low within 3 sessions, with vr ≥ 1.2 on the recovery day [mine].
Tag `shakeout=true` and record the undercut %. Report win % and expectancy with vs without a shakeout.

### B. Overhead supply (rejection, all setups)
Reject if any high from 326 to 750 sessions ago lies between 0% and 10% above the pivot [mine], AND price fell at least 30% from that old high afterwards [mine].
Log the old high's date and distance. Exception: none.

### C. All-time-high pivot (tag)
Tag `ath_pivot=true` when the pivot is at or above the highest high in all available history before the base.
Report results with vs without.

### D. Leader at market low (tag, and tie-break)
RS line = close ÷ NIFTY 500 close.
Tag `early_leader=true` when the RS line makes a 252-session high while NIFTY 500 is at least 10% below its own 252-session high [mine].
When signals compete for cash, rank by early_leader first, then RS rank.

### E. Stage classifier (tag on every signal and every open position, daily)
- Stage 2: TT passes.
- Stage 4: close < SMA200 and SMA200 < SMA200 21 sessions ago.
- Stage 1: SMA200 within ±1% of its value 21 sessions ago, and close within ±15% of SMA200 [mine].
- Stage 3: otherwise, if the stock was in stage 2 within the last 60 sessions [mine].
- Otherwise: unclassified.
Report signal counts and outcomes by stage at entry.

### F. Top-signal exit (Phase 2 variant)
Exit the full remaining position at the next open if either:
- the session's decline is the largest daily % decline in the last 252 sessions AND its volume is the largest in the last 252 sessions, or
- the stock moves to stage 4 (as defined in E).
Run as variant R4 = R3 + this exit. Compare with R3.

### G. Tennis ball check (Phase 2 variant)
For an open position that has reached +1R: if a pullback then goes 10 sessions [mine] without a new closing high, move the stop up to breakeven (or keep it if already higher).
Run as variant R5 = R3 + this rule. Compare with R3.

### Reporting
Add to Phase 2 reports: results split by shakeout, ath_pivot, early_leader, and stage at entry; overhead-supply rejection counts; R4 and R5 vs R3.
