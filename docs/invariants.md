# Invariants

The money-safety and trading rules the code must keep. Code is the source of truth; if code and
this file disagree, one of them is wrong and a check should say which. The mechanics for touching
orders are in `.claude/skills/trading-safety/SKILL.md`. The index at the end gives every `HAT-*`
ID that code, tests and skills cite, with the checks that prove it.

## Six trading invariants (no configuration can change them)

1. A stop is never moved further from price (`current_stop` only rises).
2. No order adds to a losing position, and a bot never adds to an open position.
3. No entry without a stop price below the entry price.
4. No entry when the day's data or the live feed is stale, or the instrument's own last price is
   unknown, 30 s old or outside the day's price band.
5. No order outside the exchange session from `hat_trading_calendar` (09:15-15:30 IST).
6. `max_chase` always applies, even if a config deletes the criterion.

## Money safety

- **One path to an order.** A signal is judged by `evaluate_signal`, recorded by `reserve_order`,
  sent by `send_order`. No manual ticket, no second risk or sizing function, no override, bypass
  or force switch. `size_position` is called from `decide` only.
- **Shadow never reaches the broker.** Confirm and auto need: account active, adapter reports GTT
  delivery, DDPI confirmed by the owner, policy weights confirmed. Otherwise the bot runs shadow.
  A mode change takes effect at the next session open, never inside market hours.
- **Orders are idempotent.** Every order has an `HT` tag (signal id + purpose + sequence), committed
  as `pending` before the broker is called. An order of unknown outcome is looked up by tag before
  any retry; an unknown entry is never re-sent. Fyers does not enforce tag uniqueness, so HAT does.
- **Exact payloads and exact math.** `Decimal` for prices, notional, P&L and size; floats and
  non-finite numbers are refused. Product is CNC; a finite positive price; the stored broker symbol.
- **Risk never raises.** A missing or unreadable input is a REJECT with a typed reason; no fallback
  to a hard-coded price. Size is `floor(min(E*r/(entry-stop), E*p_max/entry, adv_cap*ADV20, cash/entry))`;
  zero skips the trade.
- **Exits are never blocked** by a cap, kill switch, data failure, reconciliation state or mode.
  Kill switches, pause and a failed quality gate stop new entries only (manage-only).
- **Fail closed on the broker.** A missing client, open circuit or failed read raises
  `BrokerUnavailableError`, never an empty book. A write with unknown outcome is `unconfirmed`.
  No entry on a trade date before that date's first reconciliation succeeded; a mismatch blocks
  entries and never auto-closes or auto-adopts a position.
- **Every real position has a resting broker stop** (intraday stop, then GTT overnight), re-verified
  nightly; a lost broker answer never freezes a position and is settled against the broker's book.
- **Kill switches are HAT's own** (account and bot drawdown, health, manual, pause). A drawdown or
  manual kill re-arms by hand with a second confirmation; a health kill clears itself.
- **Approvals.** Tokens are signed, bound to signal and expiry, single-use, stored only as hashes;
  approval expires after 5 minutes; Take re-checks `max_chase`; unknown chats are ignored and logged.
  Free text never produces an order; auto, `/arm` and `/flatten` need a second confirmation.
- **Policy changes.** Live levers only reduce risk; an increase waits 7 days and passes the
  evidence gate; decreases apply next session; policy and strategy versions are immutable.
- **One owner per resource.** One live socket, one session owner per broker account, one token
  store, one fake broker, one risk authority. HAT code imports no legacy or manual-trading module.
- **Nothing leaks.** Results carry fixed codes, never broker or database text; tokens and secrets
  are never logged or returned; no secret value in the repo.
- **Events.** Every state change and its event commit in one transaction; `hat_event` is
  append-only (trigger); trade state replays from events.
- **Data.** Scans never run on data that failed the gate; features use bars up to the scan date
  only; raw bars are immutable; adjusted prices are raw times cumulative factors.

## Requirement index

One line per ID: `- ID: title | checks`. Methods: TLA (model!property in `verification/tla/`), PBT
(`area.property` in `backend/tests/hat/properties/test_<area>.py`), DB (Postgres check in
`backend/tests/hat/db/`), CONF (`backend/tests/hat/conformance/`), OPS (procedure in
`runbook.md`). `PLANNED` marks an item not built. `test_conf_verification.py` parses this index:
every check named must exist, and a new check is added here with its ID.

### Overview and boundaries

- HAT-OVR-001: Market rules stay out of the engine | CONF `market_id_on_market_tables`, PBT `calendar.session_from_reference_data`
- HAT-OVR-002: MVP scope is two sleeves, one user, one broker | CONF `mvp_seed_scope`
- HAT-OVR-003: Criteria import no broker, DB session or UI module | CONF `import_graph_boundaries`
- HAT-OVR-004: Only the router reaches the broker | CONF `import_graph_boundaries`, TLA `ExecutionRouter!RiskBeforeRoute`
- HAT-OVR-005: HAT owns only `hat_` tables | CONF `hat_tables_prefixed_and_isolated`
- HAT-OVR-006: One live feed | CONF `single_feed_import_rule`
- HAT-OVR-007: HAT is off unless enabled | TLA `JobScheduler!NoJobWhenDisabled`, CONF `hat_enabled_default_false`
- HAT-OVR-040: One fake broker and one risk authority | CONF `no_duplicate_fakes_or_authorities`
- HAT-OVR-041: HAT stands alone from the legacy code | CONF `imports.hat_decoupled_from_legacy`, CONF `imports.hat_suites_boot_no_legacy_app`, CONF `imports.legacy_does_not_import_hat`
- HAT-OVR-051: Existing SmartTrader paths are unchanged except where a resolution says so | CONF `existing_suite_green`
- HAT-OVR-052: HAT data lives under `HAT_DATA_ROOT` | CONF `hat_data_root_paths`
- HAT-OVR-060: Deferred items are absent from MVP code paths | TLA `ExecutionRouter!LongOnly`, CONF `no_deferred_integrations`
- HAT-OVR-061: Auto mode is personal-account only | CONF `auto_mode_single_user`

### Decisions

