# 01: Decisions

The decisions register from the design docs (MA "Decisions register"), with the 20 Oct revisions of
B1, B2 and B12, plus the owner's build decisions of 20 Oct. Each decision is a requirement with ID
`HAT-DEC-<code>`. Most decisions are detailed by requirements in later files; the decision's
property here is the top-level promise, and the section reference says where the detail lives.
Requirement format and verification methods: see [README.md](README.md) and
[11-verification.md](11-verification.md).

Precedence: the build decisions in §5 override the docs where they conflict (T13, I7, I8 and the
OpenAlgo rows). The owner's verification instruction (OWN-FV) overrides T16.

## §1 Tech decisions (T1-T16)

#### HAT-DEC-T1: Multi-tenant-ready schema
- **Statement:** Every owned `hat_` table carries `user_id`; the MVP uses one fixed user UUID from settings (`HAT_USER_ID`).
- **Source:** MA "Recommended options" (T1); SCH "Summary and conventions" (Tenancy).
- **PR:** 2, 3
- **Verification:** CONF `user_id_on_owned_tables`
- **Acceptance:**
  - Property: every `hat_` table that the schema spec lists with `user_id` has it as a non-null column, and every row HAT writes carries `HAT_USER_ID`. Harm: going multi-user later would need a data migration across the event log and money tables. Detail: 03 §1.

#### HAT-DEC-T2: Python core
- **Statement:** The engine is Python inside the existing FastAPI backend.
- **Source:** MA "Recommended options" (T2), T4.
- **PR:** 2
- **Verification:** CONF `hat_package_in_backend`
- **Acceptance:**
  - Property: all HAT engine code lives under `backend/app/hat/` and is importable by the backend's Python version. Harm: a second runtime adds a deployment and a boundary nobody verifies.

#### HAT-DEC-T3: Web dashboard plus Telegram
- **Statement:** HAT exposes read-only dashboard views through `/api/hat/*` and one frontend page; Telegram is the control surface. The only writes are the two Fyers login routes, `POST /api/hat/auth/fyers/token` and `POST /api/hat/auth/fyers/disconnect`. They write only the token file and run behind the write guard (HAT-DEC-BD5).
- **Source:** MA "Recommended options" (T3); MA "Reuse vs build" (KPI dashboards); design review 2026-10-22 item 17 (the two POSTs were exempt without being named here).
- **PR:** 16, 17, 38
- **Verification:** CONF `hat_routes_read_only`, CONF `hat_writes_guarded`
- **Acceptance:**
  - Property: every route under `/api/hat/` is an HTTP GET with no side effect on HAT tables, except the two Fyers login POSTs, and each of those depends on the write guard. Harm: an unauthenticated localhost API could change modes, risk or positions outside the governed Telegram path. Detail: 10 §1, 08 §3.
- **Changed (PR 38):** the two login POSTs are named in the statement and must carry the write guard; before this the CONF check exempted them silently.
- The owner's setup and release steps (seeds, onboarding, universe, versions, lifecycle edges) run through the local admin command line `python -m app.hat.admin` on the host, never through a route (10 §9, HAT-OPS-091, PR 35; CONF `ops.admin_cli_calls_services` checks that nothing imports it).

#### HAT-DEC-T4: Extend SmartTrader
- **Statement:** HAT extends SmartTrader; SmartTrader's stack wins wherever it differs from T2, T3, T5 or T6.
- **Source:** MA "Your overrides" (T4).
- **PR:** 2-17
- **Verification:** CONF `existing_suite_green`
- **Acceptance:**
  - Property: every HAT PR keeps the existing SmartTrader CI jobs green and adds no second web framework, ORM or frontend toolchain. Harm: two stacks double the maintenance and split the risk authority. Detail: 00 §3-§5.

#### HAT-DEC-T5: Postgres
- **Statement:** HAT's system of record is the SmartTrader Postgres database; the HAT runtime refuses to start on SQLite.
- **Source:** MA "Recommended options" (T5); PLAN §3 "Migrations" step 6.
- **PR:** 2, 17
- **Verification:** CONF `hat_runtime_requires_postgres`
- **Acceptance:**
  - Property: for every database URL whose dialect is not `postgresql`, starting the HAT runtime with `HAT_ENABLED=true` raises before any job or listener starts. Harm: HAT runs without the DB-enforced governance (triggers, CHECKs) it depends on. Detail: 03 §10, 10 §1.

#### HAT-DEC-T6: Parquet plus DuckDB for price history
- **Statement:** Daily bars, features, 5-minute bars and index levels live in date-partitioned Parquet read through DuckDB, not in Postgres.
- **Source:** MA "Recommended options" (T6); SCH "Parquet datasets"; PLAN C1.
- **PR:** 5
- **Verification:** PBT `ingest.parquet_roundtrip`
- **Acceptance:**
  - Property: for every set of bar rows written for a date, reading the same date back through DuckDB returns the same rows and values. Harm: scans and backtests run on bars that differ from what was ingested. Detail: 02 §8.

#### HAT-DEC-T7: In-process scheduler, jobs in Postgres
- **Statement:** An in-process asyncio scheduler runs HAT jobs, each recorded in `hat_job_run` keyed by (job, trade_date).
- **Source:** MA "Recommended options" (T7); SCH "Backtest and ops tables"; PLAN C4.
- **PR:** 2, 17
- **Verification:** TLA `JobScheduler!AtMostOnceSuccess`
- **Acceptance:**
  - Property: in every reachable state, for every (job, trade_date) there is at most one `hat_job_run` with status success. Harm: a nightly job double-writes data or double-sends alerts after a restart. Detail: 10 §2.

