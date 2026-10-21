# HAT build plan

The roadmap the orchestrator works through: 17 PRs, one builder each. The specs in
`docs/specs/hat/` are the source of truth; this file only orders the work. Update the status table
in the same PR that changes it (the orchestrator does so when it opens a builder or merges).

## Status

| Plan PR | What | GitHub PR | State |
|---|---|---|---|
| PR 1 | HAT specs, old-doc cleanup | #83 | merged |
| PR 2 | Foundation, schema A, job framework | #84 | merged |
| PR 3 | Schema B, event store | #85 | merged |
| PR 4 | Reference data, calendar, NSE MCP client | #86 | merged |
| PR 5 | Historical data build | #89 | merged |
| PR 6 | Features | #92 | merged |
| PR 7 | Nightly ingest, quality gate | #104 | merged |
| PR 8 | Criteria library, bot configs | #102 | merged |
| PR 9 | Portfolio layer | #103 | merged |
| PR 10 | Risk and governance | #88 | merged |
| PR 11 | Engine core | #105 | in review |
| PR 12 | Backtest | | next (after PR 11) |
| PR 13 | Broker interface, Fyers adapter | #87 | merged |
| PR 14 | Live data | #90 | merged |
| PR 15 | Execution router | | next (after PR 11) |
| PR 16 | Telegram | | after PR 15 |
| PR 17 | Wiring, API, dashboard, deploy | | after PR 7, 12, 16 |