- HAT-DEC-T1: Multi-tenant-ready schema | CONF `user_id_on_owned_tables`
- HAT-DEC-T2: Python core | CONF `hat_package_in_backend`
- HAT-DEC-T3: Web dashboard plus Telegram | CONF `hat_routes_read_only`, CONF `hat_writes_guarded`
- HAT-DEC-T4: Extend SmartTrader | CONF `existing_suite_green`
- HAT-DEC-T5: Postgres | CONF `hat_runtime_requires_postgres`
- HAT-DEC-T6: Parquet plus DuckDB for price history | PBT `ingest.parquet_roundtrip`
- HAT-DEC-T7: In-process scheduler, jobs in Postgres | TLA `JobScheduler!AtMostOnceSuccess`
- HAT-DEC-T8: One async service per broker session | TLA `OrderReconciliation!OneSessionOwner`, PBT `router.desk_calls_serialised`
- HAT-DEC-T9: Self-hosted machine | OPS `host_static_ip_ups_failover`
- HAT-DEC-T10: Criteria versioned in the DB, YAML import and export | PBT `config.yaml_round_trip`, PBT `config.hash_canonical`
- HAT-DEC-T11: Own polars indicators | PBT `features.no_lookahead`
- HAT-DEC-T12: One engine for backtest and live | PBT `backtest.scan_parity`
- HAT-DEC-T13: Secrets in `.env` | CONF `secrets_not_in_repo`, OPS `env_file_permissions`
- HAT-DEC-T14: Manual morning login via Telegram link | TLA `ExecutionRouter!NoEntryWithoutSession`
- HAT-DEC-T15: Structured logs plus Telegram alerts | PBT `telegram.health_alert_for_every_failure`
- HAT-DEC-T16: Verification instead of unit and replay tests | CONF `spec_requirements_well_formed`
- HAT-DEC-B1: Three execution modes (revised) | TLA `ExecutionRouter!ModeDomain`
- HAT-DEC-B2: Default mode confirm (revised, D130/D132) | CONF `seed_bots_confirm`
- HAT-DEC-B3: Criteria library with parameters | CONF `criterion_catalogue_complete`
- HAT-DEC-B4: Pass/fail plus weighted score | PBT `scoring.threshold_gate`
- HAT-DEC-B5: Versioned configs | DB `governance.version_config_immutable`
- HAT-DEC-B6: Earnings filter optional, off | CONF `bot_seed_s1_parses`
- HAT-DEC-B7: Scored VCP detector | PBT `criteria.vcp_contract`
- HAT-DEC-B8: Intraday pivot break plus volume pace | PBT `criteria.trigger_conjunction`
- HAT-DEC-B9: 5-minute opening range | PBT `bars.opening_range_fixed`
- HAT-DEC-B10: NSE announcement attached to EPs | PBT `announcements.match_window`
- HAT-DEC-B11: Regime gate blocks new entries | TLA `ExecutionRouter!GateOffNoEntry`
- HAT-DEC-B12: No regime override (revised) | TLA `ExecutionRouter!GateOffNoEntry`, CONF `no_override_command`
- HAT-DEC-B13: Fixed percentage risk | PBT `sizing.risk_bounded`
- HAT-DEC-B14: Default risk per trade 0.25% | CONF `initial_risk_policy_seed`
- HAT-DEC-B15: Per-bot capital plus global open-risk cap | PBT `caps.global_open_risk`
- HAT-DEC-B16: Progressive exposure | PBT `exposure.ladder_transitions`
- HAT-DEC-B17: GTT overnight, stop orders intraday | TLA `StopPlacement!AlwaysProtected`, PBT `manage.real_fill_protected`
- HAT-DEC-B18: MA exits around 15:25 | TLA `JobScheduler!ExitsCloseInWindow`
- HAT-DEC-B19: Stackable exit rules | PBT `exits.full_beats_partial`
- HAT-DEC-B20: No adding to winners | TLA `ExecutionRouter!NoAddToOpenPosition`
- HAT-DEC-B21: Max 6 positions per bot | PBT `caps.max_positions`
- HAT-DEC-B22: All NSE EQ above ₹10 cr turnover | PBT `criteria.min_turnover_contract`
- HAT-DEC-B23: Include and exclude lists | PBT `universe.include_exclude`
- HAT-DEC-B24: Delivery only | TLA `OrderReconciliation!ProductIsCNC`
- HAT-DEC-B25: Skip at upper circuit | PBT `criteria.max_chase_contract`
- HAT-DEC-B26: Kill switches 10% / 10% | TLA `KillSwitch!TripOnDrawdown`, PBT `caps.kill_thresholds`
- HAT-DEC-B27: Auto-adjust for splits and bonuses | PBT `corporate_actions.adjusted_view`
- HAT-DEC-I1: NSE MCP for nightly prices | TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.batches_of_50`
- HAT-DEC-I2: History from NSE bhavcopy archives | PBT `bhavcopy.parsers_agree`
- HAT-DEC-I3: Fyers WebSocket as the single live source | CONF `single_feed_import_rule`
- HAT-DEC-I4: Broker minute history for EP backtests | PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_plan_clamp`
- HAT-DEC-I5: Fundamentals later via XBRL | PBT `criteria.fundamentals_point_in_time`
- HAT-DEC-I6: NSE announcements RSS | PBT `announcements.dedupe`
- HAT-DEC-I7: Own broker interface (direct Fyers adapter) | CONF `broker_interface_methods`
- HAT-DEC-I8: Brokers Fyers first | CONF `mvp_seed_scope`
- HAT-DEC-I9: In-app broker onboarding | TLA `ExecutionRouter!AutoNeedsGtt`
- HAT-DEC-I10: Every order tagged with bot and signal | PBT `tags.encode_decode_roundtrip`, DB `runtime.order_tag_unique`
- HAT-DEC-I11: Telegram alerts, approvals and commands | CONF `telegram_command_set`
- HAT-DEC-I12: Single user | CONF `user_id_on_owned_tables`
- HAT-DEC-I13: Telegram only, one chat | PBT `telegram.unknown_chat_has_no_effect`
- HAT-DEC-I14: Nightly data-quality gate | TLA `NightlyPipeline!NoScanAfterGateFail`
- HAT-DEC-I15: Append-only event log | DB `events.append_only`, PBT `ingest.event_and_state_atomic`
- HAT-DEC-I16: MCP for research and chat | CONF `mcp_endpoints_allowlist`
- HAT-DEC-I17: US data decided in P2 | CONF `mvp_seed_scope`
- HAT-DEC-PL1: The unit is a sleeve | CONF `bot_has_sleeve_columns`
- HAT-DEC-PL2: Regime scales weights only | PBT `allocator.formula_and_bounds`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle`
- HAT-DEC-PL3: Regime engine v1 is rules | PBT `regime.classification_total`
- HAT-DEC-PL4: Lifecycle on evidence only | TLA `SleeveLifecycle!AllowedTransitions`, PBT `dsr.bounds_and_monotonicity`
- HAT-DEC-PL5: Manual universe in the MVP | DB `governance.one_live_version`
- HAT-DEC-BD1: In-repo bounded module | CONF `hat_package_in_backend`
- HAT-DEC-BD2: Direct Fyers adapter behind HAT's own broker interface | CONF `fyers_adapter_wraps_existing`
- HAT-DEC-BD3: No OpenAlgo in the MVP | CONF `no_deferred_integrations`
- HAT-DEC-BD5: No API auth; loopback bind; guarded writes | CONF `hat_writes_guarded`, CONF `api_loopback_no_auth_wiring`, CONF `frontend_sends_write_header`, PBT `fyers_auth_routes.cross_site_write_refused`, PBT `fyers_auth_routes.token_for_another_account_refused`, PBT `fyers_auth.accounts_read_in_order`, PBT `fyers_auth.complete_login_checks_the_account`
- HAT-DEC-BD4: Specs win over existing repo code | CONF `old_docs_removed`

### Portfolio

- HAT-PORT-010: A sleeve is a bot pinning one strategy version and one universe version | CONF `bot_sleeve_columns`, PBT `allocator.sleeve_count_agnostic`
- HAT-PORT-011: S1 and S2 form one risk cluster with a combined cap | PBT `allocator.cluster_cap`
- HAT-PORT-020: Universe versions are immutable with one live version | DB `governance.universe_version_immutable`, DB `governance.one_live_version`
- HAT-PORT-021: Membership is point-in-time | PBT `universe.point_in_time_members`
- HAT-PORT-022: MVP universe content | PBT `universe.include_exclude`
- HAT-PORT-023: Leaving the universe stops entries but never force-sells | PBT `universe.leave_no_force_sell`, PBT `scan.no_row_for_removed`
- HAT-PORT-030: State changes only after 3 consecutive days | TLA `RegimeConfirmation!ThreeDayRule`, PBT `regime.three_day_confirmation`
- HAT-PORT-031: Regime inputs are computed exactly as defined | PBT `regime.inputs`
- HAT-PORT-032: Classification is total and deterministic | PBT `regime.classification_total`
- HAT-PORT-033: Momentum sleeve multipliers per state | PBT `regime.multiplier_table`
- HAT-PORT-034: Regime output is stored daily and never touches lifecycle | DB `portfolio.unique_keys`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle`
- HAT-PORT-040: Health multiplier from sleeve drawdown bands | PBT `health.bands`, TLA `SleeveLifecycle!ProbationOnHealthZero`
- HAT-PORT-050: Weight formula and policy bounds | PBT `allocator.formula_and_bounds`, PBT `allocator.virtual_allocation`, PBT `allocator.virtual_entries_sized`
- HAT-PORT-051: Multipliers above 1 only through a policy increase | PBT `allocator.bounds_from_policy`, DB `governance.risk_increase_cooling_off`
- HAT-PORT-052: Multipliers scale both capital and risk per trade | PBT `allocator.capital_and_risk`
- HAT-PORT-053: Recomputed nightly, applied to new entries only | TLA `DailyCycle!AllocateAfterRegime`, PBT `allocator.no_effect_on_open_positions`
- HAT-PORT-054: Smoothing caps the daily multiplier move at 0.25 | PBT `allocator.smoothing`
- HAT-PORT-055: Unallocated weight stays cash | PBT `allocator.cash_remainder`
- HAT-PORT-056: One audited allocation row per sleeve and day | DB `portfolio.unique_keys`, TLA `JobScheduler!AtMostOnceSuccess`
- HAT-PORT-060: Lifecycle transitions follow the table and its guards | TLA `SleeveLifecycle!AllowedTransitions`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`, PBT `admin.first_deploy`, PBT `admin.shadow_beside_live`
- HAT-PORT-061: Backtested requires DSR probability ≥ 0.95, ≥ 200 trades, a five-year span, no gap in an enabled playbook | PBT `lifecycle.backtest_gate`, PBT `lifecycle.backtest_refusal`, PBT `dsr.evidence_gate_facts`, TLA `SleeveLifecycle!AllowedTransitions`
- HAT-PORT-062: Shadow lasts 20 trading days before Live | TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.shadow_days`
- HAT-PORT-063: Probation trigger and effect | PBT `lifecycle.probation_trigger`, TLA `SleeveLifecycle!ProbationOnHealthZero`, PBT `router.non_live_sleeve_routes_as_shadow`
- HAT-PORT-064: Retirement and re-entry | TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.retire_gate`
- HAT-PORT-065: Live results are compared against a discounted backtest | PBT `lifecycle.discounted_benchmark`
- HAT-PORT-066: Lifecycle, regime and allocation changes are events | CONF `lifecycle_single_writer`, PBT `lifecycle.transition_records_its_event`
- HAT-PORT-070: Model family and features | PLANNED PBT `regime_v2.no_lookahead`
- HAT-PORT-071: Training, refit and relabelling | PLANNED PBT `regime_v2.relabel_stable`
- HAT-PORT-072: Output, bounds and smoothing | PLANNED PBT `regime_v2.multiplier_convex`
- HAT-PORT-073: Acceptance gate and fallback | PLANNED TLA `SleeveLifecycle!NoLiveWithoutEvidence`, PBT `regime_v2.fallback_to_v1`

### Criteria

- HAT-CRIT-010: Stages evaluate in the fixed order 1 to 9 | PBT `scoring.stage_order`, PBT `scan.watchlist_gates`, PBT `scan.no_universe_publishes_nothing`, DB `runtime.publish_serialised`
- HAT-CRIT-011: Only stages 1, 3, 4 and 5 hold library criteria | PBT `config.stage_slots`
- HAT-CRIT-020: A criterion instance carries exactly the seven documented fields | PBT `config.instance_fields`, CONF `criterion_instance_schema`
- HAT-CRIT-021: Params are validated against the type schema; invalid configs never save | PBT `config.invalid_never_saves`, CONF `criterion_type_registry`
- HAT-CRIT-022: `enabled` and `order` control evaluation without deleting | PBT `scoring.disabled_is_noop`
- HAT-CRIT-030: Setup score is the weighted mean of scored criteria | PBT `scoring.weighted_mean`
- HAT-CRIT-031: Default setup threshold 0.6 | PBT `scoring.threshold_gate`
- HAT-CRIT-032: Hard criteria short-circuit and store the first failure reason | PBT `scoring.hard_short_circuit`
- HAT-CRIT-040: `vcp` scored volatility-contraction detector | PBT `criteria.vcp_contract`, PBT `criteria.vcp_parity`, PBT `criteria.vcp_structure_narrows`, PBT `criteria.vcp_base_high`
- HAT-CRIT-100: `series_in` | PBT `criteria.series_in_contract`
- HAT-CRIT-101: `min_turnover` | PBT `criteria.min_turnover_contract`
- HAT-CRIT-102: `min_price` | PBT `criteria.min_price_contract`
- HAT-CRIT-103: `price_band_allowed` | PBT `criteria.price_band_allowed_contract`
- HAT-CRIT-104: `adr_min` | PBT `criteria.adr_min_contract`
- HAT-CRIT-105: `sma_above` | PBT `criteria.sma_above_contract`
- HAT-CRIT-106: `sma_rising` | PBT `criteria.sma_rising_contract`
- HAT-CRIT-107: `pct_from_52w_low` | PBT `criteria.pct_from_52w_low_contract`
- HAT-CRIT-108: `pct_from_52w_high` | PBT `criteria.pct_from_52w_high_contract`
- HAT-CRIT-109: `rs_rank` | PBT `criteria.rs_rank_contract`
- HAT-CRIT-110: `momentum_rank` | PBT `criteria.momentum_rank_contract`, PBT `scan.rank_within_members`
- HAT-CRIT-111: `consolidation` | PBT `criteria.consolidation_contract`
- HAT-CRIT-112: `ma_surf` | PBT `criteria.ma_surf_contract`
- HAT-CRIT-113: `prior_move` | PBT `criteria.prior_move_contract`
- HAT-CRIT-114: `earnings_growth` registered, disabled by default | PBT `criteria.earnings_growth_contract`, CONF `bot_seed_s1_parses`
- HAT-CRIT-115: `gap_up` | PBT `criteria.gap_up_contract`
- HAT-CRIT-116: `volume_pace` | PBT `criteria.volume_pace_contract`
- HAT-CRIT-117: `early_volume` | PBT `criteria.early_volume_contract`
- HAT-CRIT-118: `catalyst_attached` | PBT `criteria.catalyst_attached_contract`
- HAT-CRIT-119: `price_breaks_level` | PBT `criteria.price_breaks_level_contract`
- HAT-CRIT-120: `neglect` | PBT `criteria.neglect_contract`
- HAT-CRIT-121: `adtr_filter` | PBT `criteria.adtr_filter_contract`
- HAT-CRIT-122: `max_chase` always applies | PBT `criteria.max_chase_contract`, TLA `ExecutionRouter!MaxChaseAlwaysApplied`
- HAT-CRIT-123: `close_vs_pivot` refuses a base that already broke out | PBT `criteria.close_vs_pivot_contract`, PBT `criteria.close_vs_pivot_stage`, PBT `scoring.stage_levels`, PBT `scoring.numeric_levels`, PBT `config.close_vs_pivot_needs_vcp`

### Bots

- HAT-BOT-050: A playbook bundles setup, trigger, entry filters and stop | PBT `config.playbook_shape`, PBT `trades.shared_bot_cap`
- HAT-BOT-051: One signal per bot, playbook, instrument and day | DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- HAT-BOT-052: Episodic-pivot setup is built live from the pre-open scan | PBT `criteria.ep_candidate_bands`
- HAT-BOT-053: Strategy regime rules are evaluated as configured | PBT `regime.strategy_rules`
- HAT-BOT-060: S1 seed parses and keeps earnings_growth off | CONF `bot_seed_s1_parses`
- HAT-BOT-061: New bots ask for confirm mode and run shadow until Live | CONF `bot_seed_default_mode`, PBT `config.new_bot_mode_confirm`
- HAT-BOT-062: S1 skips trades whose stop is more than 8% below entry | PBT `stops.final_contraction_low`
- HAT-BOT-070: S2 seed parses and hashes stably | CONF `bot_seed_s2_parses`
- HAT-BOT-071: The breakout level expression resolves to the larger level | PBT `criteria.level_expression`
- HAT-BOT-072: Strategy configs hold no risk numbers | PBT `config.no_risk_keys`, CONF `risk_policy_seed_bot_limits`
- HAT-BOT-080: Every save creates a new version; published versions are immutable | PBT `config.save_creates_version`, DB `governance.version_config_immutable`
- HAT-BOT-081: `config_hash` is the sha256 of canonical JSON | PBT `config.hash_canonical`
- HAT-BOT-082: YAML import and export round-trip losslessly | PBT `config.yaml_round_trip`
- HAT-BOT-083: Signals and trades record the version they ran under | PBT `config.trade_keeps_entry_version`, TLA `TradeLifecycle!VersionPinned`
- HAT-BOT-084: Strategy edits happen outside market hours as drafts through the release gate | TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`, PBT `config.release_gate`, PBT `admin.shadow_beside_live`