#### HAT-DEC-T8: One async service per broker session
- **Statement:** Each broker account's session is owned by one async service that serialises its broker calls.
- **Source:** MA "Recommended options" (T8).
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!OneSessionOwner`, PBT `router.desk_calls_serialised`
- **Acceptance:**
  - Property: in every reachable state, at most one session service holds a live session for a given broker account. Harm: two sessions race on the same account and invalidate each other's token mid-order. Detail: 07 §9.
  - Property: every broker call of an account's desk (the reconciler, the router, the stop desk, fill sync, the trade manager and the session check) runs through the account's owner, so no two overlap, and a session check reads the status once and records that same status. Harm: concurrent sends on one account race the reconciliation gate, and the recorded session status disagrees with the one that raised the alert.

#### HAT-DEC-T9: Self-hosted machine
- **Statement:** HAT runs on one always-on self-hosted machine with an ISP static IP registered with the broker, a UPS and a backup internet link used for alerts and manual login only.
- **Source:** MA "Your overrides" (T9); MA "Deployment and security".
- **PR:** 17
- **Verification:** OPS `host_static_ip_ups_failover`
- **Acceptance:**
  - Property: on every trading day, the host that places orders egresses from the static IP registered with the broker app; when it does not, no new entries are placed and an alert is sent. Harm: orders are rejected by the broker's static-IP rule and positions go unmanaged. Detail: 10 §5.

#### HAT-DEC-T10: Criteria versioned in the DB, YAML import and export
- **Statement:** Strategy configs are versioned JSONB rows validated against criterion schemas, with lossless YAML import and export.
- **Source:** MA "Recommended options" (T10); SCH "Summary and conventions" (Config).
- **PR:** 8
- **Verification:** PBT `config.yaml_round_trip`, PBT `config.hash_canonical`
- **Acceptance:**
  - Property: for every valid config, export to YAML then import yields an equal config with the same `config_hash`. Harm: a round-trip silently changes a parameter and a different strategy runs than the one reviewed. Detail: 04 §8.

#### HAT-DEC-T11: Own polars indicators
- **Statement:** HAT computes its indicators itself with polars (no TA library), on adjusted bars.
- **Source:** MA "Recommended options" (T11); PLAN C1.
- **PR:** 6
- **Verification:** PBT `features.no_lookahead`
- **Acceptance:**
  - Property: for every bar history and date d, features for d are a function of bars dated ≤ d only. Harm: look-ahead makes backtests look better than live. Detail: 02 §7.

#### HAT-DEC-T12: One engine for backtest and live
- **Statement:** Backtest and live call the same scan, risk and exit functions.
- **Source:** MA "Recommended options" (T12); MA "Boundary rules".
- **PR:** 11, 12
- **Verification:** PBT `backtest.scan_parity`
- **Acceptance:**
  - Property: for every feature snapshot and bot config, the backtest scan and the live nightly scan produce identical watchlist rows for the same date. Harm: backtest evidence no longer predicts live behaviour and the release gate is meaningless. Detail: 09 §1.

#### HAT-DEC-T13: Secrets in `.env`
- **Statement:** HAT secrets (Telegram token, approval HMAC secret, MCP endpoints, DB credentials) live in `.env` outside the repo, readable by the service user only; Fyers credentials stay where SmartTrader keeps them today (no OpenAlgo store).
- **Source:** MA "Your overrides" (T13); OWN-20OCT; PLAN C5, §7.
- **PR:** 2, 17
- **Verification:** CONF `secrets_not_in_repo`, OPS `env_file_permissions`
- **Acceptance:**
  - Property: no tracked file in the repo contains a value for `HAT_TELEGRAM_TOKEN`, `HAT_APPROVAL_SECRET` or a broker secret, and every HAT setting holding a secret is read from the environment. Harm: a leaked token lets anyone approve trades or control the bots. Detail: 10 §6.

#### HAT-DEC-T14: Manual morning login via Telegram link
- **Statement:** Each trading day at 08:30 Telegram sends one login link per connected broker, repeated at 09:05 if not done; no entries until logged in.
- **Source:** MA "Recommended options" (T14); MA "Daily cycle" step 1; DS "Outbound messages".
- **PR:** 13, 16
- **Verification:** TLA `ExecutionRouter!NoEntryWithoutSession`
- **Acceptance:**
  - Property: in every reachable state, no real entry order is placed for a broker account whose `hat_broker_session` for the trade date is not live. Harm: orders fail or go out on an expired session. Detail: 07 §9, 08 §2.

#### HAT-DEC-T15: Structured logs plus Telegram alerts
- **Statement:** HAT logs are structured, and every health failure (data gate, stale feed, broker session, broker capabilities changed, a corporate action the rescale cannot handle, order rejected) raises a Telegram alert.
- **Source:** MA "Recommended options" (T15); DS "Outbound messages" (Health alert).
- **PR:** 16
- **Verification:** PBT `telegram.health_alert_for_every_failure`
- **Acceptance:**
  - Property: for every health failure event type in {`quality.failed`, `feed.stale`, `broker.session_lost`, `broker.capabilities_changed`, `corporate_action.unhandled`, `order.rejected`}, recording it causes exactly one health alert message. Harm: the owner does not learn that bots went manage-only or an order failed. Detail: 10 §8.

#### HAT-DEC-T16: Verification instead of unit and replay tests
- **Statement:** T16 ("unit + replay tests") is replaced by the owner's verification rule: TLA+ models, Hypothesis properties, DB checks; replay survives only as a determinism property.
- **Source:** MA "Recommended options" (T16); OWN-FV.
- **PR:** 2-17
- **Verification:** CONF `spec_requirements_well_formed`
- **Acceptance:**
  - Property: every HAT requirement is verified by a method from 11 §1. Harm: guarantees rest on hand-picked examples. Detail: 11.

## §2 Business decisions (B1-B27)

#### HAT-DEC-B1: Three execution modes (revised)
- **Statement:** Each bot runs in one of `shadow`, `confirm` or `auto`; signal-only mode no longer exists.
- **Source:** MA "Recommended options" (B1, revised 20 Oct); BL "Shadow replaces signal-only"; PLAN C10.
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!ModeDomain`
- **Acceptance:**
  - Property: in every reachable state, every bot's execution mode is in {shadow, confirm, auto}. Harm: a bot in an undefined mode routes orders by accident. Detail: 07 §1.