A clean-up series (K1 to K6, #91 to #101) removed all pre-HAT code before PR 7; the repo is HAT only.

## Ordered PR list

Each PR has a single builder, must stay green on all four CI jobs, and adds **zero** new warnings. Every PR adds tests under `backend/tests/hat/` (a new package with an `__init__.py`-free layout like the existing tests). It reuses `FakeBroker`, `freeze_market` and `FixedClock`, and **never** calls real Fyers/NSE/Telegram (HTTP is faked with a recorded-fixture transport). Any PR that adds or changes a route regenerates `frontend/lib/api/schema.d.ts` (`pnpm gen:api`). Execution PRs (13, 15, 16) follow `.claude/skills/trading-safety/SKILL.md` and list rule-by-rule compliance in the PR description.

### Dependency graph and parallel waves

```
W0  PR1 specs
W1  PR2 foundation + schema A + job framework
W2  PR3 schema B + event store        PR4 reference data, calendar, NSE MCP client
W3  PR5 historical build (4)          PR10 risk & governance (3)       PR13 broker interface + Fyers adapter (3)
    PR14 live data (3,4)
W4  PR6 features (5)                  PR7 nightly ingest + quality gate (5)
W5  PR8 criteria + bot configs (6)    PR9 portfolio layer (3,6)
W6  PR11 engine core: scan, stops, exits, lifecycle, shadow (8,9,10)
W7  PR12 backtest (11)                PR15 execution router & live trade mgmt (11,13,14)
W8  PR16 Telegram (10,15)
W9  PR17 daily-cycle wiring, HAT API + dashboard, Compose, runbook (7,12,16)
```

That is 17 PRs: 1 docs plus 16 code. Up to 4 can be built in parallel (W3).

---

### PR 1: HAT specs and old-doc cleanup
- **Scope:** section 5 above. Docs only, plus comment-line edits.
- **Files:**
  - `docs/specs/hat/00…10*.md` and `README.md`;
  - deletions per 5.2(a); `AGENTS.md`→`CLAUDE.md`;
  - reference edits per 5.2(b).
- **Tests:** existing suite. **Acceptance:** CI green; `git grep -nE "docs/(ARCHITECTURE|Websocket|plans|execution|architecture-review)|AGENTS\.md|_PRD"` returns nothing; every requirement has an ID and acceptance bullets.
- **Deps:** none.

### PR 2: HAT foundation, schema A, job framework
- **Implements:** 00 §3; 03 §1–§4, §7 (job_run, quality_check), §8 (strategy/universe/risk-policy rules), §10; 10 §2 (framework); 01 T1/T5/T7/T10.
- **Scope:**
  - Package skeleton: `backend/app/hat/{__init__,settings,ids,money,time}.py` and subpackages `data/ engine/ execution/ risk/ telegram/ ops/ models/`.
  - Settings: `HAT_ENABLED` (default false), `HAT_USER_ID`, `HAT_DATA_ROOT` (added to `app/paths.py`).
  - `uuid7()` helper; Decimal money helpers (reuse `services/execution/paper_book.to_decimal/to_money`); IST trade-date helpers.
  - Add `polars`, `duckdb` to `backend/requirements.txt` and regenerate `requirements-ci.lock`.
  - Models for reference (9), strategy/risk/bot (9, including the `hat_bot` sleeve columns), portfolio (7), `hat_job_run`, `hat_quality_check`.
  - `app/hat/schema.py:ensure_hat_schema()` with triggers/CHECKs for version immutability, one-live-version partial indexes, and the risk-increase 7-day rule. Called from `init_db()`.
  - Seed `hat_market(NSE)`.
  - `app/hat/ops/jobs.py`: `run_job(name, trade_date, fn)`. It is idempotent on unique (job, trade_date), records status/calls/rows/error, and is safe on restart.
  - `app/hat/ops/scheduler.py`: an asyncio loop that is not started unless `HAT_ENABLED`.
  - Import-boundary test.
- **Tables:** 27 (listed above).
- **Tests:**
  - SQLite create_all still works for the whole suite.
  - Postgres: UPDATE of a non-draft version config raises; two live versions are rejected; an increase with `effective_from` < +7 d is rejected; a risk version UPDATE (other than `superseded_at`) raises.
  - The job runs once per (job, date); a failed job can be retried.
  - Import-graph rule (HAT-OVR-003).
- **Acceptance:** CI green including `backend-postgres`; `init_db()` is idempotent when run twice on PG; the existing 1510 tests are unchanged.
- **Deps:** PR 1.

### PR 3: Schema B, event store, invariant-1 trigger
- **Implements:** 03 §5, §6, §7 (rest), §8 (stop trigger, event append-only), §9; 01 I15; 10 §2 (partition job).
- **Scope:**
  - Runtime models (8), `hat_event` (partitioned on PG, PK `(id, recorded_at)`), `hat_backtest_run (+family_key)`, `hat_broker_session`, `hat_telegram_message`, `hat_command_log`, `hat_daily_bot_snapshot`.
  - Indexes per the doc, including unique `hat_order(tag)`, `hat_signal` unique (bot, playbook, instrument, trade_date), and partial `hat_position(bot_id) where closed_at is null`.
  - `app/hat/events.py`: `record(session, *, aggregate_type, aggregate_id, event_type, actor, payload, correlation_id, causation_id)`. It writes in the caller's transaction and validates the event type against the MVP event catalogue.
  - The `events.partitions` job function.
  - Retention purge job for Telegram/command logs (1 year).
- **Tables:** 14.
- **Tests:**
  - Postgres: UPDATE/DELETE on `hat_event` raises; lowering `current_stop` raises and raising it succeeds; inserts land in the right monthly partition and the partition job creates the next months.
  - Unit: state change and event are atomic, so a rollback leaves neither.
  - Duplicate signal key is rejected.
- **Acceptance:** CI green; the event catalogue matches spec 03 §6 exactly (a test enumerates it).
- **Deps:** PR 2.

### PR 4: Reference data, trading calendar, NSE MCP client
- **Implements:** 02 §2, §3; 03 §2; 01 I1 (client), C7.
- **Scope:**
  - `app/hat/data/mcp_client.py`: a thin JSON-RPC-over-HTTP MCP client (tools/call). It has a configurable endpoint per server (`nse-bhavcopy`, `cm-market`), retry ×3 with backoff, a concurrency limit (4), and a per-call log (tool, symbol count, returned date). It is behind a `McpTransport` protocol so tests use recorded JSON fixtures.
  - Instrument master builder (ISIN+series key; delisted kept). Sources: `cm_get_equity_stocks` prefix-paged A–Z, plus the NSE equity list CSV fallback, plus the existing `symbol_master`/`symbol_history` for renames → `hat_instrument_symbol_history`.
  - `hat_broker_symbol` for Fyers via `symbol_master.to_fyers`.
  - Price-band and industry loaders (file location `[verify]`; parsers coded against a fixture plus a fallback to broker quote circuit limits).
  - NSE holiday list → `hat_trading_calendar`, feeding `market_hours.DEFAULT_POLICY.holidays` at startup (C7).
  - Job functions `reference.weekly`, `bands.nightly`, `calendar.refresh`.
- **Tests:**
  - MCP client: batching, retry, a date mismatch is reported, and errors never leak raw text.
  - Instrument upsert is idempotent; a rename creates a history row.
  - A holiday is closed in `MarketHoursPolicy`, and existing market-hours tests still pass.
- **Acceptance:** CI green; with fixtures, 3,121 symbols resolve to instruments; no network in tests.
- **Deps:** PR 2.

### PR 5: Historical data build (2010 → today) and Parquet/DuckDB store
- **Implements:** 02 §5 (CA storage + adjusted view), §8, §9; 01 I2, T6, B27.
- **Scope:**
  - `app/hat/data/store.py`: Parquet writers/readers for `bars_daily_raw/date=…` and `index_daily/date=…`, atomic file replace, and DuckDB views `bars_daily_adj` (raw × cumulative factors from `hat_corporate_action`) and `bars_daily` (latest per instrument).
  - `app/hat/data/bhavcopy.py`:
    - downloader (browser-like headers, ~1 req/s, resumable, raw zips kept immutable in `raw/`, missing file → holiday candidate);
    - parsers for **old CM bhavcopy** and **UDiFF**, normalised to the one schema keyed by ISIN+series. Reuse the `phase1_build._bhavcopy_equity_rows` column knowledge and `_parse_corp_action_factor`/`find_bonus_ratio` by moving them into a shared helper and keeping `phase1_build` working.
  - Corporate actions loader (`get_corporate_actions` from 2010, archive CSV fallback) → `hat_corporate_action`.
  - Index history (NIFTY 50/500, Smallcap 250, India VIX) → `index_daily`.
  - Validation report (row counts per day; 50 random symbol-days against `get_stock_history`).
  - CLI: `python -m app.hat.data.history_build --from 2010-01-01 [--to] [--resume]`.
- **Tests:**
  - Parsers on tiny fixture zips of both formats produce identical normalised rows.
  - Adjusted view: a 1:2 split halves pre-ex-date prices while raw stays unchanged.
  - Resume skips downloaded days.
  - The phase-1 golden tests still pass.
- **Acceptance:** CI green; the build runs end-to-end on fixtures offline; real-network runs are manual only.
- **Deps:** PR 4.

### PR 6: Features (polars)
- **Implements:** 02 §7; 01 T11.
- **Scope:**
  - `app/hat/data/features.py`: SMA10/20/50/150/200, 52w high/low, ADR20, ADTR20, ADV20, turnover20 (close×volume where traded value is null), ret21/63/126/252, rs_pct (weights 0.4/0.2/0.2/0.2 over 3/6/9/12 m, universe percentile), mom_rank. All on adjusted bars → `features_daily/date=…`.
  - Breadth inputs (% above 50 SMA, A/D) for the regime.
  - The `features` job (incremental by date; full rebuild flag).
- **Tests:**
  - Golden values on a hand-checked fixture.
  - Equivalence with the existing pandas `services/vcp/pattern.compute_rs_map`/stage-2 averages where definitions match.
  - No look-ahead: features for date d use bars ≤ d.
- **Acceptance:** CI green; the full-history rebuild is documented with an expected runtime.
- **Deps:** PR 5.

### PR 7: Nightly ingest via NSE MCP, quality gate, fallback
- **Implements:** 02 §4, §5 (flagging), §6; 01 I1, I14; 10 §3 steps 7–8, §4.
- **Scope:**
  - The `ingest.nightly` job:
    - probe `get_bulk_quote(["RELIANCE"])` until date = trade date (10-min poll, 22:00 cutoff);
    - fetch 50/batch, 4 in parallel; row checks;
    - CA flagging (prev_close mismatch, >20% move, CA RSS) → `get_corporate_actions` for flagged symbols only;
    - write raw Parquet (`traded_value` null) plus index snapshot (including VIX) and `get_market_breadth`;
    - at 22:00 fall back to the PR 5 bhavcopy downloader for the day.
  - `app/hat/data/quality.py`: coverage ≥99% `[A]`, zero OHLC violations, every >20% move explained or excluded, index snapshot present → `hat_quality_check` rows. A failure emits `quality.failed`, sets the data-health kill state (consumed by PR 10), and raises a Telegram alert (via a `Notifier` protocol; Telegram implements it in PR 16).
  - Events `ingest.completed/failed`.
- **Tests:**
  - A late-data poll sequence (FixedClock) ends at fallback at 22:00.
  - Gate pass/fail matrix.
  - A re-run on the same date is a no-op (job idempotency).
  - The flagged-symbol set is correct.
- **Acceptance:** CI green; the whole nightly path runs offline against fixtures.
- **Deps:** PR 5 (and PR 3 for events).

### PR 8: Criteria library (daily stages), config model, two default bots
- **Implements:** 04 §1–§8 (live trigger/entry-filter *evaluation* is in PR 15, but their schemas are registered here); 01 B3–B7, B22–B23, T10.
- **Scope:**
  - `app/hat/engine/criteria/`: a registry of the 24 types. Each has a pydantic param model (→ JSON Schema stored in `hat_criterion_type`), a stage, data needs, and a pure function over polars frames returning `(passed, score∈[0,1], value, reason)`.
  - Implement the universe and setup types now: `series_in`, `min_turnover`, `min_price`, `price_band_allowed`, `adr_min`, `sma_above`, `sma_rising`, `pct_from_52w_low/high`, `rs_rank`, `momentum_rank`, `vcp` (port of `services/vcp/pattern.py`, scored, exposing pivot + final contraction low), `consolidation`, `ma_surf`, `prior_move`, `earnings_growth` (registered, disabled; no data until P2) and `neglect`.
  - Config model: 9 stages, playbooks, hard short-circuit, weighted setup score with threshold, `enabled`/`order`, YAML import/export, `config_hash` (sha256 of canonical JSON), versioning service (draft creation; publish = immutable).
  - Seeds: S1 `stage2-contraction-breakout` v1 and S2 `momentum-breakout-ep` v1 YAML, exactly per the docs.
- **Tests:**
  - One test module per criterion with literal expected values.
  - Invalid params fail to save; the hash is stable under key reordering.
  - The YAML round-trip is lossless.
  - VCP port parity with the existing detector on the existing fixtures (`tests/test_vcp_pattern.py` data).
  - Import-boundary rule.
- **Acceptance:** CI green; both default configs load, validate and hash deterministically.
- **Deps:** PR 6 (feature column contract), PR 2.

### PR 9: Portfolio layer: universes, regime v1, health, allocator, lifecycle
- **Implements:** 05 §1–§6; 01 PL1–PL5.
- **Scope:**
  - Universe versions: manual definition (seed from NIFTY 500 constituents via `services/universe` + include/exclude), point-in-time `hat_universe_member`, and the one-live-version rule.
  - `regime.py`: trend (NIFTY 500 vs 50/200 SMA), VIX 252-d percentile, breadth (% > 50 SMA, A/D) → 4 states → 3-day confirmation → `hat_regime_state`, plus `hat_regime_model_version` (rules v1).
  - `health.py`: drawdown bands 8/12% → 1.0/0.5/0 → probation trigger.
  - `allocator.py`: w = b × m_regime × m_health, with bounds [0.25, 1.0] from the policy, smoothing ≤0.25/day, a cluster cap for S1+S2, and the remainder as cash → `hat_allocation_daily` (capital, risk_pct).
  - Lifecycle state machine with guards (the backtest evidence check is pluggable; PR 12 supplies the deflated Sharpe).
  - Jobs `regime`, `allocate`.
- **Tests:**
  - State rules table and the 3-day rule.
  - Smoothing caps the move.
  - Bounds come from the policy (a multiplier > 1 is impossible without a policy increase).
  - The cluster cap holds.
  - Lifecycle transitions are allowed or denied per the table and emit events.
- **Acceptance:** CI green; allocation rows reproduce a hand-computed fixture.
- **Deps:** PR 3, PR 6.

### PR 10: Risk policy, sizing, caps, kill switches, governance (single risk authority)
- **Implements:** 06 §1–§10; 01 B11–B16, B20–B21, B26; 03 §3 (`hat_kill_switch`, `hat_exposure_state`, `hat_mode_change`).
- **Scope:**
  - `app/hat/risk/policy.py`: versioned policy (initial seed: 0.25% risk, p_max 20%/25%, 1% ADV, global open risk 3%, 6 positions, industry 2, entries/day 3, stop-outs/day 3, ladder L1–L3, kill 15%/10%, base weights, multiplier bounds). The change service handles decrease (next session) and increase (+7 d, evidence gate ≥50 live trades with expectancy in tolerance).
  - `sizing.py`: the exact formula, Decimal, floor, q=0 → skip with reason.
  - `caps.py`, `exposure.py` (ladder).
  - `kill.py`: account/bot drawdown from `hat_daily_bot_snapshot`, manual, data/broker health. Re-arm is manual (or automatic for health).
  - Invariants 1–6.
  - **`RiskManager.evaluate_signal(signal_ctx) -> RiskDecision`** in `services/risk_manager.py`. It composes the existing market-hours/price-band/margin checks with the HAT checks and returns warnings plus a typed reason code. Existing methods are untouched.
  - Mode-change queue (effective next session from the calendar); pause/resume.
  - `snapshot.bots` job.
- **Tests:**
  - Sizing literals (including each cap binding).
  - Every cap breach gives its reason code.
  - Ladder moves.
  - Kill trips, blocks entries, never blocks exits.
  - Increase rejected inside cooling-off.
  - Invariant tests (no entry without stop, no order on stale data or outside hours, using `freeze_market`).
  - The existing `test_risk_fixes.py`/`test_live_trading_safety.py` stay green.
- **Acceptance:** CI green; no HAT path can size an order without `evaluate_signal`, enforced by a test on the router contract in PR 15.
- **Deps:** PR 3.

### PR 11: Engine core: nightly scan, stops, exit stack, trade lifecycle, shadow fills
- **Implements:** 04 §1, §3, §5; 06 §5; 07 §4, §5, §7 (rule logic), §13 (shadow tracking of skips); 09 §1; 01 T12, B18–B19.
- **Scope:**
  - `app/hat/engine/scan.py`: per bot, universe → strategy regime gate → setup → `hat_watchlist_entry` (score, criteria_results, pivot, planned_stop, adtr), plus `watchlist.published`. The same function is called by backtest and live.
  - Stop rules: `below_final_contraction_low` (buffer, max 8% → skip), `low_of_day`, `pct_below_entry`, `atr_multiple`, `swing_low`.
  - Exit stack: the 9 rules and the evaluation rules (full beats partial, fire-once, fractions of the original qty, never lower a stop).
  - Trade lifecycle state machine (11 states), driven by events, with state derived and persisted in the same transaction.
  - Virtual fill engine for shadow (`is_virtual=true` orders/fills/positions); `hat_trade_result` on close (R, MFE/MAE).
- **Tests:**
  - Scan on fixture features gives the expected watchlist.
  - Each stop rule; the exit-stack conflict cases.
  - Lifecycle replay from the event log reproduces state.
  - A shadow trade end-to-end makes zero broker calls.
- **Acceptance:** CI green.
- **Deps:** PR 8, PR 9, PR 10.

### PR 12: Backtest engine (shared), cost model, trials, deflated Sharpe
- **Implements:** 09 §1–§6; 05 §6 (evidence); 01 T12, T16, PL4.
- **Scope:**
  - `app/hat/engine/backtest.py`: a day loop over the Parquet store that calls the PR 11 scan/risk/exit code. The trigger proxy on daily bars is documented (pivot break on high, with ORB not modelled unless Fyers minute history is loaded through the optional `FyersClient.get_historical_data` loader).
  - Fill and cost model: slippage bps, STT, stamp duty, exchange + SEBI fees, GST.
  - `hat_backtest_run` with `family_key`, trial count, metrics via `services/backtest_metrics.py` plus expectancy/R stats, deflated Sharpe probability (Bailey & López de Prado), `trades.parquet`.
  - Lifecycle evidence hook (≥200 trades, DSR ≥0.95).
  - Replay test harness (fixed fixture market → golden metrics JSON, like the existing `tests/fixtures/*_golden.json`).
  - CLI `python -m app.hat.engine.backtest --bot … --from … --to …`.
- **Tests:**
  - Golden replay.
  - Trial counting per family.
  - DSR against a published worked example.
  - Cost model literals.
  - Backtest and live scan produce identical watchlists for the same date.
- **Acceptance:** CI green; S1 and S2 backtests run on fixtures.
- **Deps:** PR 11.

### PR 13: Broker interface and Fyers adapter
- **Implements:** 07 §8–§11, §6 (GTT mechanics); 01 I7–I10, B17, B24, T8, T14.
- **Scope:**
  - `app/hat/execution/broker.py`: a `HatBroker` protocol with `capabilities()`, `login_url()`, `complete_login(code)`, `session_status()`, `place_order(intent)`, `modify_order`, `cancel_order`, `place_gtt_stop`, `modify_gtt`, `cancel_gtt`, `orders/trades/positions/holdings/funds`, `instruments()` and `order_updates()` (polling).
  - `fyers_adapter.py`: wraps the existing `FyersBroker` (place/modify/cancel/positions/funds, circuit breaker, unconfirmed handling, `find_order_by_tag`), `FyersClient`, and `FyersAuthService` (login link + exchange), plus new GTT calls via the SDK (`place_gtt_order` etc.) with a payload-exact mapping, CNC only.
  - HAT tag encoder `HT`+purpose+16 characters (C12).
  - Persist `hat_order` as PENDING before the broker call, idempotent on tag; unconfirmed → reconcile by tag.
  - Reconciliation service (startup and periodic): match broker orders/positions to `hat_order`/`hat_position` by tag, and fail closed.
  - `hat_broker_account` + capability check (the onboarding steps without OpenAlgo); `hat_broker_session`.
  - Extend `tests/fakes/broker.py` `FakeBroker` with GTT and tag lookups (do not create a new fake).
- **Tests:**
  - Exact Fyers payload per order type and for GTT (trading-safety rule 4).
  - A retry with the same tag never double-places.
  - Unconfirmed → found by tag.
  - Broker down → raises, never an empty list.
  - No broker error text leaks.
  - Session status mapping.
- **Acceptance:** CI green; the trading-safety checklist is in the PR body.
- **Deps:** PR 3.

### PR 14: Live data: tick hook, 5-min bars, staleness, pre-open, gap scan, announcements
- **Implements:** 02 §10–§12; 01 I3, I6, B8–B10; 10 §3 steps 2–4.
- **Scope:**
  - `LiveMarketService.add_tick_listener(cb)`: a minimal change. It runs on the loop thread, can never block the broadcast, and a listener exception is logged and isolated, with tests.
  - HAT registers as one `SubscriptionRegistry` owner (`"hat"`) for watchlist, positions and EP candidates (<200).
  - `app/hat/data/bars.py`: 5-min bars aligned to 09:15, final at close+2 s; cumulative volume and volume pace; opening-range high/low after 09:20; `bars_5m` Parquet persistence.
  - Feed staleness: >30 s of silence in market hours → `feed.stale` → the health kill state.
  - Pre-open gap capture (`[verify]` Fyers fields; fall back to the open of the first tick).
  - 09:16 full-market gap scan via MCP `cm_get_equity_stocks` (~26 calls) → subscribe gappers ≥10% passing the universe and 10%/20%-band/F&O rules.
  - RSS poller (Announcements, Financial Results, Corporate Actions) → `hat_announcement` (GUID dedupe; instrument match by symbol/ISIN; unmatched kept).
- **Tests:**
  - Bar alignment and finalisation under FixedClock; volume pace math; staleness trip/clear.
  - Listener isolation, and the existing `test_live_market_*` tests stay green.
  - RSS dedupe and matching on fixtures.
  - Gap-scan filtering.
- **Acceptance:** CI green; existing WebSocket behaviour is unchanged.
- **Deps:** PR 3, PR 4.

### PR 15: Execution router and live trade management
- **Implements:** 07 §1–§3, §5–§7 (live), §12, §13; 06 §7 (live invariants); 04 §4 (trigger and entry-filter evaluation); 01 B1, B2, B8, B9, B17, B18, B25.
- **Scope:**
  - Live criteria: `price_breaks_level`, `volume_pace`, `gap_up`, `early_volume`, `catalyst_attached`, `neglect` (live use), `adtr_filter`, `max_chase` (always on, even if deleted; skip at upper circuit).
  - Trigger monitor over PR 14 bars → `hat_signal` (id = idempotency key; one per bot/playbook/instrument/day) → `RiskManager.evaluate_signal` → router:
    - **shadow:** virtual fills via PR 11;
    - **confirm:** `hat_approval` with a 5-min expiry, a price re-check against max-chase on take, and skips continue as shadow trades for veto scoring;
    - **auto:** a real order via `HatBroker`, blocked when the broker lacks GTT.
  - On fill: an intraday SL order at the stop.
  - Live exit rules (breakeven, partial at R) on price events.
  - 15:20–15:25 `exits.close`; `gtt.swap` (1% limit offset `[A]`); `gtt.verify` nightly.
  - Order rejected → retry at most once, then an alert.
  - Reconcile before the first order.
  - A `Notifier` protocol for cards and alerts (no-op default).
- **Tests:**
  - The three modes end-to-end with FakeBroker (shadow: zero calls; auto: the exact payloads).
  - Approval expiry; a chase-exceeded take is refused.
  - A duplicate trigger is refused.
  - Exits never ask.
  - A stop is never lowered.
  - Stale feed → no entry.
  - Outside hours → no order.
  - Kill → entries blocked, exits continue.
- **Acceptance:** CI green; trading-safety checklist; HAT-EXEC-010 is proven.
- **Deps:** PR 11, PR 13, PR 14.

### PR 16: Telegram
- **Implements:** 08 §1–§5; 01 I11–I13, T14, T15.
- **Scope:**
  - `app/hat/telegram/`: a Bot API client over `aiohttp` (already a dependency) with long polling and offset persistence.
  - Single allowlisted chat; unknown chats are logged in `hat_command_log`.
  - Commands per 4.4. Mode changes outside hours only; `auto`, `/arm` and `/flatten` need a second confirm; `/flatten` logs a discipline breach and goes through `LiveExitService`-style exit-only paths.
  - Approval cards with Take/Skip buttons carrying an HMAC-signed, single-use, expiring token (`hat_approval.token_hash`).
  - Outbound messages: login prompts 08:30/09:05, watchlist digest, shadow trade, fill/exit/stop moved, rejection digest 15:35, health alerts, daily P&L 15:40, weekly veto digest.
  - `hat_telegram_message` linking.
  - Implements the `Notifier` used by PRs 7 and 15.
- **Tests:**
  - Allowlist; token forgery, replay and expiry rejected.
  - Second-confirm flows.
  - No free-text order path.
  - Message rendering literals.
  - The HTTP layer is faked.
- **Acceptance:** CI green; no inbound port; the token never appears in logs.
- **Deps:** PR 10, PR 15.

### PR 17: Daily-cycle wiring, HAT API and dashboard, deployment
- **Implements:** 10 §1–§9; 00 §3; 01 T3, T7, T9, T13.
- **Scope:**
  - Register all jobs in the scheduler with calendar awareness (skip non-trading days).
  - Lifespan start/stop behind `HAT_ENABLED` (refuse on SQLite).
  - Restart safety: reconcile, then resume.
  - Read-only `GET /api/hat/{status,bots,watchlist,signals,positions,allocations,regime,events}` routers (sync `def`, `get_db`, no broker text in errors), then regenerate `schema.d.ts`.
  - One frontend page `frontend/app/hat/page.tsx`: bots, regime, allocations, watchlist, open positions, recent events. Read-only, using the realtime hooks for LTP, and no `useEffect`.
  - `deploy/hat/docker-compose.yml` (Postgres 18 + backend; frontend optional), `.env.example` entries, a `pg_dump` + Parquet backup script, and a runbook in `docs/specs/hat/10-ops.md` §9 (update).
- **Tests:**
  - The scheduler fires the daily cycle in order under FixedClock across a simulated day, including the 22:00 fallback and a holiday skip.
  - API contract tests; frontend `vp check`/test; `api-schema` in sync.
- **Acceptance:** CI green; with `HAT_ENABLED=true` against fixture data and FakeBroker, a simulated full day runs from end to end in shadow mode.
- **Deps:** PR 7, PR 12, PR 16.

---