### Schema

- HAT-SCH-010: Every HAT table is prefixed and self-contained | CONF `schema.prefix_and_boundary`
- HAT-SCH-011: Keys are uuid v7 generated in Python | PBT `ids.uuid7_ordered`, CONF `schema.key_types`
- HAT-SCH-012: Tenancy | CONF `schema.user_id_columns`
- HAT-SCH-013: Market scoping | CONF `schema.market_id_columns`
- HAT-SCH-014: Money is exact | CONF `schema.money_types`
- HAT-SCH-015: Time is UTC-aware; trade dates are exchange-local | CONF `schema.time_types`, PBT `time.trade_date_ist`
- HAT-SCH-016: Configs are validated JSON with a canonical hash | PBT `config.hash_canonical`
- HAT-SCH-017: Virtual and real rows share tables | PBT `runtime.virtual_consistency`
- HAT-SCH-021: NSE market row is seeded | CONF `seed.market_nse`
- HAT-SCH-022: Reference uniqueness is enforced by the database | DB `reference.instrument_unique`, DB `reference.broker_symbol_unique`, DB `reference.corporate_action_unique`, DB `reference.announcement_guid_unique`
- HAT-SCH-023: Price band values are restricted | DB `reference.band_pct_check`
- HAT-SCH-031: Status and mode domains are enforced | DB `governance.domain_checks`
- HAT-SCH-032: Bots and strategy versions hold no risk numbers | CONF `schema.bot_no_risk_columns`, PBT `config.no_risk_keys`
- HAT-SCH-033: Kill-switch rows are well formed | DB `governance.kill_switch_scope_check`, PBT `kill.current_state`
- HAT-SCH-034: Exposure counters are non-negative | DB `governance.exposure_checks`
- HAT-SCH-041: Portfolio uniqueness and domains | DB `portfolio.unique_keys`, DB `portfolio.domain_checks`
- HAT-SCH-042: Universe membership is point-in-time without overlaps | PBT `universe.membership_intervals`
- HAT-SCH-043: Fundamentals are read by filing date | PLANNED PBT `universe.fundamentals_point_in_time`
- HAT-SCH-051: One signal per bot, playbook, instrument and day | DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- HAT-SCH-052: Every signal names its versions | DB `runtime.signal_versions_not_null`
- HAT-SCH-053: Order tags are unique and well formed | DB `runtime.order_tag_unique`, PBT `tags.format`
- HAT-SCH-054: Order purpose and approval decision domains | DB `runtime.domain_checks`
- HAT-SCH-055: Fills are unique per broker trade | DB `runtime.fill_unique`
- HAT-SCH-056: Approval tokens are stored only as hashes | PBT `telegram.token_never_stored`
- HAT-SCH-057: A trade result is written once per position | DB `runtime.trade_result_once`, TLA `TradeLifecycle!ResultOnClose`
- HAT-SCH-058: Order status values | DB `runtime.order_status_check`, TLA `OrderReconciliation!PersistBeforeCall`
- HAT-SCH-059: Trade states are the eleven lifecycle states | DB `runtime.state_domain`
- HAT-SCH-061: The event log is append-only | DB `events.append_only`
- HAT-SCH-062: State changes and their events commit together | PBT `events.atomic_with_state`, PBT `journal.validates_before_writing`
- HAT-SCH-063: Event types are exactly the MVP catalogue | PBT `events.unknown_type_rejected`
- HAT-SCH-064: Aggregate types and correlation | PBT `events.correlation`, PBT `events.correlation_links_the_signal`
- HAT-SCH-065: User actions are attributable | PBT `events.user_actor_chat_id`
- HAT-SCH-066: Monthly partitions exist ahead of time | DB `events.partitions_ahead`, DB `events.partitions_job`
- HAT-SCH-067: Event ids increase on SQLite too | PBT `events.ids_increasing`
- HAT-SCH-068: App-role revoke is deferred | DB `events.append_only`
- HAT-SCH-071: Every backtest run belongs to a family | DB `ops.family_key_not_null`
- HAT-SCH-072: Jobs are unique per trade date | DB `ops.job_run_unique`, TLA `JobScheduler!AtMostOnceSuccess`
- HAT-SCH-073: Daily bot snapshots are unique per day | DB `ops.snapshot_unique`
- HAT-SCH-074: Unknown-chat commands are logged | PBT `telegram.every_command_logged`
- HAT-SCH-080: Published strategy versions are immutable | DB `governance.version_config_immutable`
- HAT-SCH-081: `current_stop` can only rise | DB `governance.stop_only_rises`, DB `governance.rescale_moves_stop`, TLA `StopPlacement!StopOnlyTightens`
- HAT-SCH-082: One live version per strategy and per universe | DB `governance.one_live_version`
- HAT-SCH-083: No live version without evidence | DB `governance.live_requires_evidence`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- HAT-SCH-084: Risk increases wait 7 days | DB `governance.risk_increase_cooling_off`, PBT `risk_policy.cooling_off`
- HAT-SCH-085: Risk-policy versions are immutable except `superseded_at` | DB `governance.risk_version_immutable`
- HAT-SCH-086: Mode changes take effect at the next session open | PBT `calendar.next_session_open`, TLA `ExecutionRouter!ModeChangeNextSession`
- HAT-SCH-087: Universe versions are immutable once published | DB `governance.universe_version_immutable`
- HAT-SCH-091: Retention purge touches only Telegram messages and the command log | PBT `retention.purge_scope`
- HAT-SCH-100: HAT models live on the shared Base and create on SQLite | CONF `schema.portable_types`, CONF `schema.sqlite_create_all`
- HAT-SCH-101: `ensure_hat_schema` adds the Postgres-only DDL idempotently | DB `migration.ensure_idempotent`
- HAT-SCH-102: Postgres-only rules have postgres-marked checks | CONF `verification.db_rules_covered`
- HAT-SCH-103: Later column additions follow the ensure pattern | DB `migration.ensure_idempotent`
- HAT-SCH-104: FK targets exist before their referrers | CONF `schema.sqlite_create_all`
- HAT-SCH-105: The HAT runtime refuses to start on SQLite | CONF `hat_runtime_requires_postgres`
- HAT-SCH-106: HAT migrations never alter SmartTrader tables | CONF `schema.prefix_and_boundary`