#### HAT-DEC-B2: Default mode confirm (revised, D130/D132)
- **Statement:** A new bot asks for confirm mode (manual approval of each trade); it runs shadow until its sleeve is Live, its account reports GTT for delivery and has DDPI confirmed, and the owner has confirmed the policy's base weights and cluster cap (07 HAT-EXEC-011, 06 HAT-GOV-085).
- **Source:** MA "Recommended options" (B2, revised); D130, D132; plan audit 2026-10-22 PR 45.
- **PR:** 8, 15, 45
- **Verification:** CONF `seed_bots_confirm`
- **Acceptance:**
  - Property: every seeded or newly created `hat_bot` row has `execution_mode = 'confirm'` and lifecycle state research, so it routes shadow until a Live sleeve and the gates allow more. Harm: a new bot trades real money before shadow evidence exists, or comes up in auto. Detail: 07 §1.

#### HAT-DEC-B3: Criteria library with parameters
- **Statement:** Bots are built from a shared library of parameterised criterion types.
- **Source:** MA (B3); BL "Criteria library".
- **PR:** 8
- **Verification:** CONF `criterion_catalogue_complete`
- **Acceptance:**
  - Property: the set of registered criterion types equals the 25 types listed in 04 §4, each with a parameter schema stored in `hat_criterion_type`. Harm: a config refers to a type with no schema and runs unvalidated parameters. Detail: 04 §4.

#### HAT-DEC-B4: Pass/fail plus weighted score
- **Statement:** A symbol makes the watchlist when every hard criterion passes and the weighted setup score clears the threshold.
- **Source:** MA (B4); BL "How criteria combine".
- **PR:** 8
- **Verification:** PBT `scoring.threshold_gate`
- **Acceptance:**
  - Property: for every set of criterion results, a symbol is on the watchlist iff all enabled hard criteria pass and `Σ w_i s_i / Σ w_i ≥ threshold`. Harm: symbols that fail a hard rule get traded. Detail: 04 §3.

#### HAT-DEC-B5: Versioned configs
- **Statement:** Every save creates a new strategy version; signals and trades record the version they ran under.
- **Source:** MA (B5); BL "Summary" (Versioned).
- **PR:** 2, 8
- **Verification:** DB `governance.version_config_immutable`
- **Acceptance:**
  - Property: no UPDATE can change `config` of a `hat_strategy_version` whose status is not draft. Harm: a trade can no longer be explained by the config that produced it. Detail: 03 §8, 04 §8.

#### HAT-DEC-B6: Earnings filter optional, off
- **Statement:** `earnings_growth` is registered but disabled by default; it has no data until P2.
- **Source:** MA (B6); BL "Criteria library" (`earnings_growth`), S1 YAML.
- **PR:** 8
- **Verification:** CONF `seed_bots_match_spec_yaml`
- **Acceptance:**
  - Property: in every seeded strategy version, every `earnings_growth` criterion has `enabled: false`. Harm: a filter with no data rejects every symbol, or passes everything, silently. Detail: 04 §4.

#### HAT-DEC-B7: Scored VCP detector
- **Statement:** VCP quality is a scored criterion (`vcp`, weight 2 in S1).
- **Source:** MA (B7); BL "Criteria library", S1 YAML.
- **PR:** 8
- **Verification:** PBT `criteria.vcp_contract`
- **Acceptance:**
  - Property: for every bar history, the `vcp` criterion returns a score in [0,1]. Harm: an out-of-range score dominates the weighted setup score. Detail: 04 §4.

#### HAT-DEC-B8: Intraday pivot break plus volume pace
- **Statement:** S1 triggers on a 5-minute bar breaking the pivot with volume pace ≥ 1.5× the 50-day average.
- **Source:** MA (B8); BL S1 YAML (`price_breaks_level`, `volume_pace`).
- **PR:** 14, 15
- **Verification:** PBT `criteria.trigger_conjunction`
- **Acceptance:**
  - Property: for every bar sequence, an S1 trigger fires only on a bar whose price breaks the pivot while projected day volume ≥ 1.5 × avg50 volume. Harm: entries on breakouts without volume confirmation. Detail: 04 §4, 07 §12.

#### HAT-DEC-B9: 5-minute opening range
- **Statement:** S2 entries trigger above the 5-minute opening-range high, fixed when the 09:15-09:20 bar closes.
- **Source:** MA (B9); BL "Qullamaggie-style bot"; DS "Live data".
- **PR:** 14, 15
- **Verification:** PBT `bars.opening_range_fixed`
- **Acceptance:**
  - Property: for every tick stream, the opening-range high and low equal the high and low of the 09:15:00-09:20:00 bar and never change after that bar is final. Harm: the trigger level moves during the day and entries fire at the wrong price. Detail: 02 §11.

