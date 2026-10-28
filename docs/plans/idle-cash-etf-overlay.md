# Plan: Idle-cash ETF overlay (portfolio level, long-only)

Repo: github.com/abhij1306/algotrading. Market: NSE.
Goal: test whether parking idle cash in ETFs, switched by a fixed market-regime rule, improves the after-tax, risk-adjusted result of the strategy bots, without hiding a weak strategy.
Applies to: S1 (book-strict config chosen in the S1 plan), S2 (EP config chosen in the S2 plan), and S1+S2 combined, each tested separately.

Every rule below is fully specified. If something is ambiguous, stop and ask.

## Ground rules
- The regime rule and every parameter below are FIXED before any result is seen. No tuning, no sweeps, no extra variants.
- In-sample: 2016-01-01 to 2021-12-31 (S1 and combined). S2 in-sample follows the S2 plan's window (5-minute bars from 2022). Holdout (2022 onward for S1/combined; 2025 onward for S2) only after I approve.
- No look-ahead: regime is evaluated on the close and acted on at the next session's open.
- No leverage: cash, sleeve and positions always sum to equity; no negative cash.
- Follow repo conventions: config-driven, unit tests for every rule, `docs/decisions.md` entries, `KNOWN_GAPS` kept accurate.
- Verify, don't assert. Every claim needs a file path, line number, test name or run ID.
- STOP at each checkpoint marked STOP.