### Data

- HAT-DATA-010: Engine data comes only from the source map | CONF `imports.engine_data_sources`
- HAT-DATA-011: Bhavcopy is a drop-in replacement for the MCP price pull | PBT `ingest.sources_same_schema`
- HAT-DATA-020: Instrument identity is (market, ISIN, series) | PBT `reference.instrument_identity_stable`, DB `reference.instrument_unique`
- HAT-DATA-021: Delisted instruments are kept | PBT `reference.delisted_never_deleted`
- HAT-DATA-022: Symbol renames are recorded with non-overlapping validity | PBT `reference.symbol_history_resolves`
- HAT-DATA-023: Weekly equity list refresh is complete | PBT `reference.prefix_paging_complete`
- HAT-DATA-024: Fyers broker symbols round-trip through symbol_master | PBT `reference.fyers_symbol_roundtrip`, DB `reference.broker_symbol_unique`
- HAT-DATA-025: Price band is the latest row on or before the date | PBT `reference.current_band`
- HAT-DATA-026: Industry classification feeds the industry cap | PBT `reference.current_industry`
- HAT-DATA-027: Trading calendar drives market hours | PBT `calendar.holiday_closed`, PBT `calendar.weekday_rows`
- HAT-DATA-030: Thin MCP client behind a transport protocol | CONF `imports.mcp_transport_only`, PBT `mcp.session_handshake`
- HAT-DATA-031: Bulk quotes are fetched in batches of at most 50 | PBT `ingest.batches_le_50`
- HAT-DATA-032: At most 4 MCP calls in flight | PBT `ingest.concurrency_bounded`
- HAT-DATA-033: Retries are bounded with backoff | PBT `mcp.retry_bounded`
- HAT-DATA-034: Every MCP call is logged | PBT `mcp.call_logged`
- HAT-DATA-035: MCP errors never leak raw text | PBT `mcp.no_raw_error_text`
- HAT-DATA-036: A date mismatch is reported, never stored as the trade date | PBT `ingest.date_mismatch_rejected`
- HAT-DATA-040: Probe until the data date matches, then fall back at 22:00 | TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.probe_schedule`
- HAT-DATA-041: Fetch covers the active EQ/BE universe | PBT `ingest.fetch_covers_universe`
- HAT-DATA-042: Row checks | PBT `ingest.row_checks`
- HAT-DATA-043: Raw bars are written once per date | PBT `ingest.raw_write_once`, TLA `JobScheduler!AtMostOnceSuccess`, PBT `ingest.checks_kept_for_retry`
- HAT-DATA-044: Index snapshot and breadth are stored nightly | PBT `ingest.index_snapshot_stored`, PBT `ingest.nightly_index_breadth`
- HAT-DATA-045: Each ingest run ends with exactly one outcome event | PBT `ingest.outcome_event`
- HAT-DATA-046: Nightly steps run in order, each after the previous succeeds | TLA `NightlyPipeline!NightlyPipelineOrder`
- HAT-DATA-047: No data by the cutoff means no scans and manage-only | TLA `NightlyPipeline!NoDataNoScan`, TLA `KillSwitch!ExitsContinue`
- HAT-DATA-048: MCP call budget per night | PBT `ingest.call_budget`
- HAT-DATA-049: Symbol renames are rebuilt from the stored bars | PBT `ingest.renames_from_nse_data`
- HAT-DATA-050: Corporate-action flagging rule | PBT `corporate_actions.flag_rule`
- HAT-DATA-051: Corporate actions are fetched only for flagged symbols | PBT `corporate_actions.lookup_only_flagged`
- HAT-DATA-052: Corporate actions are stored idempotently | PBT `corporate_actions.upsert_idempotent`, DB `reference.corporate_action_unique`, PBT `rescale.price_neutral_changes_nothing`
- HAT-DATA-053: Adjusted prices are raw prices times cumulative factors | PBT `corporate_actions.adjusted_view`
- HAT-DATA-054: Adjusted volume | PBT `corporate_actions.adjusted_volume`
- HAT-DATA-055: Split and bonus factors from text sources | PBT `corporate_actions.factor_parse`, PBT `history_build.archive_unparsed_reported`
- HAT-DATA-060: Gate fails when coverage < 99% of active universe | PBT `quality_gate.coverage_threshold`, TLA `KillSwitch!DataHealthManageOnly`
- HAT-DATA-061: Gate fails on any OHLC violation | PBT `quality_gate.ohlc_zero_tolerance`
- HAT-DATA-062: Every move over 20% is explained or excluded | PBT `quality_gate.big_moves_explained`, PBT `quality_gate.quarantine_one_mover`, PBT `quality_gate.quarantine_cap`, PBT `quality_gate.quarantine_release`, PBT `quality_gate.quarantine_lookup`
- HAT-DATA-063: Gate fails without the index snapshot | PBT `quality_gate.index_snapshot_present`
- HAT-DATA-064: Gate results are recorded per check | PBT `quality_gate.rows_written`
- HAT-DATA-065: A failed gate blocks scans but never exits | TLA `KillSwitch!DataHealthManageOnly`, TLA `KillSwitch!ExitsContinue`, TLA `NightlyPipeline!NoScanAfterGateFail`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `quality_gate.rows_written`
- HAT-DATA-070: Feature dataset has exactly the specified columns | CONF `features.schema`
- HAT-DATA-071: Features use bars up to and including d only | PBT `features.no_lookahead`
- HAT-DATA-072: Feature formulas | PBT `features.formulas`
- HAT-DATA-073: Features are invariant under corporate actions | PBT `features.split_invariant`
- HAT-DATA-074: RS percentile weights and monotonicity | PBT `features.rs_pct`
- HAT-DATA-075: Momentum rank | PBT `features.mom_rank`
- HAT-DATA-076: Breadth inputs for the regime | PBT `features.breadth`
- HAT-DATA-077: Incremental features equal a full rebuild | PBT `features.incremental_equals_full`, PBT `backtest.features_digest_pinned`
- HAT-DATA-078: HAT data code uses polars, pandas only at boundaries | CONF `imports.hat_polars_only`
- HAT-DATA-080: HAT data root is configurable and outside git | CONF `store.data_root`, CONF `store.cli_root_after_env`, PBT `history_build.cli_root_and_report`, PBT `minute_history_cli.root_and_report`
- HAT-DATA-081: Dataset layout | CONF `store.layout`
- HAT-DATA-082: Writes are atomic | PBT `store.atomic_write`
- HAT-DATA-083: Raw layers are immutable | PBT `store.raw_immutable`
- HAT-DATA-084: bars_daily has one row per instrument and date | PBT `store.bars_daily_unique`
- HAT-DATA-085: Backtest and live read the same dataset | PBT `ingest.sources_same_schema`
- HAT-DATA-090: File format is chosen by date | PBT `bhavcopy.format_by_date`
- HAT-DATA-091: Day list and holiday candidates | PBT `bhavcopy.missing_file_is_holiday_candidate`, PBT `history_build.cli_passes_calendar_holidays`
- HAT-DATA-092: Download is polite, resumable and keeps raw zips | PBT `bhavcopy.resume_skips_done`, PBT `bhavcopy.rate_limited`
- HAT-DATA-093: Both formats normalise to one schema keyed by ISIN + series | PBT `bhavcopy.parsers_agree`
- HAT-DATA-094: Corporate actions back to 2010 | PBT `corporate_actions.upsert_idempotent`
- HAT-DATA-095: Index history | PBT `ingest.index_snapshot_stored`, PBT `history_build.mcp_index_source_ends_at_depth`, PBT `history_build.archive_fills_before_mcp_depth`, PBT `history_build.index_failure_isolated`, CONF `fixtures.index_history_no_data`
- HAT-DATA-096: Validation report | PBT `bhavcopy.validation_flags_mismatch`, PBT `history_build.cli_root_and_report`
- HAT-DATA-097: Bhavcopy traded value is kept | PBT `bhavcopy.traded_value_kept`
- HAT-DATA-100: Poll cadence | PBT `announcements.poll_schedule`
- HAT-DATA-101: Items are deduplicated by GUID | PBT `announcements.dedupe`, DB `reference.announcement_guid_unique`
- HAT-DATA-102: Stored fields and unmatched items | PBT `announcements.unmatched_kept`
- HAT-DATA-103: Match rule | PBT `announcements.match_window`
- HAT-DATA-104: Announcements are never parsed for meaning | PBT `announcements.text_never_gates`
- HAT-DATA-110: One live feed, owned by HAT | CONF `imports.no_second_websocket`
- HAT-DATA-111: A tick listener can never block or break the broadcast | PBT `bars.listener_isolation`
- HAT-DATA-112: HAT subscriptions are one registry owner | PBT `bars.subscription_set`, PBT `ops.subscription_limit_alerts`, CONF `ops.subscription_alert_wired`
- HAT-DATA-113: 5-minute bars are aligned to 09:15 and final at close + 2 s | PBT `bars.alignment`
- HAT-DATA-114: Opening range is fixed when the first bar closes | PBT `bars.opening_range_fixed`, PBT `bars.restore_matches_live`, PBT `bars.session_state_never_partial`, PBT `bars.trigger_never_uses_a_partial_session`, PBT `bars.seed_needs_ordered_prices`, PBT `bars.delayed_seed_spoiled_by_gap`, PBT `bars.live_flags_parsed`
- HAT-DATA-115: Volume pace | PBT `bars.volume_pace`
- HAT-DATA-116: Feed silence of 30 seconds or more marks the feed stale | PBT `bars.staleness_trip`, PBT `bars.stale_feed_trips_health_kill`, TLA `ExecutionRouter!NoOrderWhenStale`, PBT `bars.usable_price`, PBT `bars.price_book_dates_each_tick`, PBT `bars.exits_and_placement_read_the_checked_price`, PBT `bars.symbol_silence_resubscribes_and_alerts`
- HAT-DATA-117: 5-minute bars persist to Parquet | PBT `bars.persisted_equals_live`
- HAT-DATA-118: Pre-open gap capture | PBT `bars.gap_pct`, PBT `bars.gap_record_from_its_own_day`
- HAT-DATA-119: Optional Fyers minute history for EP backtests | PBT `bars.history_matches_builder`, PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_budget`, PBT `bars.minute_history_resume`, PBT `bars.minute_history_plan_clamp`, PBT `minute_history_cli.root_and_report`
- HAT-DATA-120: Full-market gap scan at 09:16 | PBT `gapscan.covers_universe`
- HAT-DATA-121: Gap-scan selection rule | PBT `gapscan.filter`
- HAT-DATA-122: Gap-scan timing is recorded | PBT `gapscan.timing_recorded`, OPS `gapscan.latency_observed`
- HAT-DATA-123: One provider socket, bounded reconnects | PBT `feed.session_one_socket`, PBT `feed.session_gives_up`, PBT `feed.session_close_stops_a_backing_off_runner`, PBT `feed.socket_factory`, PBT `feed.token_source`, PBT `feed.traceback_throttle`
- HAT-DATA-124: Ticks are normalised once | PBT `feed.tick_normalised`, PBT `feed.tick_flow`
- HAT-DATA-125: The subscription registry is reference counted per owner | PBT `feed.registry_model`, PBT `feed.registry_concurrent`
- HAT-DATA-126: The provider converges on the wanted set | PBT `feed.provider_converges`
- HAT-DATA-127: The feed follows login state and market hours | PBT `feed.auth_hooks`, PBT `feed.off_hours_stops_a_backing_off_runner`, PBT `feed.monitor_survives_errors`, PBT `feed.token_check_off_loop`, PBT `feed.calendar_closed`