#### HAT-DEC-B10: NSE announcement attached to EPs
- **Statement:** EP candidates get matching NSE announcements (within 24 hours) attached as catalysts.
- **Source:** MA (B10); DS "Announcements for EP catalysts".
- **PR:** 14, 15
- **Verification:** PBT `announcements.match_window`
- **Acceptance:**
  - Property: for every announcement and candidate, the announcement is attached iff its instrument matches and it was published within 24 hours (parameter) before the check. Harm: an EP is taken on a stale or unrelated catalyst. Detail: 02 §10.

#### HAT-DEC-B11: Regime gate blocks new entries
- **Statement:** When a bot's strategy regime gate is off, the bot opens no new entries; open positions keep their stops and exits.
- **Source:** MA (B11); BL "Regime gate (B11, B12)".
- **PR:** 10, 11, 15
- **Verification:** TLA `ExecutionRouter!GateOffNoEntry`
- **Acceptance:**
  - Property: in every reachable state where a bot's gate is off, no entry signal for that bot is approved, and exit actions are unaffected. Harm: new longs open in a falling market the strategy says to avoid. Detail: 06 §5.

#### HAT-DEC-B12: No regime override (revised)
- **Statement:** No mode, command or config can override the regime gate.
- **Source:** MA (B12, revised); MA "Execution modes".
- **PR:** 10, 16
- **Verification:** TLA `ExecutionRouter!GateOffNoEntry`, CONF `no_override_command`
- **Acceptance:**
  - Property: no Telegram command, API route or config field exists that sets the regime gate to on. Harm: a discretionary override reintroduces the risk the gate exists to remove. Detail: 06 §5.

#### HAT-DEC-B13: Fixed percentage risk
- **Statement:** Each trade risks a fixed share of bot equity, sized from the stop distance.
- **Source:** MA (B13); BL "Position size".
- **PR:** 10
- **Verification:** PBT `sizing.risk_bounded`
- **Acceptance:**
  - Property: for every valid input, `q · (P_entry − P_stop) ≤ E · r`. Harm: a trade loses more than the policy allows when stopped out. Detail: 06 §2.

#### HAT-DEC-B14: Default risk per trade 0.25%
- **Statement:** The initial risk policy sets risk per trade to 0.25% (`risk_pct = 0.0025`).
- **Source:** MA (B14); BL "Position size"; BL S1 notes (1.25-2.5% book risk reduced to 0.25% for phase 1).
- **PR:** 10
- **Verification:** CONF `initial_risk_policy_seed`
- **Acceptance:**
  - Property: the initial `hat_risk_policy_version` (direction `initial`) has `policy.risk_pct = 0.0025` exactly, as a Decimal. Harm: the first live trades risk more than the owner agreed. Detail: 06 §1.

#### HAT-DEC-B15: Per-bot capital plus global open-risk cap
- **Statement:** Each bot gets its capital from the risk policy, and total open risk across bots is capped at 3% of total equity.
- **Source:** MA (B15); BL "Caps".
- **PR:** 10
- **Verification:** PBT `caps.global_open_risk`
- **Acceptance:**
  - Property: for every set of open positions and candidate entry, the entry is approved only if `Σ (entry − stop) × qty` including it ≤ 0.03 × total equity. Harm: correlated stop-outs lose more than 3% in one move. Detail: 06 §3.

#### HAT-DEC-B16: Progressive exposure
- **Statement:** Exposure starts at ladder level L1 and moves up and down on consecutive winners and losers.
- **Source:** MA (B16); BL "Progressive exposure (B16)".
- **PR:** 10
- **Verification:** PBT `exposure.ladder_transitions`
- **Acceptance:**
  - Property: for every sequence of closed-trade results, the ladder level equals the one computed by the 06 §4 table, starting at L1. Harm: full size is used before the bot has shown it is working. Detail: 06 §4.

#### HAT-DEC-B17: GTT overnight, stop orders intraday
- **Statement:** On fill an intraday stop order rests at the broker; around 15:25 it is replaced with a GTT stop for overnight.
- **Source:** MA (B17); BL "Stop placement at the broker (B17)".
- **PR:** 13, 15
- **Verification:** TLA `StopPlacement!AlwaysProtected`, PBT `manage.real_fill_protected`
- **Acceptance:**
  - Property: in every reachable state, every real open position has a resting broker-side stop (intraday SL or GTT). Harm: a gap-down overnight or a machine outage hits an unprotected position. Detail: 07 §6.

#### HAT-DEC-B18: MA exits around 15:25
- **Statement:** Moving-average exits are evaluated on the live price between 15:20 and 15:25 and sent then.
- **Source:** MA (B18); MA "Daily cycle" step 6.
- **PR:** 11, 15
- **Verification:** TLA `JobScheduler!ExitsCloseInWindow`
- **Acceptance:**
  - Property: in every simulated trading day, `close_below_sma` exits are evaluated once in the 15:20-15:25 window using the live price. Harm: MA exits miss the session and positions carry overnight against the rule. Detail: 07 §7.

#### HAT-DEC-B19: Stackable exit rules
- **Statement:** Exits are an ordered stack evaluated top to bottom; a full exit beats partials.
- **Source:** MA (B19); BL "Exit rules", "Evaluation rules".
- **PR:** 11
- **Verification:** PBT `exits.full_beats_partial`
- **Acceptance:**
  - Property: for every position and set of simultaneously firing rules, the resulting exit is the full exit if any full exit fires, else the partials in stack order. Harm: a partial is sent where the rules say the whole position should go. Detail: 07 §7.

#### HAT-DEC-B20: No adding to winners
- **Statement:** A bot never adds to an open position (one entry per position).
- **Source:** MA (B20).
- **PR:** 10
- **Verification:** TLA `ExecutionRouter!NoAddToOpenPosition`
- **Acceptance:**
  - Property: in every reachable state, a bot has no entry order for an instrument in which it holds an open position. Harm: pyramiding raises exposure beyond the sized risk. Detail: 06 §7.

