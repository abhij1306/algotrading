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

## Built
Phase 0 (the data, `HAT_DATA_ROOT/etf/`) and Phase 1 (the overlay engine and its tax, with their
unit tests) are built: `docs/decisions.md` ("Idle-cash ETF overlay"), `backend/app/hat/backtest/overlay.py`
and `tax.py`. Each variant below is a portfolio file's `overlay` block (`docs/backtests/process.md`,
How to define a backtest).

## Phase 2: Fixed variants (run for S1, S2 and S1+S2 combined)
- V0: strategy alone, idle cash earns nothing (current behaviour; `overlay: {sleeve: none}` taxes it alike).
- V1: strategy + LIQUIDBEES sleeve always (no regime; `sleeve: liquid`). This is the "cash earning interest" baseline.
- V2: strategy + NIFTYBEES sleeve always (no regime; `sleeve: nifty`).
- V3: strategy + regime sleeve: RISK-ON NIFTYBEES, RISK-OFF LIQUIDBEES (`sleeve: regime`).
- V4: strategy + regime sleeve with gold: RISK-ON NIFTYBEES, RISK-OFF GOLDBEES while its own band rule is on, else LIQUIDBEES (`sleeve: regime_gold`).
- B1: buy-and-hold NIFTYBEES (every overlay run's `overlay.benchmarks.buy_hold_niftybees`).
- B2: regime sleeve alone (V3's switching applied to 100% of capital, no strategy; `overlay.benchmarks.sleeve_alone`).
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