### Telegram

- HAT-TG-005: Messages from other chats are ignored and logged | PBT `telegram.unknown_chat_has_no_effect`
- HAT-TG-010: Long polling only, no inbound port | CONF `telegram.no_webhook_no_listener`
- HAT-TG-011: The offset commits with the handled update; none is dropped or applied twice | PBT `telegram.offset_commits_with_update`
- HAT-TG-012: Every inbound command is logged | PBT `telegram.every_command_logged`
- HAT-TG-013: User-initiated events carry actor and chat ID | PBT `telegram.user_events_attributed`
- HAT-TG-014: No broker or secret text leaks into messages or logs | PBT `telegram.no_secret_in_output`
- HAT-TG-020: Outbound messages go only to the allowlisted chat | PBT `telegram.send_only_to_allowlist`
- HAT-TG-021: Login prompts at 08:30 and 09:05 | TLA `JobScheduler!LoginPromptOrder`, PBT `telegram.login_prompt_links`
- HAT-TG-022: Watchlist digest content | PBT `telegram.watchlist_digest_content`, PBT `telegram.watchlist_digest_once`
- HAT-TG-023: Shadow trade and approval card content | PBT `telegram.card_matches_signal`, PBT `confidence.card_shows_label`
- HAT-TG-024: Event-driven and scheduled reports | TLA `JobScheduler!ReportsAfterClose`, PBT `telegram.report_content`, PBT `telegram.relay_late_commit`, PBT `telegram.veto_digest_on_week_end`
- HAT-TG-030: Command set is closed | CONF `telegram.command_catalogue`, PBT `telegram.unknown_command_rejected`, PBT `telegram.other_bot_ignored`
- HAT-TG-031: No order from free text | PBT `telegram.no_free_text_orders`, CONF `telegram.no_order_import_in_text_path`
- HAT-TG-032: Mode changes only outside market hours, effective next session | TLA `ExecutionRouter!ModeChangeNextSession`, PBT `telegram.mode_outside_hours`
- HAT-TG-033: Second confirmation for auto, /arm and /flatten | TLA `ApprovalToken!SecondConfirmRequired`, PBT `telegram.second_confirm`
- HAT-TG-034: /kill makes every bot manage-only and cancels pending approvals | TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!ExitsContinue`
- HAT-TG-035: /flatten is exit-only and logged as a discipline breach | TLA `KillSwitch!FlattenExitOnly`, PBT `telegram.flatten_logged`
- HAT-TG-036: /pause and /resume | TLA `KillSwitch!PauseBlocksEntriesOnly`
- HAT-TG-038: /mode names one bot and says why it is refused | PBT `telegram.mode_names_a_bot_and_its_gate`
- HAT-TG-037: /status, /positions, /watchlist and /login are read-only | PBT `telegram.read_commands_side_effect_free`
- HAT-TG-040: Tokens are signed and bound to signal and expiry | PBT `telegram.token_forgery_rejected`
- HAT-TG-041: Tokens are single-use | TLA `ApprovalToken!SingleDecision`
- HAT-TG-042: Approvals expire after 5 minutes | TLA `ApprovalToken!NoTakeAfterExpiry`, PBT `telegram.expiry_window`
- HAT-TG-043: Take re-checks price against max-chase | TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `telegram.take_chase_recheck`
- HAT-TG-044: Token hash only; raw token never stored or logged | PBT `telegram.token_never_persisted`
- HAT-TG-045: Skipped and expired signals continue as shadow trades | TLA `ExecutionRouter!SkipBecomesShadow`
- HAT-TG-050: Telegram messages and command log kept 1 year | PBT `telegram.retention_purge`

### Backtest

- HAT-BT-010: Backtest calls the same scan, risk and exit code as live | CONF `backtest_shares_engine_imports`, PBT `backtest.scan_parity`, PBT `backtest.nightly_scan_parity`
- HAT-BT-011: No look-ahead | PBT `backtest.no_lookahead`, PBT `backtest.nightly_no_lookahead`
- HAT-BT-012: Survivorship-free, adjusted data | PBT `backtest.survivorship_free`, PBT `backtest.market_point_in_time`
- HAT-BT-013: A run pins one strategy version and one risk-policy version | PBT `backtest.version_pinned`
- HAT-BT-014: Entries are sized from the sleeve's nightly allocation | PBT `backtest.sized_from_allocation`, PBT `backtest.invested_share`, PBT `backtest.sizing_conventions_refused`, PBT `backtest.regime_inputs_loaded`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.fill_conventions_recorded_digest`
- HAT-BT-020: Entry fill on daily bars | PBT `backtest.entry_fill_proxy`, PBT `backtest.entry_fill_proxy_chase`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.fill_conventions_recorded_digest`, PBT `backtest.stop_on_instrument_tick`, PBT `backtest.pre_break_low`, PBT `backtest.entry_day_stop`, PBT `backtest.contraction_stop_too_wide`
- HAT-BT-021: Stop and close-based exit fills | PBT `backtest.exit_fill_proxy`, PBT `backtest.exit_fill_proxy_close`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.raised_stop_dip`, PBT `backtest.raised_stop_dip_rules_agree`, PBT `backtest.lower_circuit_inferred`, PBT `backtest.lower_circuit_carry`, PBT `backtest.suspension_markdown`, PBT `backtest.path_rules_refused`
- HAT-BT-022: Slippage | PBT `costs.slippage_adverse`, PBT `costs.slippage_adverse_net`, PBT `costs.slippage_adverse_bounds`, PBT `backtest.fill_conventions_recorded`
- HAT-BT-023: Statutory costs for delivery trades | PBT `costs.charges_contract`, PBT `costs.charges_contract_run`, PBT `costs.charges_contract_float_rates`
- HAT-BT-024: Money in Decimal, quantities integer | PBT `backtest.decimal_money`
- HAT-BT-025: Entry filters at the trigger | PBT `backtest.adtr_filter_applied`, PBT `backtest.upper_circuit_inferred`, PBT `backtest.upper_circuit_skip`
- HAT-BT-026: Slot order on a capped day | PBT `backtest.slot_order_rank`, PBT `backtest.slot_order_refused`, PBT `backtest.slot_order_applied`, PBT `backtest.fill_conventions_recorded`, PBT `backtest.fill_conventions_recorded_digest`
- HAT-BT-030: Every run increments the trial count of its family | PBT `dsr.trial_counting`, PBT `dsr.dsr_needs_recorded_run`, PBT `dsr.returns_persisted`, CONF `dsr_scored_only_by_runs`
- HAT-BT-031: Family key is strategy × universe | PBT `dsr.family_key`
- HAT-BT-032: Run record and trade output | CONF `backtest_run_columns`, PBT `backtest.trades_match_metrics`, PBT `backtest.fill_conventions_recorded`
- HAT-BT-040: Trade R and expectancy | PBT `backtest.r_and_expectancy`, PBT `backtest.robustness_metrics`, PBT `metrics.tail_trimmed_mean`
- HAT-BT-041: Standard metrics reuse the existing module | PBT `backtest.metrics_bounds`, PBT `backtest.robustness_metrics`, PBT `metrics.longest_drawdown`, PBT `metrics.exposure`, PBT `metrics.mar_ratio`, PBT `metrics.cost_share`
- HAT-BT-042: Deflated Sharpe probability | PBT `dsr.bounds_and_monotonicity`, PBT `dsr.bounds_and_monotonicity_in_sr`, PBT `dsr.bounds_and_monotonicity_first_trial`, PBT `dsr.bounds_and_monotonicity_undefined`, PBT `dsr.bounds_and_monotonicity_bad_denominator`, CONF `dsr_published_example`, PBT `dsr.family_floors`, PBT `dsr.pbo_ranked_variants`, PBT `dsr.pbo_two_variants`, PBT `dsr.pbo_refused`, PBT `dsr.family_pbo_recorded`, PBT `dsr.family_pbo_skips_unreadable`
- HAT-BT-050: Backtests are deterministic | PBT `backtest.deterministic`
- HAT-BT-051: Golden replay of a fixed fixture market | CONF `backtest_golden_replay`
- HAT-BT-060: Evidence hook for the Backtested state | PBT `lifecycle.backtest_gate`, PBT `lifecycle.backtest_refusal`, PBT `dsr.evidence_current_trials`, PBT `dsr.evidence_promoted_only`, PBT `dsr.evidence_latest_by_completion`, PBT `dsr.evidence_gate_facts`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- HAT-BT-061: Strategy versions record their backtest | DB `governance.live_requires_evidence`, PBT `backtest.version_status_requires_run`
- HAT-BT-070: Every unmodelled behaviour is a listed gap | CONF `known_gaps_listed`, PBT `lifecycle.backtest_refusal`, PBT `backtest.deterministic`, PBT `backtest.fill_conventions_recorded`