#### HAT-DEC-B21: Max 6 positions per bot
- **Statement:** A bot holds at most 6 open positions (lower while the ladder says so).
- **Source:** MA (B21); BL "Caps".
- **PR:** 10
- **Verification:** PBT `caps.max_positions`
- **Acceptance:**
  - Property: for every bot state, an entry is approved only if the bot's open positions after it ≤ min(6, ladder max). Harm: concentration beyond the policy. Detail: 06 §3.

#### HAT-DEC-B22: All NSE EQ above ₹10 cr turnover
- **Statement:** The default universe stage keeps NSE EQ/BE stocks with 20-day average turnover ≥ ₹10 crore.
- **Source:** MA (B22); BL "Criteria library" (`min_turnover`).
- **PR:** 8
- **Verification:** PBT `criteria.min_turnover_contract`
- **Acceptance:**
  - Property: for every bar history, `min_turnover` passes iff the 20-day average of close × volume ≥ 10 × 10^7 INR. Harm: illiquid stocks enter the universe and fills slip. Detail: 04 §4.

#### HAT-DEC-B23: Include and exclude lists
- **Statement:** Each universe version carries include and exclude lists applied on top of the universe criteria.
- **Source:** MA (B23); BL "Configuration model" (Universe).
- **PR:** 8, 9
- **Verification:** PBT `universe.include_exclude`
- **Acceptance:**
  - Property: for every universe evaluation, an excluded instrument is never eligible and an included one is eligible unless a hard universe criterion fails. Harm: the owner's exclusions are ignored. Detail: 05 §2.

#### HAT-DEC-B24: Delivery only
- **Statement:** Every real HAT order uses product CNC (delivery).
- **Source:** MA (B24).
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!ProductIsCNC`
- **Acceptance:**
  - Property: in every reachable state, every broker order or GTT HAT places has product CNC. Harm: an intraday product is auto-squared off by the broker and the swing trade is lost. Detail: 07 §9.

#### HAT-DEC-B25: Skip at upper circuit
- **Statement:** `max_chase` skips an entry when the stock is locked at its upper circuit.
- **Source:** MA (B25); BL "Criteria library" (`max_chase`).
- **PR:** 8, 15
- **Verification:** PBT `criteria.max_chase_contract`
- **Acceptance:**
  - Property: for every live state where the last price equals the upper band limit with no sellers, `max_chase` fails. Harm: a buy order rests at circuit and fills at the worst price of the day. Detail: 04 §4.

#### HAT-DEC-B26: Kill switches 15% / 10%
- **Statement:** Account equity ≥ 15% below peak puts all bots manage-only; bot equity ≥ 10% [A] below its peak puts that bot manage-only.
- **Source:** MA (B26); BL "Kill switches (B26)".
- **PR:** 10
- **Verification:** TLA `KillSwitch!TripOnDrawdown`, PBT `caps.kill_thresholds`
- **Acceptance:**
  - Property: for every equity series, the account kill switch is tripped iff equity ≤ 0.85 × peak at the snapshot, and a bot switch iff bot equity ≤ 0.90 × bot peak. Harm: drawdowns keep compounding past the agreed stop. Detail: 06 §6.
- **[A]** Bot kill 10%: default 0.10; validated by live review (BL "Defaults that are assumptions").

#### HAT-DEC-B27: Auto-adjust for splits and bonuses
- **Statement:** Adjusted prices are computed from raw bars and corporate-action factors; raw bars are never rewritten.
- **Source:** MA (B27); SCH "Parquet datasets"; DS "Nightly ingest" step 6.
- **PR:** 5, 7
- **Verification:** PBT `corporate_actions.adjusted_view`
- **Acceptance:**
  - Property: for every raw bar series and set of factors, every adjusted price equals raw × the product of factors with ex-date after the bar date, and raw files are byte-identical before and after. Harm: splits look like crashes and trigger false signals or stops. Detail: 02 §5.

## §3 Integration decisions (I1-I17)

#### HAT-DEC-I1: NSE MCP for nightly prices
- **Statement:** Nightly prices come from the NSE MCP (`get_bulk_quote`, 50 symbols per call), polled from 18:00 until the returned date equals the trade date, with bhavcopy download as fallback at 22:00.
- **Source:** MA "Your overrides" (I1); DS "Nightly ingest from NSE MCP".
- **PR:** 4, 7
- **Verification:** TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.batches_of_50`
- **Acceptance:**
  - Property: in every simulated evening, either the MCP data for the trade date is ingested before 22:00 or the bhavcopy fallback runs at 22:00, and never both write the same date. Harm: a night's scan is silently skipped or written twice. Detail: 02 §4.

#### HAT-DEC-I2: History from NSE bhavcopy archives
- **Statement:** A one-off job builds 2010-today history from NSE bhavcopy archives in both formats.
- **Source:** MA "Your overrides" (I2); DS "Historical data build".
- **PR:** 5
- **Verification:** PBT `bhavcopy.parsers_agree`
- **Acceptance:**
  - Property: for every security-day present in both an old-format and a UDiFF fixture, both parsers produce identical normalised rows. Harm: backtests see a price discontinuity on 8 Jul 2024. Detail: 02 §9.

#### HAT-DEC-I3: Fyers WebSocket as the single live source
- **Statement:** All live ticks come from the one Fyers WebSocket that HAT's `LiveFeed` owns.
- **Source:** MA (I3).
- **PR:** 14
- **Verification:** CONF `single_feed_import_rule`
- **Acceptance:**
  - Property: no live-data source exists other than `LiveFeed` tick listeners. Harm: a second source disagrees on prices and triggers fire twice or not at all. Detail: 00 HAT-OVR-006.

