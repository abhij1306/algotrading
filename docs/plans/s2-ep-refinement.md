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

### A0. Feasibility probe (report only). STOP after this.
1. Find how the repo currently fetches NSE data (session handling, headers, rate limits, `robots.txt` compliance) and reuse it.
2. Probe NSE's financial-results endpoint (the source behind nseindia.com/companies-listing/corporate-filings-financial-results) and the Integrated Filing – Financials listing (used for results from the March 2025 quarter onward).
3. For 20 symbols (mix of large/mid/small caps, at least 2 banks, 2 NBFCs), for each year 2016–2026, record:
   - whether filing metadata exists, with its filing timestamp field
   - whether the XBRL document downloads (HTTP status)
   - XBRL format/taxonomy (Reg 33 Ind AS, Reg 33 banking, NBFC, insurance, Integrated Filing variants)
   - whether standalone, consolidated, or both exist
4. For 20 results filings, compare the results endpoint's filing timestamp with the corporate-announcement broadcast time of the same results (the PDF announcement). Report the typical difference. Background: companies must file the results PDF within 30 minutes of the board meeting ending, but the XBRL within 24 hours, so the two timestamps can differ.
5. Report: coverage table by year, the earliest year with complete XBRL coverage, the timestamp comparison, and any blocking issues.
   Expected (from third-party reports, to be confirmed): metadata back to ~2007; XBRL documents fully available from 2019, partial 2018, missing 2017 and earlier.

### A1. Storage (append-only)
Table `hat_financial_result` (new Alembic migration), one row per filing version:
- instrument_id, symbol, isin
- period_start, period_end (from the XBRL document, NOT the filing metadata; if they disagree, the document wins and the mismatch is logged)
- basis: standalone | consolidated
- format: reg33_indas | reg33_banking | reg33_nbfc | integrated_indas | integrated_banking | integrated_nbfc | other
- revenue (operations), total_income, net_profit (attributable to owners where tagged), eps_basic, eps_diluted, all for the QUARTER
- ytd_revenue, ytd_net_profit, ytd_eps (if present)
- public_at: the earliest of (results-announcement broadcast time, results filing timestamp), timezone-aware IST
- xbrl_at: the XBRL filing timestamp
- version (1 = original; revisions get 2, 3, …), supersedes_id
- source_url, sha256 of the raw file, raw file path
Raw XBRL files are cached on disk by sha256 and never overwritten. Revisions are new rows, never updates.
Insurance companies: store the metadata row, skip parsing, flag `format=insurance_skipped`.

### A2. Parser rules (each needs a failing-then-passing unit test with a recorded fixture)
1. Quarter vs year-to-date: tags/contexts for the current quarter ("One…" contexts) are the quarter; "Four…" contexts are year-to-date. Never read YTD as the quarter. Test: Q3 fixture where YTD ≈ 3× quarter.
2. Banks: no revenue-from-operations tag. Map interest earned → revenue, profit/loss for the period → net_profit. Same for NBFCs if their template differs. Test with one bank fixture and one NBFC fixture.
3. Period: always from the document; log mismatches with metadata.
4. Q4: if a Q4 filing carries only annual and nine-month figures, derive Q4 = annual − nine-month YTD (same basis), and flag `q4_derived=true`.
5. Units: parse the declared unit/decimals. Sanity check: if quarterly revenue differs from the same company's median of the previous 4 quarters by more than 20× in either direction, flag `unit_suspect=true` and exclude the row from growth calculations.
6. Both old Reg 33 and Integrated Filing formats map into the same schema.

### A3. Point-in-time growth (derived, recomputed from A1)
For each (instrument, period_end, basis), using the latest version public at the evaluation time:
- sales_yoy = revenue / revenue(same quarter one year earlier, same basis) − 1
- eps_yoy = eps_basic / eps_basic(same quarter one year earlier, same basis) − 1, defined only when both are > 0
- turnaround = prior-year EPS ≤ 0 and current EPS > 0
- Basis choice: use consolidated when both the current and prior-year quarter have it; otherwise standalone when both have it; otherwise growth is undefined. Never mix bases in one comparison.
- API: `results_as_of(instrument_id, ts)` returns only rows whose public_at ≤ ts.

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