### Risk

- HAT-RISK-010: Risk policy holds every risk number | CONF `risk.policy_schema_fields`, CONF `risk.no_risk_keys_in_strategy_schema`, PBT `sizing.uses_effective_policy`
- HAT-RISK-011: Initial policy seed | CONF `risk.initial_seed_equals_spec`, PBT `policy.seed_supersedes_old_initial`, PBT `policy.upkeep_raises_unless_race_lost`, DB `migration.ensure_idempotent`
- HAT-RISK-020: Position size formula | PBT `sizing.formula_exact`, PBT `sizing.risk_bounded`, PBT `sizing.monotone`
- HAT-RISK-021: Zero quantity skips the trade | PBT `sizing.zero_skips_with_reason`
- HAT-RISK-022: Stop must be below entry for a long | PBT `sizing.stop_below_entry_required`
- HAT-RISK-030: Global open-risk cap | PBT `caps.global_open_risk`, PBT `trades.global_open_risk_across_bots`, DB `runtime.owner_serialised`
- HAT-RISK-031: Max open positions per bot | PBT `caps.max_positions`
- HAT-RISK-032: Per-industry cap | PBT `caps.industry`
- HAT-RISK-033: Daily entry cap | PBT `caps.daily_entries`, PBT `trades.day_counts`
- HAT-RISK-034: Daily stop-out cap | PBT `caps.daily_stopouts`, PBT `trades.day_counts`
- HAT-RISK-035: Caps never block exits | PBT `caps.exits_never_blocked`
- HAT-RISK-036: Per-instrument cooldown after a hard stop | PBT `caps.cooldown`, PBT `caps.cooldown_input_fails_closed`, PBT `caps.cooldown_shortening_is_increase`, PBT `trades.last_hard_stop`, PBT `backtest.cooldown_applied`, PBT `dsr.evidence_cooldown_in_force`
- HAT-RISK-040: Ladder transitions | PBT `exposure.ladder_transitions`
- HAT-RISK-041: Ladder bounds positions and risk | PBT `exposure.level_applies`
- HAT-RISK-042: Shadow trades do not move the ladder | PBT `exposure.virtual_ignored`
- HAT-RISK-050: Gate off blocks new entries only | PBT `caps.regime_gate_blocks_entries`, TLA `ExecutionRouter!GateOffNoEntry`
- HAT-RISK-051: Gate checked nightly and at 09:20 | TLA `ExecutionRouter!GateFreshBeforeFirstEntry`
- HAT-RISK-052: No regime override | CONF `risk.no_gate_override_path`, TLA `ExecutionRouter!GateOffNoEntry`
- HAT-RISK-060: Account drawdown kill | PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly`
- HAT-RISK-061: Bot drawdown kill | PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly`
- HAT-RISK-062: Manual kill | TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!TripImpliesManageOnly`
- HAT-RISK-063: Health kill | TLA `KillSwitch!HealthAutoRearm`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `kill.kill_service_matches_model`, PBT `router.broker_health_follows_the_session`
- HAT-RISK-064: Manual re-arm with second confirm | TLA `KillSwitch!NoAutoRearm`
- HAT-RISK-065: Manage-only keeps protecting positions | TLA `KillSwitch!ExitsContinue`
- HAT-RISK-066: A planned reallocation is not a drawdown | PBT `kill.reallocation_is_not_drawdown`, PBT `kill.snapshot_latest_allocation`
- HAT-RISK-070: Invariant 1, stop never widens | TLA `StopPlacement!StopOnlyTightens`, DB `governance.stop_only_rises`, PBT `exits.never_lowers_stop`
- HAT-RISK-071: Invariant 2, never add to a loser | PBT `caps.no_add_to_loser`
- HAT-RISK-072: No adding to winners (B20) | PBT `caps.no_pyramiding`
- HAT-RISK-073: Invariant 3, no entry without a stop | PBT `caps.entry_requires_stop`, TLA `ExecutionRouter!EntryHasStop`
- HAT-RISK-074: Invariant 4, no order on stale data | TLA `ExecutionRouter!NoOrderWhenStale`, PBT `caps.stale_blocks_entry`, PBT `bars.exits_and_placement_read_the_checked_price`
- HAT-RISK-075: Invariant 5, no order outside market hours | PBT `caps.market_hours_only`, TLA `ExecutionRouter!NoOrderOutsideHours`
- HAT-RISK-076: Invariant 6, max-chase always applies | PBT `caps.max_chase_always`
- HAT-RISK-100: One risk authority | CONF `risk.single_sizing_path`, TLA `ExecutionRouter!RiskBeforeRoute`
- HAT-RISK-101: Decision is typed and stored | PBT `caps.decision_total`
- HAT-RISK-102: Decimal money and fail closed | PBT `caps.fail_closed`
- HAT-RISK-103: HAT's safety properties are pinned and no manual-trading path exists | CONF `risk.hat_safety_properties_pinned`, CONF `risk.no_manual_trading_path`
- HAT-RISK-104: HAT's own kill switches are the only halt | PBT `caps.hat_kill_is_authority`, CONF `risk.no_legacy_halt_read`

### Governance

- HAT-GOV-080: Live levers only reduce risk | CONF `gov.lever_catalogue`, TLA `ExecutionRouter!UserCannotRaiseRisk`
- HAT-GOV-081: Pause and resume | TLA `KillSwitch!PauseBlocksEntriesOnly`
- HAT-GOV-082: Flatten-all is a logged discipline breach | TLA `KillSwitch!FlattenNeedsConfirm`
- HAT-GOV-083: Strategy changes ship as gated releases | TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`, PBT `config.release_gate`
- HAT-GOV-084: Mode changes outside hours, effective next session | TLA `ExecutionRouter!ModeChangeNextSession`, PBT `router.mode_change_takes_effect_next_session`
- HAT-GOV-085: Confirm and auto need the owner's DDPI and weights confirmation; the open applies or refuses each change | PBT `router.mode_gates_hold_bots_in_shadow`, PBT `router.open_settles_mode_changes`, PBT `broker.confirm_and_auto_need_gtt`, PBT `admin.first_deploy`, PBT `admin.refusal_writes_nothing`, PBT `admin.ddpi_revoked`
- HAT-GOV-090: Decrease effective next session | PBT `caps.policy_direction_classification`
- HAT-GOV-091: Increase waits 7 days and passes the evidence gate | DB `governance.risk_increase_cooling_off`, PBT `caps.increase_gate`, PBT `dsr.gate_evidence`, PBT `caps.policy_change_inside_session_or_without_evidence_is_refused`
- HAT-GOV-092: Policy versions are immutable | DB `governance.risk_version_immutable`
- HAT-GOV-093: Multiplier bounds live in the policy | PBT `caps.policy_direction_classification`
- HAT-GOV-094: Changes are events | PBT `caps.policy_change_emits_event`

### Operations