#### HAT-DEC-I4: Broker minute history for EP backtests
- **Statement:** EP backtests may use Fyers historical minute data through an optional loader; without it, EP is not backtested on intraday data. On the Fyers Standard plan the loader serves the last 365 days only (100 days a call, 50 calls a minute, 5,000 a day); older intraday history is to come from an NSE source later.
- **Source:** MA (I4); DS "Source map"; PLAN Q4; owner decision D131 of 2026-10-22 (the 1-year download runs on the Standard plan; audit 2026-10-22 live checks, "Fyers history depth": intraday from exactly 365 days back, older ranges refused with -50 for the plan).
- **PR:** 12, 41
- **Verification:** PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_plan_clamp`
- **Acceptance:**
  - Property: for every minute-bar history loaded, the derived 5-minute bars equal the aggregation the live bar builder produces for the same ticks. Harm: EP backtests use different bars from live and overstate results. Detail: 09 §1.
  - Property: for every period reaching before the plan's depth, a refusal for the depth clamps the start to the first date the plan serves and the run loads every symbol from there; any other refusal is a failure, never a clamp. Harm: the download crashes on its first call, or silently skips history it could have had.
- **[OWNER-PENDING]** Daily-bar trigger proxy when minute history is absent: PLAN Q4 default; owner to confirm.
- **[OWNER-PENDING]** Intraday history older than 365 days: from an NSE source (owner, D131); not built.

#### HAT-DEC-I5: Fundamentals later via XBRL
- **Statement:** Fundamentals come in P2 from NSE/BSE XBRL filings stamped with filing time; `hat_fundamental` exists empty in the MVP.
- **Source:** MA (I5); BL "Stock universes".
- **PR:** 2
- **Verification:** PBT `criteria.fundamentals_point_in_time`
- **Acceptance:**
  - Property: for every evaluation date, a fundamental value is visible only if its `filed_at` ≤ the evaluation date. Harm: backtests use results the market had not seen yet. Detail: 05 §7.

#### HAT-DEC-I6: NSE announcements RSS
- **Statement:** NSE Announcements, Financial Results and Corporate Actions RSS feeds are polled and stored, deduplicated by GUID.
- **Source:** MA (I6); DS "Announcements for EP catalysts".
- **PR:** 14
- **Verification:** PBT `announcements.dedupe`
- **Acceptance:**
  - Property: for every sequence of polled feed items, `hat_announcement` holds exactly one row per GUID. Harm: duplicate catalysts or lost ones change which EPs qualify. Detail: 02 §10.

#### HAT-DEC-I7: Own broker interface (direct Fyers adapter)
- **Statement:** HAT defines its own broker interface; the first and only MVP implementation is a direct Fyers adapter (OpenAlgo is not used).
- **Source:** MA (I7); OWN-20OCT; PLAN C5.
- **PR:** 13
- **Verification:** CONF `broker_interface_methods`
- **Acceptance:**
  - Property: the `HatBroker` protocol defines exactly the methods in 07 §8 and the Fyers adapter implements every one of them. Harm: a missing method forces callers around the interface to the SDK. Detail: 07 §8.

#### HAT-DEC-I8: Brokers Fyers first
- **Statement:** The MVP executes through Fyers only; Zerodha then Kotak Neo follow later behind the same interface.
- **Source:** MA (I8); OWN-20OCT.
- **PR:** 13
- **Verification:** CONF `mvp_seed_scope`
- **Acceptance:**
  - Property: every adapter registered in the MVP is the Fyers adapter. Harm: an unverified adapter reaches a live path. Detail: 07 §9.

#### HAT-DEC-I9: In-app broker onboarding
- **Statement:** A broker account is onboarded by a capability check (funds, positions, holdings fetched) before it can be bound to a bot.
- **Source:** MA (I9); DS "Onboarding a broker".
- **PR:** 13
- **Verification:** TLA `ExecutionRouter!AutoNeedsGtt`
- **Acceptance:**
  - Property: in every reachable state, a bot can be in auto mode only when its broker account passed the capability check and reports GTT for delivery. Harm: auto mode runs on a broker that cannot hold an overnight stop. Detail: 07 §8.

#### HAT-DEC-I10: Every order tagged with bot and signal
- **Statement:** Every HAT order carries a tag encoding the signal ID and purpose, unique across orders.
- **Source:** MA (I10); SCH "Runtime" (tag); PLAN C12.
- **PR:** 13
- **Verification:** PBT `tags.encode_decode_roundtrip`, DB `runtime.order_tag_unique`
- **Acceptance:**
  - Property: for every signal uuid and purpose code, the tag is 20 alphanumeric characters starting `HT`, and decoding it yields the same signal prefix and purpose. Harm: reconciliation cannot tell HAT orders from terminal orders, or matches the wrong signal. Detail: 07 §10.

#### HAT-DEC-I11: Telegram alerts, approvals and commands
- **Statement:** Telegram carries alerts, approve/reject of entries, and the command set in 08 §3.
- **Source:** MA (I11); DS "Telegram".
- **PR:** 16
- **Verification:** CONF `telegram_command_set`
- **Acceptance:**
  - Property: the set of accepted commands equals the 08 §3 list. Harm: an undocumented command changes state outside governance. Detail: 08.

#### HAT-DEC-I12: Single user
- **Statement:** The MVP serves one user; tables still carry `user_id`.
- **Source:** MA "Your overrides" (I12/I13).
- **PR:** 2, 16
- **Verification:** CONF `user_id_on_owned_tables`
- **Acceptance:**
  - Property: every HAT row carries the one configured `HAT_USER_ID`. Harm: a second identity appears with no authorisation model. Detail: 03 §1.

#### HAT-DEC-I13: Telegram only, one chat
- **Statement:** The only control channel is one allowlisted Telegram chat; WhatsApp is out of scope.
- **Source:** MA "Your overrides" (I12/I13); DS "Telegram / Security".
- **PR:** 16
- **Verification:** PBT `telegram.unknown_chat_has_no_effect`
- **Acceptance:**
  - Property: for every inbound update whose chat ID is not the configured `HAT_TELEGRAM_CHAT_ID`, no state changes besides one `hat_command_log` row with `accepted = false`. Harm: a stranger controls the bots. Detail: 08 §1.

#### HAT-DEC-I14: Nightly data-quality gate
- **Statement:** A quality gate runs after every ingest and blocks scans when it fails.
- **Source:** MA (I14); DS "Nightly ingest" step 7.
- **PR:** 7
- **Verification:** TLA `NightlyPipeline!NoScanAfterGateFail`
- **Acceptance:**
  - Property: for every ingest result, scans for that trade date run iff every gate check passes. Harm: watchlists are built from bad data. Detail: 02 §6.

#### HAT-DEC-I15: Append-only event log
- **Statement:** Every decision is recorded in `hat_event`, which cannot be updated or deleted, in the same transaction as the state change.
- **Source:** MA (I15); SCH "Event log".
- **PR:** 3
- **Verification:** DB `events.append_only`, PBT `ingest.event_and_state_atomic`
- **Acceptance:**
  - Property: every UPDATE or DELETE on `hat_event` raises. Harm: the audit trail can be rewritten. Detail: 03 §6.

#### HAT-DEC-I16: MCP for research and chat
- **Statement:** Research MCPs (Tapetide and others) are chat-side only and never feed a scan or an order.
- **Source:** MA (I16); DS "MCP role".
- **PR:** 4
- **Verification:** CONF `mcp_endpoints_allowlist`
- **Acceptance:**
  - Property: the HAT MCP client can be configured only with the `nse-bhavcopy` and `cm-market` servers. Harm: unverified third-party data reaches a scan. Detail: 02 §3.

#### HAT-DEC-I17: US data decided in P2
- **Statement:** No US data source is integrated in the MVP.
- **Source:** MA (I17).
- **PR:** P2
- **Verification:** CONF `mvp_seed_scope`
- **Acceptance:**
  - Property: the only market row is NSE. Harm: half-built US paths run untested. Detail: 00 §6.

## §4 Portfolio decisions (PL1-PL5)

#### HAT-DEC-PL1: The unit is a sleeve
- **Statement:** The engine is strategy-agnostic and runs sleeves (one strategy version × one universe version); `hat_bot` is the sleeve.
- **Source:** MA "Portfolio-layer decisions" (PL1); BL "Portfolio layer".
- **PR:** 2, 9
- **Verification:** CONF `bot_has_sleeve_columns`
- **Acceptance:**
  - Property: every `hat_bot` row references exactly one live strategy version and one universe version. Harm: a sleeve runs against an unpinned universe and backtests cannot be reproduced. Detail: 05 §1.

#### HAT-DEC-PL2: Regime scales weights only
- **Statement:** Regime scales sleeve weights continuously within [0.25, 1.0]; it never promotes or retires a sleeve.
- **Source:** MA (PL2); BL "Allocation".
- **PR:** 9
- **Verification:** PBT `allocator.formula_and_bounds`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle`
- **Acceptance:**
  - Property: for every regime input, every multiplier is in [0.25, 1.0] (policy bounds) and no lifecycle state changes as a result. Harm: a market wobble retires a working sleeve or inflates one past its base. Detail: 05 §5-§6.