## Phase 0: Data (report only). STOP after this.
1. Daily OHLCV for NIFTYBEES, GOLDBEES and LIQUIDBEES from 2015-01-01 to latest, adjusted for any splits or consolidations (check NSE corporate actions; list every adjustment applied).
2. LIQUIDBEES total return: its price stays near ₹1,000 and the return comes from daily dividends paid as extra units. Build a total-return series from its dividend history (NSE corporate actions or the AMC's records). If that history isn't available for the full window, use the NIFTY 1D Rate Index minus LIQUIDBEES' expense ratio as a proxy, and flag every date range where the proxy is used.
3. NIFTY 50 daily close (for the regime rule) and NIFTY 50 TRI + NIFTY 500 TRI (benchmarks).
4. 5-minute bars for NIFTYBEES and LIQUIDBEES from 2022, if available (needed to price intraday sleeve sales that fund S2 entries).
5. Report: coverage per series, gaps, adjustments, and where proxies are used.

## Phase 1: Overlay engine

### 1.1 Regime rule (fixed)
- Signal: NIFTY 50 close vs its 200-day SMA.
- Switch to RISK-ON when the close is above SMA200 × 1.02 for 5 consecutive sessions.
- Switch to RISK-OFF when the close is below SMA200 × 0.98 for 5 consecutive sessions.
- Otherwise keep the previous state. Initial state on the first date: RISK-OFF until a switch condition is met.
- Evaluated after the close; any switch executes at the next session's open.
- This is deliberately separate from (and slower than) the strategies' own regime filters. Do not change either.

### 1.2 Sleeve assets per variant (see Phase 2 for which variant uses what)
- RISK-ON asset: NIFTYBEES.
- RISK-OFF asset: LIQUIDBEES, or, in the gold variant only: GOLDBEES when GOLDBEES' close is above its 200-day SMA, otherwise LIQUIDBEES (checked daily with the same 1.02/0.98 band and 5-session confirmation, applied to GOLDBEES vs its own SMA200).

### 1.3 Sizing and cash
- Equity = cash + sleeve value + open positions at mark.
- Strategy risk per trade is computed on total equity (including the sleeve), exactly as the strategy config already specifies.
- Cash buffer: keep 5% of equity in cash at all times (not in the sleeve).
- Target sleeve value = equity − open position value − cash buffer, floored at 0.
- Equity exposure cap: open strategy positions + NIFTYBEES sleeve ≤ 100% of equity. (Automatically true under the target above; add an assertion.)
- Rebalance: at each session's close, compare actual sleeve value with target. Trade at the next open only if the gap exceeds 2% of equity. Log every rebalance.

### 1.4 Funding a new strategy entry
- If cash (excluding nothing; the buffer may be used) covers the entry, use cash.
- Otherwise sell enough sleeve to cover the shortfall, at the same time as the entry:
  - with 5-minute ETF bars: the sleeve sells at the bar in which the entry fills
  - without them: at the day's open, and flag the trade as `sleeve_price_estimated`
- Same-day credit from the sale: 80% of proceeds before 2024-10-07, 100% from 2024-10-07 onward (Zerodha's policy; configurable per broker as `same_day_sale_credit_pct` with an effective date). The uncredited part becomes available the next session.
- If the entry still can't be fully funded, size it down to what's available; if that's below the strategy's minimum position size, refuse it with reason `insufficient_same_day_credit`. Count these refusals.

### 1.5 Costs
- Use the repo's existing cost model (brokerage, STT, exchange, SEBI, GST, stamp) for all ETF trades.
- ETF slippage: 0.05% per side for NIFTYBEES and GOLDBEES, 0.01% per side for LIQUIDBEES. Configurable; report them in every run.

### 1.6 Tax (after-tax results are the primary comparison)
Apply current rules uniformly across the whole backtest (do not model historical rate changes; note this in KNOWN_GAPS):
- Lots tracked FIFO per instrument.
- Strategy stock trades and NIFTYBEES: gains on holdings ≤ 12 months taxed at 20%; > 12 months at 12.5% on gains above ₹1.25 lakh per financial year (the exemption is shared across all equity long-term gains in that year).
- GOLDBEES: ≤ 12 months at slab rate; > 12 months at 12.5%, no exemption.
- LIQUIDBEES: dividend units taxed at slab rate in the year received (value of units at receipt becomes their cost); gains on sale at slab rate regardless of holding period.
- Slab rate: config parameter `slab_rate`, default 30% (plus 4% cess on all tax).
- Losses: short-term losses offset short- or long-term gains in the same financial year; long-term losses offset only long-term gains. Carry forward unused losses for up to 8 years.
- Tax is paid out of cash on 31 March of each financial year (simplification; note it). If cash is short, sell sleeve to pay it.
- Buy-and-hold benchmarks: report pre-tax, and after-tax assuming full liquidation on the final date.

### 1.7 Unit tests (required)
Regime switching with band and confirmation (including no-switch inside the band), initial state, rebalance threshold, exposure cap, funding from the sleeve with 80%/100% same-day credit around 2024-10-07, refusal when underfunded, FIFO lots, each tax rule, loss set-off and carry-forward, LIQUIDBEES dividend units.

## Phase 2: Fixed variants (run for S1, S2 and S1+S2 combined)
- V0: strategy alone, idle cash earns nothing (current behaviour).
- V1: strategy + LIQUIDBEES sleeve always (no regime). This is the "cash earning interest" baseline.
- V2: strategy + NIFTYBEES sleeve always (no regime).
- V3: strategy + regime sleeve: RISK-ON NIFTYBEES, RISK-OFF LIQUIDBEES.
- V4: strategy + regime sleeve with gold: RISK-ON NIFTYBEES, RISK-OFF GOLDBEES-or-LIQUIDBEES per 1.2.
- B1: buy-and-hold NIFTYBEES.
- B2: regime sleeve alone (V3's switching applied to 100% of capital, no strategy).
- Reference only: NIFTY 50 TRI and NIFTY 500 TRI.
No other variants.

## Phase 3: Report (in-sample). STOP after this.
For every run, pre-tax and after-tax:
- CAGR, max drawdown, longest time to recover from drawdown (sessions), Sharpe, MAR
- by year: return vs NIFTY 50 TRI
- regime switches per year; sleeve turnover per year; tax paid per year; costs per year
- average and max total equity exposure; days at the exposure cap
- `insufficient_same_day_credit` refusals and `sleeve_price_estimated` trade counts
- drawdown overlap: for the 5 largest drawdowns of the blended portfolio, the share of the loss coming from strategy positions vs the sleeve
For the strategy itself (from V0 and V1):
- return on deployed capital = total strategy P&L ÷ time-weighted average capital in strategy positions, annualised
- correlation of the strategy's daily returns with NIFTY 50
- max drawdown of strategy P&L alone

## Decision rule (state which applies, with numbers)
1. Judge the strategy first, using V1 (strategy + liquid sleeve) vs B1, after tax:
   - If V1 has both lower CAGR and a worse MAR than B1, report it plainly: the strategy isn't adding value, and an overlay would only hide that.
2. Judge the overlay only if step 1 passes:
   - Adopt V2, V3 or V4 only if its after-tax MAR beats V1's AND its max drawdown is no worse than B1's.
   - Among those passing, prefer the one with fewer switches per year if MARs are within 0.05 of each other.
   - If none pass, keep V1 (liquid sleeve only).
3. Holdout: after I approve, run the chosen variant and B1, unchanged, on the holdout window, and report the Phase 3 tables.

## Docs to update when implemented
- `docs/decisions.md`: overlay design, regime rule, funding rule, tax simplifications.
- `backtest/spec.py`: KNOWN_GAPS entries for uniform current-tax-rules, year-end tax payment, sleeve open-price estimates, and any LIQUIDBEES total-return proxy ranges.