- HAT-OPS-001: HAT is off unless HAT_ENABLED is true | PBT `ops.disabled_starts_nothing`
- HAT-OPS-002: HAT refuses to run on SQLite | PBT `ops.refuses_non_postgres`
- HAT-OPS-003: Restart safety: reconcile before acting | TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, TLA `ExecutionRouter!EntriesSettled`, PBT `router.crash_resumes_entry_once`, PBT `router.stranded_entry_ends_next_day`, PBT `router.resumed_entry_waits_for_feed`, PBT `router.one_login_per_broker`, PBT `ops.stop_names_abandoned_jobs`, PBT `ops.stop_after_failed_start`
- HAT-OPS-004: Exits and stops never depend on the nightly pipeline | TLA `NightlyPipeline!ExitsIndependentOfPipeline`
- HAT-OPS-010: A job keyed (job, trade_date) runs at most once to success | TLA `JobScheduler!AtMostOnceSuccess`, DB `ops.job_run_unique`
- HAT-OPS-011: A failed job can be retried | TLA `JobScheduler!FailedJobRetryable`, PBT `ops.job_run_recording`, PBT `ops.retry_after_failed_run`, PBT `ops.chain_carries_past_midnight`
- HAT-OPS-012: Scheduler skips non-trading days | PBT `ops.calendar_skip`
- HAT-OPS-013: Scheduler does not run when HAT is disabled | PBT `ops.disabled_starts_nothing`
- HAT-OPS-021: Nightly pipeline order | TLA `NightlyPipeline!NightlyPipelineOrder`, TLA `NightlyPipeline!AdjustIndependentOfGate`, TLA `NightlyPipeline!SnapshotEveryNight`, PBT `ops.entry_inputs_fail_closed`
- HAT-OPS-022: Ingest polling and the 22:00 fallback | TLA `NightlyPipeline!FallbackAt2200`
- HAT-OPS-023: Intraday sequence | TLA `DailyCycle!IntradayOrder`, PBT `router.entry_needs_live_session`, CONF `ops.route_matches_protocol`
- HAT-OPS-024: Nightly GTT verification | TLA `StopPlacement!NightlyVerifyCovers`
- HAT-OPS-030: Data failure blocks scans, never exits | TLA `KillSwitch!DataHealthManageOnly`, TLA `NightlyPipeline!ExitsIndependentOfPipeline`
- HAT-OPS-031: Broker session drop: retry with backoff, alert after 2 minutes | TLA `KillSwitch!DataHealthManageOnly`, PBT `ops.session_backoff_alert`, PBT `ops.no_outage_before_the_login_window`
- HAT-OPS-032: Order rejected: retried at most once | TLA `OrderReconciliation!RetryAtMostOnce`
- HAT-OPS-033: Duplicate trigger is refused | TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`
- HAT-OPS-034: 15:25 exit vs official close is logged | PBT `ops.close_gap_recorded`
- HAT-OPS-040: Compose deployment | CONF `ops.compose_services`
- HAT-OPS-041: Host prerequisites | OPS `ops.host_checklist`
- HAT-OPS-042: No restarts or deploys during market hours | OPS `ops.deploy_window`, PBT `ops.deploy_window_guard`
- HAT-OPS-043: 4G failover places no orders | OPS `ops.failover_drill`
- HAT-OPS-050: Secrets only in .env outside the repo | CONF `ops.no_secrets_in_repo`, OPS `ops.env_permissions`
- HAT-OPS-051: Fyers credentials are not duplicated | CONF `ops.single_fyers_secret_store`
- HAT-OPS-060: Nightly pg_dump and Parquet sync | OPS `ops.backup_restore_drill`, PBT `ops.backup_manifest`
- HAT-OPS-070: Structured logs | PBT `ops.log_records_structured`
- HAT-OPS-071: Health alerts go to Telegram | PBT `telegram.report_content`
- HAT-OPS-072: Runtime status is observable | PBT `ops.status_matches_db`
- HAT-OPS-090: Runbook covers every OPS check and failure | CONF `ops.runbook_complete`
- HAT-OPS-091: The owner's admin command line | PBT `admin.first_deploy`, PBT `admin.refusal_writes_nothing`, CONF `ops.admin_cli_calls_services`, PBT `admin.shadow_beside_live`, PBT `admin.ddpi_revoked`

### Execution

- HAT-EXEC-010: Shadow never reaches the broker | TLA `ExecutionRouter!ShadowNeverCallsBroker`, PBT `exits.shadow_rows_virtual`
- HAT-EXEC-011: Default mode is confirm (revised, D130/D132) | CONF `exec.default_mode_confirm`
- HAT-EXEC-014: Auto places only high-confidence signals | PBT `router.auto_places_only_high_confidence`, PBT `router.placement_rechecks_confidence`
- HAT-EXEC-015: Every signal carries an immutable confidence label | PBT `confidence.label_rule`, PBT `confidence.label_monotone`, PBT `confidence.band_of`, PBT `confidence.bucket_stats`, PBT `confidence.buckets_match_trades`, PBT `confidence.signal_label_from_evidence`, PBT `confidence.backtest_label_is_fixed`, DB `governance.signal_confidence_immutable`
- HAT-EXEC-012: Auto requires a broker with delivery GTT | TLA `ExecutionRouter!AutoNeedsGtt`, PBT `router.routes_by_mode`, PBT `router.a_stored_capability_never_enables_a_real_entry`, PBT `tags.refusals_happen_before_the_broker`
- HAT-EXEC-013: Delivery only | PBT `tags.intent_product_cnc`
- HAT-EXEC-020: One risk check, three routes | TLA `ExecutionRouter!RouteByMode`, TLA `ExecutionRouter!RiskBeforeRoute`, PBT `router.routes_by_mode`, PBT `router.refused_entry_is_not_sent`
- HAT-EXEC-021: Duplicate triggers are refused | TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`, PBT `trades.retry_returns_existing_signal`
- HAT-EXEC-022: Router honours invariants 4 and 5 | TLA `ExecutionRouter!NoOrderWhenStale`, TLA `ExecutionRouter!NoOrderOutsideHours`, PBT `router.take_rechecks_max_chase`
- HAT-EXEC-023: Order rejection retried at most once | TLA `OrderReconciliation!RetryAtMostOnce`, PBT `router.rejected_entry_is_retried_once`, PBT `router.crash_resumes_entry_once`
- HAT-EXEC-030: Approval expires after 5 minutes | TLA `ApprovalToken!NoTakeAfterExpiry`, TLA `ExecutionRouter!EntriesSettled`, PBT `router.sweep_idempotent`, PBT `router.stranded_entry_ends_next_day`, PBT `router.sweep_survives_a_bad_signal`
- HAT-EXEC-031: Max-chase re-check on take | TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `caps.max_chase_always`, PBT `router.take_rechecks_max_chase`, PBT `bars.circuit_locks_read_the_band`, PBT `bars.exits_and_placement_read_the_checked_price`
- HAT-EXEC-032: Skips continue as shadow trades | TLA `ExecutionRouter!SkipBecomesShadow`, PBT `router.skip_and_expiry_become_one_veto_trade`
- HAT-EXEC-033: Exits never ask | TLA `ExecutionRouter!ExitsNeverAsk`
- HAT-EXEC-034: Catalyst required in confirm for EP | PBT `criteria.catalyst_required_in_confirm`
- HAT-EXEC-040: Lifecycle states and transitions | TLA `TradeLifecycle!TypeOK`, TLA `TradeLifecycle!OnlyAllowedTransitions`, CONF `lifecycle_transitions_match_spec`
- HAT-EXEC-041: Endings are terminal and every trade ends | TLA `TradeLifecycle!EndingsTerminal`, TLA `TradeLifecycle!EventuallyEnds`, PBT `manage.entry_ended_by_broker`
- HAT-EXEC-042: State derived from events in one transaction | TLA `TradeLifecycle!StateMatchesLog`, PBT `exits.replay_reproduces_state`, CONF `journal_only_writers`, PBT `journal.validates_before_writing`, PBT `journal.check_names_mismatches`, PBT `journal.check_alerts`, PBT `manage.async_sale_rejection_retried`
- HAT-EXEC-043: Rejections reach the daily digest only | TLA `ExecutionRouter!RejectNoCard`
- HAT-EXEC-044: Shadow runs the same states | TLA `TradeLifecycle!ShadowSameMachine`
- HAT-EXEC-045: One bot's entries are evaluated and written one at a time | TLA `TradeLifecycle!PerBotCapHolds`, DB `runtime.per_bot_serialised`, PBT `trades.shared_bot_cap`
- HAT-EXEC-046: An unfilled entry ends on a failed break or a timeout | PBT `manage.unfilled_entry_cancelled`, PBT `bars.live_flags_parsed`
- HAT-EXEC-050: below_final_contraction_low | PBT `stops.final_contraction_low`, PBT `stops.contraction_stop_at_the_fill`
- HAT-EXEC-051: low_of_day | PBT `stops.low_of_day`, PBT `bars.trigger_never_uses_a_partial_session`
- HAT-EXEC-052: Custom stop rules | PBT `stops.custom_rules`
- HAT-EXEC-053: Stops are priced on the instrument's real tick | PBT `stops.tick_size_slabs`, PBT `ops.daily_tick_matches_reference`
- HAT-EXEC-060: Stop at the broker on every real fill | TLA `StopPlacement!StopAfterFill`, TLA `StopPlacement!SharesTracked`, TLA `StopPlacement!RestCancelledOnExit`, TLA `StopPlacement!EmptiedCloses`, PBT `manage.real_fill_protected`, PBT `manage.a_resting_sale_is_never_stopped_over`, PBT `manage.entry_remainder_tracked`, PBT `router.fill_sync_cadence`
- HAT-EXEC-061: Intraday stop swapped to GTT at ~15:25 | TLA `StopPlacement!NoUnprotectedGap`, PBT `manage.swap_leaves_no_gap`
- HAT-EXEC-062: GTT limit offset | PBT `stops.gtt_limit_offset`, TLA `StopPlacement!GttUnfilledAlerted`, PBT `manage.gtt_trigger_handled`
- HAT-EXEC-063: Nightly GTT verify | TLA `StopPlacement!NightlyVerifyCovers`, PBT `manage.verify_covers`, PBT `manage.verify_renews_old_gtt`
- HAT-EXEC-064: Stop moves follow the stop | TLA `StopPlacement!BrokerStopMatchesCurrent`, TLA `StopPlacement!StopOnlyTightens`, PBT `manage.broker_stop_only_tightens`, PBT `manage.verify_covers`
- HAT-EXEC-065: A lost broker answer never freezes a position | TLA `StopPlacement!LostGttSettled`, TLA `StopPlacement!LostNeverBlocksSale`, PBT `manage.lost_answers_never_freeze`, PBT `manage.a_settled_answer_never_sells_twice`, PBT `manage.the_close_ends_only_settled_answers`, PBT `tags.the_gtt_book_reads_only_resting_stops`, PBT `tags.a_reserved_tag_is_sent_by_one_sender`, PBT `tags.a_lost_gtt_is_found_by_its_tag`
- HAT-EXEC-066: A triggered GTT's resting sale is sold at the market | TLA `StopPlacement!RestingSold`, PBT `manage.gtt_resting_marketed`, PBT `mpp.gtt_market_sale_asked_again`
- HAT-EXEC-070: Ordered evaluation, full beats partial | PBT `exits.full_beats_partial`
- HAT-EXEC-071: Fire once | PBT `exits.fire_once`
- HAT-EXEC-072: Fraction arithmetic | PBT `exits.fraction_arithmetic`
- HAT-EXEC-073: Breakeven and partial rules | PBT `exits.rule_semantics`
- HAT-EXEC-074: Scheduled exits at 15:25 on live price | TLA `JobScheduler!ExitsCloseInWindow`, PBT `manage.no_sale_after_close`, PBT `ops.window_not_starved`
- HAT-EXEC-075: An exit the exchange rejects is retried once | PBT `manage.async_sale_rejection_retried`, PBT `mpp.resting_market_sale_resent`, PBT `mpp.lower_circuit_lock_holds_the_sale`, PBT `mpp.rejected_twice_goes_at_the_band`, PBT `mpp.no_cancel_without_a_resend`
- HAT-EXEC-120: Triggers on final bars inside the window | PBT `bars.trigger_only_on_final_bars`
- HAT-EXEC-121: All trigger criteria must pass | PBT `criteria.trigger_conjunction`
- HAT-EXEC-122: Entry filters skip with reason | PBT `criteria.entry_filters_skip`
- HAT-EXEC-130: Skips are measured | PBT `exits.veto_score`
- HAT-EXEC-131: Veto verdict after 50 decisions | PBT `exits.veto_verdict_threshold`
- HAT-EXEC-140: Next-session actions are fetched the night before | PBT `rescale.next_session_lookup`
- HAT-EXEC-141: Open positions are rescaled before the open | PBT `rescale.preserves_position`, PBT `rescale.applied_once`, PBT `rescale.factor_exact`, DB `governance.rescale_moves_stop`
- HAT-EXEC-142: Until rescaled, the instrument is manage-only and its exits wait | PBT `rescale.pending_is_manage_only`
- HAT-EXEC-143: The broker stop follows the rescale down | PBT `rescale.broker_stop_follows`
- HAT-EXEC-144: An action the rescale cannot handle leaves the position manage-only until the owner resolves it | PBT `rescale.unhandled_is_manage_only`, PBT `manage.flatten_capped_at_holdings`, PBT `corporate_actions.factor_parse_unparseable_is_reported`, PBT `rescale.price_neutral_changes_nothing`, PBT `rescale.resolution_ends_lock`
- HAT-EXEC-145: A GTT the broker cancelled before an ex-date is placed again before the open | PBT `manage.ex_date_gtt_replaced`, PBT `ops.ex_date_job_behind_flag`, PBT `manage.ex_date_check_serialised`
- HAT-EXEC-146: A series change makes the position manage-only and re-maps its symbol | PBT `manage.series_change_remapped`

