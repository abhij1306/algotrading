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

### A4. Validation report. STOP after this.
- Coverage by year and by format: filings found, parsed, excluded (with reason counts).
- Spot check: 20 random company-quarters; compare parsed revenue, net profit and EPS with the PDF results. Report every mismatch.
- Count of q4_derived, unit_suspect, period mismatches, revisions.

## Part B: EP playbook refinement

Current EP config (for reference): gap_up ≥ 10%; early_volume ≥ 0.5 × ADV20 at the trigger; neglect = |63-session return| ≤ 30%; catalyst_attached = any announcement within 24h (live only, not evaluated in backtests); trigger = 5-minute opening-range high; adtr_filter ≤ 1.5× ADTR20; max_chase 2%; stop = low of day. Exits are shared with the breakout playbook: partial 33% after 3 days if in profit, breakeven after partial, close below SMA10.

### B1. New criterion: `earnings_catalyst` (setup or trigger stage, mode hard, evaluable in backtests from A3 data)
Passes when BOTH:
1. Timing: a results filing for the instrument has public_at in the window (previous session's 09:15 IST, trigger time].
2. Growth (on that filing, per A3):
   sales_yoy ≥ 25% AND (eps_yoy ≥ 25% OR turnaround OR sales_yoy ≥ 50%).
   - Provenance: Stockbee calls 25%+ the safer cutoff; Qullamaggie wants high double- or triple-digit EPS and sales growth but accepts high sales growth without earnings. The combination above is our construction; log both growth values on every evaluation.
Not modelled: analyst-estimate beats (no data). Add `analyst_beat_not_evaluated` to KNOWN_GAPS.
When this criterion is in use, `catalyst_not_evaluated` no longer applies to the run; update spec.py accordingly.

### B2. Catalyst tagging
On every EP signal, record catalyst_type: earnings (B1 timing matched a results filing) | other_announcement (catalyst_attached matched only non-results items) | none. Report results split by type. Do not gate on type except in the variants listed in Phase 4.

### B3. Time-anchored early volume (new mode on `early_volume`)
New param `mode: time_anchored` (keep the existing behaviour as `mode: at_trigger`, the default, so stored configs don't change):
- minutes = minutes from 09:15 IST to the close of the trigger bar
- required = ADV20 × min(1, minutes / 30)
- pass if cumulative day volume at the trigger ≥ required
- Provenance: Qullamaggie says the best EPs trade their average daily volume within the first 15–30 minutes.

### B4. Neglect fix
1. New param `direction: up_only | both` (default `both`, today's behaviour). `up_only` fails only when the 63-session return is > +max_return_63d_pct; stocks that fell are allowed.
2. Leave `max_range_126d_pct` off as a gate. Instead log the 126-session range (max high / min low − 1) on every EP signal and report results by bucket: < 30%, 30–60%, > 60%.

### B5. ADR universe exemption
`adr_min` (4%) is currently bot-level and applies to the EP playbook. Add support for a per-playbook universe override and exempt `episodic_pivot` from `adr_min` in the Phase 4 variant that tests it. Log ADR20 (pre-gap, per the repo's FAQ-matching formula: 100 × (mean of High/Low over 20 sessions − 1)) on every EP signal and report by bucket: < 3%, 3–4%, 4–6%, > 6%.

### B6. Per-playbook exits
Add support for an `exits` block per playbook, falling back to the bot-level block when absent. The breakout playbook keeps today's exits unchanged.
EP exits:
- hard stop: low of day (unchanged)
- partial: sell 33% after 3 sessions, only if in profit (unchanged; QM blog says 1/3–1/2 after 3–5 days, interview notes ~20–25%)
- move stop to breakeven after the partial (unchanged)
- trail: close below SMA(N), with N per variant (10 or 20). The SMA exit becomes active only once SMA(N) is above the current stop (QM: trail "once they surpass your initial stop"); until then only the stop applies.

### B7. Intraday-results EPs (research only, no trigger yet)
Many NSE companies announce results during market hours, so a real earnings EP can be an intraday surge with no opening gap. Do NOT build a trigger yet. Using A1 data and 5-minute bars from 2022:
- Count results filings by public_at bucket: before 09:15, 09:15–15:30, after 15:30.
- For filings during market hours, report the distribution of the move from the price at public_at to the session close and to the close 5 and 20 sessions later, split by B1 growth pass/fail.
- Report only. A trigger definition comes later, based on this.

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

## Docs to update when implemented
- `docs/decisions.md`: entries for the results store, earnings_catalyst, time-anchored volume, neglect direction, per-playbook universe and exits.
- `backtest/spec.py`: KNOWN_GAPS (`analyst_beat_not_evaluated`; `catalyst_not_evaluated` only for runs without results data).
- The seed YAML: only after the decision rule is applied and I approve.