#### HAT-DEC-PL3: Regime engine v1 is rules
- **Statement:** The MVP regime engine uses rules over trend, India VIX and breadth; the ML model is P2.
- **Source:** MA (PL3); BL "Regime engine v1".
- **PR:** 9
- **Verification:** PBT `regime.classification_total`
- **Acceptance:**
  - Property: for every input triple, the v1 rules classify into exactly one of the 4 states. Harm: an unclassified day leaves sleeves on stale weights. Detail: 05 §3.

#### HAT-DEC-PL4: Lifecycle on evidence only
- **Statement:** Sleeves are promoted and retired on evidence only; every backtest run counts as a trial; promotion needs deflated Sharpe probability ≥ 0.95 [A].
- **Source:** MA (PL4); BL "Sleeve lifecycle".
- **PR:** 9, 12
- **Verification:** TLA `SleeveLifecycle!AllowedTransitions`, PBT `dsr.bounds_and_monotonicity`
- **Acceptance:**
  - Property: in every reachable state, a sleeve reaches backtested only with DSR probability ≥ 0.95 and ≥ 200 backtest trades. Harm: a lucky variant is promoted to live money. Detail: 05 §6, 09 §4.

#### HAT-DEC-PL5: Manual universe in the MVP
- **Statement:** MVP universes are manual, versioned, point-in-time lists; screener universes are P2.
- **Source:** MA (PL5); BL "Stock universes".
- **PR:** 9
- **Verification:** DB `governance.one_live_version`
- **Acceptance:**
  - Property: each universe has at most one live version. Harm: two sleeves disagree on what the universe is. Detail: 05 §2.
- **[OWNER-PENDING]** MVP universe content: NIFTY 500 point-in-time constituents plus liquidity criteria, empty include/exclude lists (PLAN Q5); owner to confirm.

