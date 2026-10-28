# Plan: NSE results data + S2 Episodic Pivot (EP) refinement

Repo: github.com/abhij1306/algotrading. Market: NSE.
Scope: the `episodic_pivot` playbook in `backend/app/hat/engine/strategy/seeds/momentum-breakout-ep.yaml` (S2), plus a new historical financial-results data store it depends on. The S2 `breakout` playbook is out of scope except where noted (it must keep its current exits).

Every rule below is fully specified here. Provenance notes ("QM blog", "Stockbee") are for reference only; do not infer rules beyond what is written. If something is ambiguous, stop and ask.

Sources the rules come from (reference only):
- Qullamaggie blog: qullamaggie.com/my-3-timeless-setups-that-have-made-me-tens-of-millions/ and /how-to-master-a-setup-episodic-pivots/
- Qullamaggie stream notes: tradingresourcehub.substack.com (e.g. 18 May 2023 notes)
- Stockbee (creator of the EP setup): qullamaggie.net/stockbee-on-trading-episodic-pivots/

## Ground rules
- Follow the repo's conventions: config-driven criteria with HAT-CRIT ids, unit tests for every new or changed criterion, decisions recorded in `docs/decisions.md`, and `KNOWN_GAPS` / `ESTIMATE_GAPS` in `backtest/spec.py` kept accurate.
- No look-ahead. Every value used at a trigger time must be computable from data public at that moment.
- No parameter sweeps. Variants in Phase 4 are fixed and listed; run exactly those.
- Verify, don't assert. Every claim in a report needs a file path, line number, test name, run ID or row count.
- Stop and wait for me at each checkpoint marked STOP.

## Part A: NSE historical financial-results data

### A0-A3. Done
The probe (A0), the store (A1), the parser rules (A2) and point-in-time growth (A3) are built:
`backend/app/hat/data/results/`, `docs/decisions.md` (Financial results store), `docs/runbook.md`
(Financial results). Every rule's test is in `backend/tests/hat/properties/test_results.py`.
Scope (owner 2026-10-28), for A1 and A3: NIFTY 500 point-in-time members only, consolidated basis
(standalone only for a quarter with no consolidated filing); A3 growth therefore compares
consolidated quarters, falling back to standalone only where both quarters are standalone.

### A4. Validation report. STOP after this.
- Coverage by year and by format: filings found, parsed, excluded (with reason counts).
- Spot check: 20 random company-quarters; compare parsed revenue, net profit and EPS with the PDF results. Report every mismatch.
- Count of q4_derived, unit_suspect, period mismatches, revisions.

## Part B: EP playbook refinement

### B1-B7. Done
`earnings_catalyst` (HAT-CRIT-127), the catalyst tagging, `early_volume` `mode: time_anchored`,
`neglect` `direction`, the per-playbook universe and exits and the SMA trail's `above_stop` are
built (`docs/decisions.md`, Strategy and risk); the EP log, `ep_candidates.csv` and the C3
breakdowns are in every run with a pre-open playbook (`docs/backtests/result-standard.md`). B7 is
`scripts/research/s2_ep_intraday_results.py`; C4 is `scripts/research/s2_ep_audit.py`. The C1/C2
sweep is `/data/workspace/backtests/sweeps/s2-ep-01.yaml` (V7 added after C3); the stored
5-minute bars start 2021-10-26.

## Part C: Backtest and evaluation

Window: only years with 5-minute bars count as EP evidence (2022 onward; confirm the exact start from stored data).
- In-sample: 2022-01-01 to 2024-12-31
- Holdout: 2025-01-01 to latest. Do not run or read the holdout until I approve.

### C1. Baseline
Re-run the current S2 EP config, unchanged, on the in-sample window. Record its run ID.

### C2. Fixed variants (one change each vs baseline, then one combined)
- V1: + earnings_catalyst (B1) as hard
- V2: early_volume mode time_anchored (B3)
- V3: neglect direction up_only (B4)
- V4: EP exempt from adr_min (B5)
- V5: EP exits with SMA10 trail and the "SMA above stop" activation (B6)
- V6: EP exits with SMA20 trail and the same activation (B6)
- V7: V1 + V2 + V3 + V4 + the better of V5/V6 by expectancy. State which you picked and the numbers.
No other variants.

### C3. Report (per run)
- trades, signals, refusal counts by reason, win %, avg win R, avg loss R, expectancy R, CAGR, NIFTY 500 TRI CAGR for the same window, max DD, MAR, Sharpe, DSR, CAGR ex top-5, top-5 P&L share
- by year
- by catalyst_type (B2), by sales_yoy bucket (< 25%, 25–50%, 50–100%, > 100%), by gap size bucket (10–15%, 15–25%, > 25%), by trigger time bucket (09:20–09:45, 09:45–11:00, after 11:00), by ADR bucket, by 126-session range bucket
- by exit reason: count, win %, expectancy, avg hold
- funnel: gap scan → gap_up → neglect → earnings_catalyst → early_volume → ORH trigger → adtr_filter → max_chase → filled

STOP after C3.

### C4. Audit
Sample 20 V7 entries and 10 near-miss rejections (fixed seed; print it). For each: symbol, date, public_at of the results, sales_yoy, eps_yoy, gap %, cumulative volume vs ADV20 at trigger, trigger time, entry, stop, exit, R. Save a 5-minute chart of the entry day (with the opening range, entry and stop marked) and a daily chart (SMA10/20/50) to `reports/s2-ep/audit/`.

### Decision rule (state which applies, with numbers)
- Fewer than 40 trades in-sample for a variant: report it as too few to judge. Do not loosen gates to raise the count.
- V1 expectancy clearly above baseline, and earnings-type trades beat other-type trades: keep earnings_catalyst as hard in live.
- V1 no better than baseline: report it; the earnings gate isn't earning its keep on NSE, and we decide manually.
- Only after I approve: run the chosen config, unchanged, on the holdout and report C3 tables.

## Docs to update when Part C is decided
- The seed YAML: only after the decision rule is applied and I approve.