### Brokers

- HAT-BRK-080: Interface completeness and boundary | CONF `brk.interface_methods`, CONF `brk.import_boundary`
- HAT-BRK-081: Broker errors fail closed | PBT `tags.broker_errors_fail_closed`, PBT `tags.a_keyless_book_never_reads_as_absent`, PBT `fyers_broker.an_ok_reply_without_its_list_is_unreadable`, PBT `fyers_broker.a_circuit_opening_mid_place_is_not_sent`, PBT `fyers_broker.a_rejection_with_an_id_stays_rejected`
- HAT-BRK-082: Broker session handling | TLA `ExecutionRouter!NoEntryWithoutSession`, PBT `broker.token_refused_is_session_expired`, PBT `broker.token_refused_on_a_read_ends_the_check`, PBT `fyers_auth.a_good_exchange_saves_once_and_reports_in_order`, PBT `broker.rate_limit_is_its_own_code`, PBT `bars.minute_history_rate_limit_stops_run`, PBT `tags.an_unreachable_broker_is_not_a_rejection`, PBT `fyers_broker.budget_windows_hold`, PBT `fyers_broker.throttled_call_not_sent`, PBT `fyers_broker.writes_go_ahead_of_reads`, PBT `fyers_broker.limits_from_settings`, PBT `fyers_broker.late_429_still_pauses`, PBT `fyers_broker.breaker_opened_while_waiting_sends_nothing`
- HAT-BRK-090: Exact Fyers payloads | PBT `tags.fyers_payload_mapping`, PBT `tags.gtt_payloads`, PBT `tags.the_gtt_book_reads_only_resting_stops`, CONF `brk.recorded_writes`
- HAT-BRK-091: Capabilities reported honestly | CONF `brk.fyers_capabilities`, CONF `brk.recorded_writes`, PBT `broker.the_fyers_adapter_unlocks_modes_on_an_active_account`
- HAT-BRK-093: Fyers HTTP leaves over IPv4 | PBT `fyers_ipv4.fyers_http_leaves_over_ipv4`, PBT `fyers_ipv4.is_fyers_host_exact`, PBT `fyers_ipv4.no_proxy_carries_fyers`, CONF `brk.fyers_adapter_wraps_existing`
- HAT-BRK-092: Onboarding and capability check | TLA `OrderReconciliation!AccountActiveAfterCheck`, PBT `broker.session_check_refreshes_capabilities`, PBT `router.desks_start_with_the_adapters_capabilities`, CONF `brk.account_constants_one_copy`
- HAT-BRK-100: Tag shape and uniqueness | PBT `tags.shape_and_roundtrip`, DB `runtime.order_tag_unique`
- HAT-BRK-101: Persist before call | TLA `OrderReconciliation!PersistBeforeCall`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- HAT-BRK-102: Unconfirmed submit resolved by tag | TLA `OrderReconciliation!UnconfirmedResolvedByTag`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`, TLA `OrderReconciliation!LostEntryAdopted`, PBT `tags.a_timeout_is_looked_up_before_any_retry`, PBT `tags.idempotent_order_submission`, PBT `router.unresolved_entry_is_alerted_not_resent`
- HAT-BRK-103: Fyers does not enforce tag uniqueness | CONF `brk.recorded_writes`, PBT `tags.a_tag_twice_at_the_broker_blocks_the_account`, PBT `manage.a_second_order_under_one_tag_is_tracked`, PBT `manage.a_dead_second_order_never_ends_the_entry`, PBT `tags.a_duplicated_tag_is_read_one_way`
- HAT-BRK-110: Reconcile before the first order | TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.reconcile_before_first_order`, PBT `router.routine_reconcile_keeps_gate`, PBT `router.entry_gate_keyed_by_trade_date`, PBT `router.failed_first_reconcile_alerts_once_per_date`
- HAT-BRK-111: Reconciliation fails closed | TLA `OrderReconciliation!MismatchBlocksEntries`, PBT `manage.real_exits_never_blocked`, PBT `tags.held_quantity_nets_today`, PBT `router.older_read_never_overrides_newer`
- HAT-BRK-112: Terminal orders ignored | PBT `tags.reconcile_scope`, PBT `tags.unreadable_foreign_rows_skipped`

### Verification

- HAT-VER-010: Every requirement carries a checkable property | CONF `spec_requirements_well_formed`
- HAT-VER-011: Example tests are not acceptance for new HAT code | CONF `traceability_points_to_methods`
- HAT-VER-020: Verification order follows the harm ranking | CONF `ci_runs_models_for_merged_targets`
- HAT-VER-030: Model files and toolchain are pinned | CONF `tla_toolchain_pinned`
- HAT-VER-031: Every model action maps to code | CONF `tla_actions_mapped`
- HAT-VER-032: Every property is shown to fail on a broken variant | CONF `tla_broken_variants_fail`
- HAT-VER-033: Bounds and assumptions are stated per model | CONF `tla_bounds_declared`
- HAT-VER-034: Findings stay findings | CONF `verify_skill_findings_recorded`
- HAT-VER-040: Properties run on the real code with a fixed profile | CONF `hypothesis_profile_registered`
- HAT-VER-041: Money and quantity properties use exact arithmetic | CONF `pbt_money_strategies_decimal`
- HAT-VER-050: DB checks only for DB-enforced invariants | CONF `db_checks_match_governance_table`
- HAT-VER-060: One CI job runs TLC and the Hypothesis suite | CONF `ci_verification_job`
- HAT-VER-070: Each verify skill is complete and registers | CONF `verify_skills_complete`
- HAT-VER-071: Coverage record and sync commit | CONF `verify_skill_sync_current`
- HAT-VER-072: Each skill is proven to catch a break | CONF `verify_skill_break_recorded`