## §5 Build decisions (20 Oct)

#### HAT-DEC-BD1: In-repo bounded module
- **Statement:** HAT is built as the bounded module `backend/app/hat/` inside the SmartTrader repo.
- **Source:** OWN-20OCT; PLAN "Ground rules".
- **PR:** 2
- **Verification:** CONF `hat_package_in_backend`
- **Acceptance:**
  - Property: every HAT source file lives under `backend/app/hat/` (plus the agreed hooks in existing modules named in 00 §4). Harm: HAT code spreads through SmartTrader and cannot be reviewed or extracted as one unit.

#### HAT-DEC-BD2: Direct Fyers adapter behind HAT's own broker interface
- **Statement:** Orders go through HAT's `HatBroker` interface, implemented by a direct Fyers adapter over HAT's own `FyersBroker` (circuit breaker, guarded timeout, unconfirmed-submit handling), `FyersSession` and `FyersAuthService` in `app/hat/execution/`; the legacy classes inherit from them. [Reworded in K2; it said the adapter reuses the legacy `FyersBroker`/`FyersClient`.]
- **Source:** OWN-20OCT; PLAN C5.
- **PR:** 13
- **Verification:** CONF `fyers_adapter_wraps_existing`
- **Acceptance:**
  - Property: the Fyers adapter calls the Fyers SDK only through HAT's `FyersBroker`, `FyersSession` and `FyersAuthService` (the GTT calls included), imports nothing from `app.brokers`, and never builds a second client. Harm: the circuit breaker, timeouts and unconfirmed-submit handling of the tested client are bypassed.

#### HAT-DEC-BD3: No OpenAlgo in the MVP
- **Statement:** OpenAlgo is not deployed or called in the MVP; `hat_broker_account.openalgo_ref` stays as a nullable column.
- **Source:** OWN-20OCT; PLAN C5.
- **PR:** 2, 13
- **Verification:** CONF `no_deferred_integrations`
- **Acceptance:**
  - Property: no HAT module imports or calls OpenAlgo, and every `openalgo_ref` value is null. Harm: a second secret store and process appear that nobody verified.

#### HAT-DEC-BD5: No API auth; loopback bind; guarded writes
- **Statement:** The HTTP API has no authentication. It binds to loopback (`BACKEND_HOST`, default `127.0.0.1`) or a VPN address and serves one owner. A shared secret is not possible, since a `VITE_*` value is in the public frontend bundle. The two writes (HAT-DEC-T3) must show they came from the HAT page instead:
  - The request carries the header `X-HAT-Request: 1`. A page on another site cannot send it without a CORS preflight, and CORS answers a preflight only for the frontend's origins.
  - An `Origin`, when sent, must pass the CORS frontend-origin rule (`app.utils.origins.is_frontend_origin`, handed to the guard as `app.state.frontend_origin`).
  - With no `Origin`, `Sec-Fetch-Site` must be absent, `same-origin` or `none`.

  A refusal is a 403 with one fixed message, and nothing is written. `POST /token` also saves a token only for the Fyers user that is the `account_ref` of an active Fyers broker account; before any such account exists (onboarding needs the first token), any user is accepted. Once an account exists, a login whose user cannot be read is refused (`ACCOUNT_UNCONFIRMED`; the owner logs in again), and one that cannot read the accounts answers 503. The accounts are read after Fyers answers, just before the save. A frontend served on a non-loopback address (a VPN) must have its origin in `CORS_ORIGINS`: a same-origin `Sec-Fetch-Site` is not trusted while an `Origin` is sent, since a DNS-rebinding page is same-origin too. Every frontend write sends the header from one `api.use()` middleware (`frontend/lib/api/client.ts`).
- **Source:** design review 2026-10-22 item 17 (any web page could disconnect Fyers or plant a token by CSRF); owner decision "no API auth" (CLAUDE.md).
- **PR:** 38
- **Verification:** CONF `hat_writes_guarded`, CONF `api_loopback_no_auth_wiring`, CONF `frontend_sends_write_header`, PBT `fyers_auth_routes.cross_site_write_refused`, PBT `fyers_auth_routes.token_for_another_account_refused`
- **Acceptance:**
  - Property: for every combination of the write header, `Origin` and `Sec-Fetch-Site`, a login POST runs only when the header is `1` and the request names a frontend origin, or names no origin and is not marked cross-site or same-site. Otherwise it answers 403, writes or removes no token and runs no login hook. Harm: any page the owner's browser opens logs HAT out of Fyers mid-session or plants its own token.
  - Property: for every set of broker accounts and every user a login token belongs to, `POST /token` saves the token exactly when no Fyers account is active or the user is an active one's `account_ref`, and saves nothing when the accounts cannot be read. Harm: HAT trades or reconciles on another Fyers account.
  - Property: the server binds to loopback by default, the app wires the CORS rule into the guard, every non-GET HAT route depends on the guard, and the frontend client sends the guard's header on its writes. Harm: the API listens on every interface, or a new write route ships unguarded.

#### HAT-DEC-BD4: Specs win over existing repo code
- **Statement:** Where existing repo code or old docs conflict with `docs/specs/hat/`, the specs win; the old docs were deleted in PR 1.
- **Source:** OWN-20OCT; PLAN "Ground rules".
- **PR:** 1
- **Verification:** CONF `old_docs_removed`
- **Acceptance:**
  - Property: the repo contains no file under `docs/` outside `docs/specs/hat/`, none of the two old product-requirement files at the repo root, and the acceptance grep of PR 1 (old doc paths, the old agent-guide name, the PRD file names) returns nothing. Harm: a builder follows an obsolete document over the specs.
