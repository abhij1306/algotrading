# Hire a Trader (HAT): specs

These specs are the source of truth for the HAT module (`backend/app/hat/`) in SmartTrader. They turn
the four design docs of 20 Oct 2026 (Master Architecture, Business Logic, Data Sources & Integration,
Database Schema) and the owner's build decisions into precise requirements, each with properties a
verification can check.

## Specs win

- Where these specs and existing repo code or old documentation disagree, **the specs win**. The old
  docs (everything under `docs/` outside this folder, and the two product-requirement files at the repo root) described a much older
  version of the project and were deleted with this folder's first commit.
- Where the design docs and the repo conflict, the resolutions C1-C12 in
  [00-overview.md](00-overview.md) §5 apply.
- Owner decisions of 20 Oct override the design docs: HAT is an in-repo module; orders go through
  HAT's own broker interface with a direct Fyers adapter; there is no OpenAlgo in the MVP; the specs
  win over existing code ([01-decisions.md](01-decisions.md) §5). New HAT code is verified formally
  (TLA+, Hypothesis, DB checks), not with example unit tests ([11-verification.md](11-verification.md)).
- A change to HAT behaviour changes the spec first, in the same PR.

## Index

| File | Covers | ID prefix |
| --- | --- | --- |
| [00-overview.md](00-overview.md) | Scope and phases, glossary, module boundaries and import rules, reuse map, conflict resolutions C1-C12, non-goals | HAT-OVR |
| [01-decisions.md](01-decisions.md) | Decisions T1-T16, B1-B27 (revised B1/B2/B12), I1-I17, PL1-PL5, build decisions of 20 Oct | HAT-DEC-&lt;code&gt; |
| [02-data.md](02-data.md) | Source map, reference data, NSE MCP client, nightly ingest, corporate actions, quality gate, features, Parquet/DuckDB, historical build, announcements, live bars, gap scan | HAT-DATA |
| [03-schema.md](03-schema.md) | The 42 tables, the event log (`hat_event`) and event types, DB-enforced governance, indexes, retention, migration mechanism | HAT-SCH |
| [04-criteria-and-bots.md](04-criteria-and-bots.md) | Nine-stage model, criterion instances, scoring, the 25 criterion types, playbooks, S1 and S2 default configs, versioning | HAT-CRIT, HAT-BOT |
| [05-portfolio-regime-allocation.md](05-portfolio-regime-allocation.md) | Sleeves, universes, regime v1, health multiplier, allocator, sleeve lifecycle, regime v2 (P2) | HAT-PORT |
| [06-risk-and-governance.md](06-risk-and-governance.md) | Risk policy, sizing, caps, progressive exposure, regime gate, kill switches, invariants, governance, the risk authority (`evaluate_signal`) | HAT-RISK, HAT-GOV |
| [07-execution-and-brokers.md](07-execution-and-brokers.md) | Modes, router, approvals, trade lifecycle, stops, exits, broker interface, Fyers adapter, tags, reconciliation, live triggers, veto scoring | HAT-EXEC, HAT-BRK |
| [08-telegram.md](08-telegram.md) | Transport, allowlist, outbound messages, commands, approval tokens, retention | HAT-TG |
| [09-backtest.md](09-backtest.md) | Shared engine, fill and cost model, runs and trials, metrics and deflated Sharpe, replay determinism, lifecycle evidence | HAT-BT |
| [10-ops.md](10-ops.md) | Runtime layout, scheduler, daily cycle, failure handling, deployment, secrets, backups, observability, runbook | HAT-OPS |
| [11-verification.md](11-verification.md) | Verification methods, targets ranked by harm, TLA+ models, Hypothesis suite, DB checks, CI job, verify skills | HAT-VER |

## Requirement format and ID scheme

Every requirement is one block:

```
#### HAT-<AREA>-<nnn>: <short title>
- **Statement:** one line.
- **Source:** <doc "section">; <decision IDs>.
- **PR:** the plan PR(s) that implement it.
- **Verification:** <METHOD `name`>, ...
- **Acceptance:**
  - Property: <an invariant or temporal property, for every input or every reachable state>. Harm: <what breaks if it fails>.
- **[A]** <assumption value>: default <x>; validated by <backtest / shadow / live review / adapter check>.
- **[OWNER-PENDING]** <what>: default <x> (plan open question); owner to confirm.
```

- **IDs** are `HAT-<AREA>-<nnn>`: `<AREA>` is the file's prefix in the index; `<nnn>` is three digits,
  based on the section number (§2 → 020-029; a large section uses a 100+ block). Decisions use their
  register code: `HAT-DEC-B14`, `HAT-DEC-T7`, `HAT-DEC-BD1`. IDs are never reused or renumbered;
  a dropped requirement keeps its ID with the statement "Withdrawn".
- **Sources**: MA = Master Architecture, BL = Business Logic, DS = Data Sources & Integration,
  SCH = Database Schema, PLAN = the implementation plan (conflicts C1-C12, open questions Q1-Q10),
  OWN-20OCT = owner build decisions of 20 Oct, OWN-FV = owner verification instruction.
- **`[A]`** marks a default the design docs call an *assumption*: it ships as the default and is
  validated as stated before real money depends on it.
- **`[OWNER-PENDING]`** marks a default chosen where the docs are silent (plan section 8); the owner
  confirms or replaces it.
- **Verification methods** (`TLA`, `PBT`, `DB`, `CONF`, `OPS`) are defined in
  [11-verification.md](11-verification.md) §1.

## Traceability

Requirement → PR → verification. The PR column comes from the plan's PR list; the verification
column names the model property, Hypothesis property, DB check or conformance check that proves the
requirement. When a PR lands a verification, it keeps the name here (or updates it) so the row points
to an artefact that exists.

<!-- traceability:start -->
| Requirement | Spec | PR | Verification |
| --- | --- | --- | --- |
| HAT-OVR-001 | [00](00-overview.md) | 2, 4 | CONF `market_id_on_market_tables`, PBT `calendar.session_from_reference_data` |
| HAT-OVR-002 | [00](00-overview.md) | 8, 13, 16 | CONF `mvp_seed_scope` |
| HAT-OVR-003 | [00](00-overview.md) | 2, 8 | CONF `import_graph_boundaries` |
| HAT-OVR-004 | [00](00-overview.md) | 2, 13, 15 | CONF `import_graph_boundaries`, TLA `ExecutionRouter!RiskBeforeRoute` |
| HAT-OVR-005 | [00](00-overview.md) | 2, 3 | CONF `hat_tables_prefixed_and_isolated` |
| HAT-OVR-006 | [00](00-overview.md) | 14 | CONF `single_feed_import_rule` |
| HAT-OVR-007 | [00](00-overview.md) | 2, 17 | TLA `JobScheduler!NoJobWhenDisabled`, CONF `hat_enabled_default_false` |
| HAT-OVR-040 | [00](00-overview.md) | 10, 13, 14 | CONF `no_duplicate_fakes_or_authorities` |
| HAT-OVR-041 | [00](00-overview.md) | K1 | CONF `imports.hat_decoupled_from_legacy`, CONF `imports.hat_suites_boot_no_legacy_app`, CONF `imports.legacy_does_not_import_hat` |
| HAT-OVR-050 | [00](00-overview.md) | 2-17 (per row: C1 5/6/8, C2 10, C3 17, C4 2/17, C5 13, C6 —, C7 4, C8 2/3, C9 2, C10 16, C11 3, C12 13) | CONF `conflict_resolutions_traced` |
| HAT-OVR-051 | [00](00-overview.md) | 2-17 | CONF `existing_suite_green` |
| HAT-OVR-052 | [00](00-overview.md) | 2, 5 | CONF `hat_data_root_paths` |
| HAT-OVR-060 | [00](00-overview.md) | 13, 15 | TLA `ExecutionRouter!LongOnly`, CONF `no_deferred_integrations` |
| HAT-OVR-061 | [00](00-overview.md) | 15, 16 | CONF `auto_mode_single_user` |
| HAT-DEC-T1 | [01](01-decisions.md) | 2, 3 | CONF `user_id_on_owned_tables` |
| HAT-DEC-T2 | [01](01-decisions.md) | 2 | CONF `hat_package_in_backend` |
| HAT-DEC-T3 | [01](01-decisions.md) | 16, 17, 38 | CONF `hat_routes_read_only`, CONF `hat_writes_guarded` |
| HAT-DEC-T4 | [01](01-decisions.md) | 2-17 | CONF `existing_suite_green` |
| HAT-DEC-T5 | [01](01-decisions.md) | 2, 17 | CONF `hat_runtime_requires_postgres` |
| HAT-DEC-T6 | [01](01-decisions.md) | 5 | PBT `ingest.parquet_roundtrip` |
| HAT-DEC-T7 | [01](01-decisions.md) | 2, 17 | TLA `JobScheduler!AtMostOnceSuccess` |
| HAT-DEC-T8 | [01](01-decisions.md) | 13 | TLA `OrderReconciliation!OneSessionOwner`, PBT `router.desk_calls_serialised` |
| HAT-DEC-T9 | [01](01-decisions.md) | 17 | OPS `host_static_ip_ups_failover` |
| HAT-DEC-T10 | [01](01-decisions.md) | 8 | PBT `config.yaml_round_trip`, PBT `config.hash_canonical` |
| HAT-DEC-T11 | [01](01-decisions.md) | 6 | PBT `features.no_lookahead` |
| HAT-DEC-T12 | [01](01-decisions.md) | 11, 12 | PBT `backtest.scan_parity` |
| HAT-DEC-T13 | [01](01-decisions.md) | 2, 17 | CONF `secrets_not_in_repo`, OPS `env_file_permissions` |
| HAT-DEC-T14 | [01](01-decisions.md) | 13, 16 | TLA `ExecutionRouter!NoEntryWithoutSession` |
| HAT-DEC-T15 | [01](01-decisions.md) | 16 | PBT `telegram.health_alert_for_every_failure` |
| HAT-DEC-T16 | [01](01-decisions.md) | 2-17 | CONF `spec_requirements_well_formed` |
| HAT-DEC-B1 | [01](01-decisions.md) | 15 | TLA `ExecutionRouter!ModeDomain` |
| HAT-DEC-B2 | [01](01-decisions.md) | 8, 15, 45 | CONF `seed_bots_confirm` |
| HAT-DEC-B3 | [01](01-decisions.md) | 8 | CONF `criterion_catalogue_complete` |
| HAT-DEC-B4 | [01](01-decisions.md) | 8 | PBT `scoring.threshold_gate` |
| HAT-DEC-B5 | [01](01-decisions.md) | 2, 8 | DB `governance.version_config_immutable` |
| HAT-DEC-B6 | [01](01-decisions.md) | 8 | CONF `seed_bots_match_spec_yaml` |
| HAT-DEC-B7 | [01](01-decisions.md) | 8 | PBT `criteria.vcp_contract` |
| HAT-DEC-B8 | [01](01-decisions.md) | 14, 15 | PBT `criteria.trigger_conjunction` |
| HAT-DEC-B9 | [01](01-decisions.md) | 14, 15 | PBT `bars.opening_range_fixed` |
| HAT-DEC-B10 | [01](01-decisions.md) | 14, 15 | PBT `announcements.match_window` |
| HAT-DEC-B11 | [01](01-decisions.md) | 10, 11, 15 | TLA `ExecutionRouter!GateOffNoEntry` |
| HAT-DEC-B12 | [01](01-decisions.md) | 10, 16 | TLA `ExecutionRouter!GateOffNoEntry`, CONF `no_override_command` |
| HAT-DEC-B13 | [01](01-decisions.md) | 10 | PBT `sizing.risk_bounded` |
| HAT-DEC-B14 | [01](01-decisions.md) | 10 | CONF `initial_risk_policy_seed` |
| HAT-DEC-B15 | [01](01-decisions.md) | 10, 46 | PBT `caps.global_open_risk` |
| HAT-DEC-B16 | [01](01-decisions.md) | 10 | PBT `exposure.ladder_transitions` |
| HAT-DEC-B17 | [01](01-decisions.md) | 13, 15 | TLA `StopPlacement!AlwaysProtected`, PBT `manage.real_fill_protected` |
| HAT-DEC-B18 | [01](01-decisions.md) | 11, 15 | TLA `JobScheduler!ExitsCloseInWindow` |
| HAT-DEC-B19 | [01](01-decisions.md) | 11 | PBT `exits.full_beats_partial` |
| HAT-DEC-B20 | [01](01-decisions.md) | 10 | TLA `ExecutionRouter!NoAddToOpenPosition` |
| HAT-DEC-B21 | [01](01-decisions.md) | 10 | PBT `caps.max_positions` |
| HAT-DEC-B22 | [01](01-decisions.md) | 8 | PBT `criteria.min_turnover_contract` |
| HAT-DEC-B23 | [01](01-decisions.md) | 8, 9 | PBT `universe.include_exclude` |
| HAT-DEC-B24 | [01](01-decisions.md) | 13 | TLA `OrderReconciliation!ProductIsCNC` |
| HAT-DEC-B25 | [01](01-decisions.md) | 8, 15 | PBT `criteria.max_chase_contract` |
| HAT-DEC-B26 | [01](01-decisions.md) | 10, 46 | TLA `KillSwitch!TripOnDrawdown`, PBT `caps.kill_thresholds` |
| HAT-DEC-B27 | [01](01-decisions.md) | 5, 7 | PBT `corporate_actions.adjusted_view` |
| HAT-DEC-I1 | [01](01-decisions.md) | 4, 7 | TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.batches_of_50` |
| HAT-DEC-I2 | [01](01-decisions.md) | 5 | PBT `bhavcopy.parsers_agree` |
| HAT-DEC-I3 | [01](01-decisions.md) | 14 | CONF `single_feed_import_rule` |
| HAT-DEC-I4 | [01](01-decisions.md) | 12, 41 | PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_plan_clamp` |
| HAT-DEC-I5 | [01](01-decisions.md) | 2 | PBT `criteria.fundamentals_point_in_time` |
| HAT-DEC-I6 | [01](01-decisions.md) | 14 | PBT `announcements.dedupe` |
| HAT-DEC-I7 | [01](01-decisions.md) | 13 | CONF `broker_interface_methods` |
| HAT-DEC-I8 | [01](01-decisions.md) | 13 | CONF `mvp_seed_scope` |
| HAT-DEC-I9 | [01](01-decisions.md) | 13 | TLA `ExecutionRouter!AutoNeedsGtt` |
| HAT-DEC-I10 | [01](01-decisions.md) | 13 | PBT `tags.encode_decode_roundtrip`, DB `runtime.order_tag_unique` |
| HAT-DEC-I11 | [01](01-decisions.md) | 16 | CONF `telegram_command_set` |
| HAT-DEC-I12 | [01](01-decisions.md) | 2, 16 | CONF `user_id_on_owned_tables` |
| HAT-DEC-I13 | [01](01-decisions.md) | 16 | PBT `telegram.unknown_chat_has_no_effect` |
| HAT-DEC-I14 | [01](01-decisions.md) | 7 | TLA `NightlyPipeline!NoScanAfterGateFail` |
| HAT-DEC-I15 | [01](01-decisions.md) | 3 | DB `events.append_only`, PBT `ingest.event_and_state_atomic` |
| HAT-DEC-I16 | [01](01-decisions.md) | 4 | CONF `mcp_endpoints_allowlist` |
| HAT-DEC-I17 | [01](01-decisions.md) | P2 | CONF `mvp_seed_scope` |
| HAT-DEC-PL1 | [01](01-decisions.md) | 2, 9 | CONF `bot_has_sleeve_columns` |
| HAT-DEC-PL2 | [01](01-decisions.md) | 9 | PBT `allocator.formula_and_bounds`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle` |
| HAT-DEC-PL3 | [01](01-decisions.md) | 9 | PBT `regime.classification_total` |
| HAT-DEC-PL4 | [01](01-decisions.md) | 9, 12 | TLA `SleeveLifecycle!AllowedTransitions`, PBT `dsr.bounds_and_monotonicity` |
| HAT-DEC-PL5 | [01](01-decisions.md) | 9 | DB `governance.one_live_version` |
| HAT-DEC-BD1 | [01](01-decisions.md) | 2 | CONF `hat_package_in_backend` |
| HAT-DEC-BD2 | [01](01-decisions.md) | 13 | CONF `fyers_adapter_wraps_existing` |
| HAT-DEC-BD3 | [01](01-decisions.md) | 2, 13 | CONF `no_deferred_integrations` |
| HAT-DEC-BD4 | [01](01-decisions.md) | 1 | CONF `old_docs_removed` |
| HAT-DEC-BD5 | [01](01-decisions.md) | 38 | CONF `hat_writes_guarded`, CONF `api_loopback_no_auth_wiring`, CONF `frontend_sends_write_header`, PBT `fyers_auth_routes.cross_site_write_refused`, PBT `fyers_auth_routes.token_for_another_account_refused` |
| HAT-DATA-010 | [02](02-data.md) | 4 | CONF `imports.engine_data_sources` |
| HAT-DATA-011 | [02](02-data.md) | 5, 7 | PBT `ingest.sources_same_schema` |
| HAT-DATA-020 | [02](02-data.md) | 4 | PBT `reference.instrument_identity_stable`, DB `reference.instrument_unique` |
| HAT-DATA-021 | [02](02-data.md) | 4 | PBT `reference.delisted_never_deleted` |
| HAT-DATA-022 | [02](02-data.md) | 4 | PBT `reference.symbol_history_resolves` |
| HAT-DATA-023 | [02](02-data.md) | 4 | PBT `reference.prefix_paging_complete` |
| HAT-DATA-024 | [02](02-data.md) | 4 | PBT `reference.fyers_symbol_roundtrip`, DB `reference.broker_symbol_unique` |
| HAT-DATA-025 | [02](02-data.md) | 4 | PBT `reference.current_band` |
| HAT-DATA-026 | [02](02-data.md) | 4 | PBT `reference.current_industry` |
| HAT-DATA-027 | [02](02-data.md) | 4, 17 | PBT `calendar.holiday_closed`, PBT `calendar.weekday_rows` |
| HAT-DATA-030 | [02](02-data.md) | 4 | CONF `imports.mcp_transport_only` |
| HAT-DATA-031 | [02](02-data.md) | 4, 7 | PBT `ingest.batches_le_50` |
| HAT-DATA-032 | [02](02-data.md) | 4, 7 | PBT `ingest.concurrency_bounded` |
| HAT-DATA-033 | [02](02-data.md) | 4 | PBT `mcp.retry_bounded` |
| HAT-DATA-034 | [02](02-data.md) | 4 | PBT `mcp.call_logged` |
| HAT-DATA-035 | [02](02-data.md) | 4 | PBT `mcp.no_raw_error_text` |
| HAT-DATA-036 | [02](02-data.md) | 4, 7 | PBT `ingest.date_mismatch_rejected` |
| HAT-DATA-040 | [02](02-data.md) | 7 | TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.probe_schedule` |
| HAT-DATA-041 | [02](02-data.md) | 7 | PBT `ingest.fetch_covers_universe` |
| HAT-DATA-042 | [02](02-data.md) | 7 | PBT `ingest.row_checks` |
| HAT-DATA-043 | [02](02-data.md) | 7, 17 | PBT `ingest.raw_write_once`, TLA `JobScheduler!AtMostOnceSuccess`, PBT `ingest.checks_kept_for_retry` |
| HAT-DATA-044 | [02](02-data.md) | 7 | PBT `ingest.index_snapshot_stored`, PBT `ingest.nightly_index_breadth` |
| HAT-DATA-045 | [02](02-data.md) | 7 | PBT `ingest.outcome_event` |
| HAT-DATA-046 | [02](02-data.md) | 7, 17, 33 | TLA `NightlyPipeline!NightlyPipelineOrder` |
| HAT-DATA-047 | [02](02-data.md) | 7 | TLA `NightlyPipeline!NoDataNoScan`, TLA `KillSwitch!ExitsContinue` |
| HAT-DATA-048 | [02](02-data.md) | 7 | PBT `ingest.call_budget` |
| HAT-DATA-049 | [02](02-data.md) | 7 | PBT `ingest.renames_from_nse_data` |
| HAT-DATA-050 | [02](02-data.md) | 7 | PBT `corporate_actions.flag_rule` |
| HAT-DATA-051 | [02](02-data.md) | 7 | PBT `corporate_actions.lookup_only_flagged` |
| HAT-DATA-052 | [02](02-data.md) | 5, 7 | PBT `corporate_actions.upsert_idempotent`, DB `reference.corporate_action_unique` |
| HAT-DATA-053 | [02](02-data.md) | 5 | PBT `corporate_actions.adjusted_view` |
| HAT-DATA-054 | [02](02-data.md) | 5 | PBT `corporate_actions.adjusted_volume` |
| HAT-DATA-055 | [02](02-data.md) | 5, 31 | PBT `corporate_actions.factor_parse`, PBT `history_build.archive_unparsed_reported` |
| HAT-DATA-060 | [02](02-data.md) | 7 | PBT `quality_gate.coverage_threshold`, TLA `KillSwitch!DataHealthManageOnly` |
| HAT-DATA-061 | [02](02-data.md) | 7 | PBT `quality_gate.ohlc_zero_tolerance` |
| HAT-DATA-062 | [02](02-data.md) | 7, 33 | PBT `quality_gate.big_moves_explained`, PBT `quality_gate.quarantine_one_mover`, PBT `quality_gate.quarantine_cap`, PBT `quality_gate.quarantine_release`, PBT `quality_gate.quarantine_lookup` |
| HAT-DATA-063 | [02](02-data.md) | 7 | PBT `quality_gate.index_snapshot_present` |
| HAT-DATA-064 | [02](02-data.md) | 7 | PBT `quality_gate.rows_written` |
| HAT-DATA-065 | [02](02-data.md) | 7, 10, 17 | TLA `KillSwitch!DataHealthManageOnly`, TLA `KillSwitch!ExitsContinue`, TLA `NightlyPipeline!NoScanAfterGateFail`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `quality_gate.rows_written` |
| HAT-DATA-070 | [02](02-data.md) | 6 | CONF `features.schema` |
| HAT-DATA-071 | [02](02-data.md) | 6 | PBT `features.no_lookahead` |
| HAT-DATA-072 | [02](02-data.md) | 6 | PBT `features.formulas` |
| HAT-DATA-073 | [02](02-data.md) | 6 | PBT `features.split_invariant` |
| HAT-DATA-074 | [02](02-data.md) | 6 | PBT `features.rs_pct` |
| HAT-DATA-075 | [02](02-data.md) | 6 | PBT `features.mom_rank` |
| HAT-DATA-076 | [02](02-data.md) | 6 | PBT `features.breadth` |
| HAT-DATA-077 | [02](02-data.md) | 6 | PBT `features.incremental_equals_full` |
| HAT-DATA-078 | [02](02-data.md) | 6 | CONF `imports.hat_polars_only` |
| HAT-DATA-080 | [02](02-data.md) | 5, 43 | CONF `store.data_root`, CONF `store.cli_root_after_env`, PBT `history_build.cli_root_and_report`, PBT `minute_history_cli.root_and_report` |
| HAT-DATA-081 | [02](02-data.md) | 5, 6, 12, 14 | CONF `store.layout` |
| HAT-DATA-082 | [02](02-data.md) | 5 | PBT `store.atomic_write` |
| HAT-DATA-083 | [02](02-data.md) | 5 | PBT `store.raw_immutable` |
| HAT-DATA-084 | [02](02-data.md) | 5 | PBT `store.bars_daily_unique` |
| HAT-DATA-085 | [02](02-data.md) | 5, 7 | PBT `ingest.sources_same_schema` |
| HAT-DATA-090 | [02](02-data.md) | 5 | PBT `bhavcopy.format_by_date` |
| HAT-DATA-091 | [02](02-data.md) | 5, 42 | PBT `bhavcopy.missing_file_is_holiday_candidate`, PBT `history_build.cli_passes_calendar_holidays` |
| HAT-DATA-092 | [02](02-data.md) | 5 | PBT `bhavcopy.resume_skips_done`, PBT `bhavcopy.rate_limited` |
| HAT-DATA-093 | [02](02-data.md) | 5 | PBT `bhavcopy.parsers_agree` |
| HAT-DATA-094 | [02](02-data.md) | 5 | PBT `corporate_actions.upsert_idempotent` |
| HAT-DATA-095 | [02](02-data.md) | 5, 44 | PBT `ingest.index_snapshot_stored`, PBT `history_build.mcp_index_source_ends_at_depth`, PBT `history_build.archive_fills_before_mcp_depth`, PBT `history_build.index_failure_isolated`, CONF `fixtures.index_history_no_data` |
| HAT-DATA-096 | [02](02-data.md) | 5, 43 | PBT `bhavcopy.validation_flags_mismatch`, PBT `history_build.cli_root_and_report` |
| HAT-DATA-097 | [02](02-data.md) | 5 | PBT `bhavcopy.traded_value_kept` |
| HAT-DATA-100 | [02](02-data.md) | 14 | PBT `announcements.poll_schedule` |
| HAT-DATA-101 | [02](02-data.md) | 14 | PBT `announcements.dedupe`, DB `reference.announcement_guid_unique` |
| HAT-DATA-102 | [02](02-data.md) | 14 | PBT `announcements.unmatched_kept` |
| HAT-DATA-103 | [02](02-data.md) | 14 | PBT `announcements.match_window` |
| HAT-DATA-104 | [02](02-data.md) | 14 | PBT `announcements.text_never_gates` |
| HAT-DATA-110 | [02](02-data.md) | 14 | CONF `imports.no_second_websocket` |
| HAT-DATA-111 | [02](02-data.md) | 14 | PBT `bars.listener_isolation` |
| HAT-DATA-112 | [02](02-data.md) | 14 | PBT `bars.subscription_set` |
| HAT-DATA-113 | [02](02-data.md) | 14 | PBT `bars.alignment` |
| HAT-DATA-114 | [02](02-data.md) | 14 | PBT `bars.opening_range_fixed`, PBT `bars.restore_matches_live` |
| HAT-DATA-115 | [02](02-data.md) | 14 | PBT `bars.volume_pace` |
| HAT-DATA-116 | [02](02-data.md) | 14, 15 | PBT `bars.staleness_trip`, PBT `bars.stale_feed_trips_health_kill`, TLA `ExecutionRouter!NoOrderWhenStale` |
| HAT-DATA-117 | [02](02-data.md) | 14 | PBT `bars.persisted_equals_live` |
| HAT-DATA-118 | [02](02-data.md) | 14 | PBT `bars.gap_pct` |
| HAT-DATA-119 | [02](02-data.md) | 12, 41, 43 | PBT `bars.history_matches_builder`, PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_budget`, PBT `bars.minute_history_resume`, PBT `bars.minute_history_plan_clamp`, PBT `minute_history_cli.root_and_report` |
| HAT-DATA-120 | [02](02-data.md) | 14 | PBT `gapscan.covers_universe` |
| HAT-DATA-121 | [02](02-data.md) | 14 | PBT `gapscan.filter` |
| HAT-DATA-122 | [02](02-data.md) | 14 | PBT `gapscan.timing_recorded`, OPS `gapscan.latency_observed` |
| HAT-DATA-123 | [02](02-data.md) | 14 | PBT `feed.session_one_socket`, PBT `feed.session_gives_up`, PBT `feed.session_close_stops_a_backing_off_runner`, PBT `feed.socket_factory`, PBT `feed.token_source`, PBT `feed.traceback_throttle` |
| HAT-DATA-124 | [02](02-data.md) | 14 | PBT `feed.tick_normalised`, PBT `feed.tick_flow` |
| HAT-DATA-125 | [02](02-data.md) | 14 | PBT `feed.registry_model`, PBT `feed.registry_concurrent` |
| HAT-DATA-126 | [02](02-data.md) | 14 | PBT `feed.provider_converges` |
| HAT-DATA-127 | [02](02-data.md) | 14 | PBT `feed.auth_hooks`, PBT `feed.off_hours_stops_a_backing_off_runner`, PBT `feed.monitor_survives_errors`, PBT `feed.token_check_off_loop`, PBT `feed.calendar_closed` |
| HAT-SCH-010 | [03](03-schema.md) | 2, 3 | CONF `schema.prefix_and_boundary` |
| HAT-SCH-011 | [03](03-schema.md) | 2 | PBT `ids.uuid7_ordered`, CONF `schema.key_types` |
| HAT-SCH-012 | [03](03-schema.md) | 2, 3 | CONF `schema.user_id_columns` |
| HAT-SCH-013 | [03](03-schema.md) | 2 | CONF `schema.market_id_columns` |
| HAT-SCH-014 | [03](03-schema.md) | 2, 3 | CONF `schema.money_types` |
| HAT-SCH-015 | [03](03-schema.md) | 2 | CONF `schema.time_types`, PBT `time.trade_date_ist` |
| HAT-SCH-016 | [03](03-schema.md) | 2, 8 | PBT `config.hash_canonical` |
| HAT-SCH-017 | [03](03-schema.md) | 3 | PBT `runtime.virtual_consistency` |
| HAT-SCH-020 | [03](03-schema.md) | 2 | CONF `schema.reference_columns` |
| HAT-SCH-021 | [03](03-schema.md) | 2 | CONF `seed.market_nse` |
| HAT-SCH-022 | [03](03-schema.md) | 2 | DB `reference.instrument_unique`, DB `reference.broker_symbol_unique`, DB `reference.corporate_action_unique`, DB `reference.announcement_guid_unique` |
| HAT-SCH-023 | [03](03-schema.md) | 2 | DB `reference.band_pct_check` |
| HAT-SCH-030 | [03](03-schema.md) | 2 | CONF `schema.strategy_risk_bot_columns` |
| HAT-SCH-031 | [03](03-schema.md) | 2 | DB `governance.domain_checks` |
| HAT-SCH-032 | [03](03-schema.md) | 2, 8 | CONF `schema.bot_no_risk_columns`, PBT `config.no_risk_keys` |
| HAT-SCH-033 | [03](03-schema.md) | 2, 10 | DB `governance.kill_switch_scope_check`, PBT `kill.current_state` |
| HAT-SCH-034 | [03](03-schema.md) | 2, 10 | DB `governance.exposure_checks` |
| HAT-SCH-040 | [03](03-schema.md) | 2 | CONF `schema.portfolio_columns` |
| HAT-SCH-041 | [03](03-schema.md) | 2 | DB `portfolio.unique_keys`, DB `portfolio.domain_checks` |
| HAT-SCH-042 | [03](03-schema.md) | 9 | PBT `universe.membership_intervals` |
| HAT-SCH-043 | [03](03-schema.md) | P2 (table in 2) | PBT `universe.fundamentals_point_in_time` |
| HAT-SCH-050 | [03](03-schema.md) | 3 | CONF `schema.runtime_columns` |
| HAT-SCH-051 | [03](03-schema.md) | 3 | DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal` |
| HAT-SCH-052 | [03](03-schema.md) | 3 | DB `runtime.signal_versions_not_null` |
| HAT-SCH-053 | [03](03-schema.md) | 3, 13 | DB `runtime.order_tag_unique`, PBT `tags.format` |
| HAT-SCH-054 | [03](03-schema.md) | 3 | DB `runtime.domain_checks` |
| HAT-SCH-055 | [03](03-schema.md) | 3 | DB `runtime.fill_unique` |
| HAT-SCH-056 | [03](03-schema.md) | 3, 16 | PBT `telegram.token_never_stored` |
| HAT-SCH-057 | [03](03-schema.md) | 3, 11 | DB `runtime.trade_result_once`, TLA `TradeLifecycle!ResultOnClose` |
| HAT-SCH-058 | [03](03-schema.md) | 3, 13 | DB `runtime.order_status_check`, TLA `OrderReconciliation!PersistBeforeCall` |
| HAT-SCH-059 | [03](03-schema.md) | 11 | DB `runtime.state_domain` |
| HAT-SCH-060 | [03](03-schema.md) | 3 | CONF `schema.event_columns`, DB `events.partitioned` |
| HAT-SCH-061 | [03](03-schema.md) | 3 | DB `events.append_only` |
| HAT-SCH-062 | [03](03-schema.md) | 3 | PBT `events.atomic_with_state` |
| HAT-SCH-063 | [03](03-schema.md) | 3, 9 | CONF `events.catalogue`, PBT `events.unknown_type_rejected` |
| HAT-SCH-064 | [03](03-schema.md) | 3, 9 | PBT `events.correlation`, PBT `events.correlation_links_the_signal` |
| HAT-SCH-065 | [03](03-schema.md) | 3, 16 | PBT `events.user_actor_chat_id` |
| HAT-SCH-066 | [03](03-schema.md) | 3, 17 | DB `events.partitions_ahead`, DB `events.partitions_job` |
| HAT-SCH-067 | [03](03-schema.md) | 3 | PBT `events.ids_increasing` |
| HAT-SCH-068 | [03](03-schema.md) | 3 | DB `events.append_only` |
| HAT-SCH-070 | [03](03-schema.md) | 2, 3, 16, 33 | CONF `schema.backtest_ops_columns` |
| HAT-SCH-071 | [03](03-schema.md) | 3, 12 | DB `ops.family_key_not_null` |
| HAT-SCH-072 | [03](03-schema.md) | 2 | DB `ops.job_run_unique`, TLA `JobScheduler!AtMostOnceSuccess` |
| HAT-SCH-073 | [03](03-schema.md) | 3, 10 | DB `ops.snapshot_unique` |
| HAT-SCH-074 | [03](03-schema.md) | 3, 16 | PBT `telegram.every_command_logged` |
| HAT-SCH-080 | [03](03-schema.md) | 2 | DB `governance.version_config_immutable` |
| HAT-SCH-081 | [03](03-schema.md) | 3, 11 | DB `governance.stop_only_rises`, TLA `StopPlacement!StopOnlyTightens` |
| HAT-SCH-082 | [03](03-schema.md) | 2 | DB `governance.one_live_version` |
| HAT-SCH-083 | [03](03-schema.md) | 2, 9 | DB `governance.live_requires_evidence`, TLA `SleeveLifecycle!NoLiveWithoutEvidence` |
| HAT-SCH-084 | [03](03-schema.md) | 2 | DB `governance.risk_increase_cooling_off`, PBT `risk_policy.cooling_off` |
| HAT-SCH-085 | [03](03-schema.md) | 2 | DB `governance.risk_version_immutable` |
| HAT-SCH-086 | [03](03-schema.md) | 10 | PBT `calendar.next_session_open`, TLA `ExecutionRouter!ModeChangeNextSession` |
| HAT-SCH-087 | [03](03-schema.md) | 2 | DB `governance.universe_version_immutable` |
| HAT-SCH-090 | [03](03-schema.md) | 3 | CONF `schema.indexes` |
| HAT-SCH-091 | [03](03-schema.md) | 3 | PBT `retention.purge_scope` |
| HAT-SCH-100 | [03](03-schema.md) | 2 | CONF `schema.portable_types`, CONF `schema.sqlite_create_all` |
| HAT-SCH-101 | [03](03-schema.md) | 2, 3 | DB `migration.ensure_idempotent` |
| HAT-SCH-102 | [03](03-schema.md) | 2, 3 | CONF `verification.db_rules_covered` |
| HAT-SCH-103 | [03](03-schema.md) | 2 | DB `migration.ensure_idempotent` |
| HAT-SCH-104 | [03](03-schema.md) | 2 | CONF `schema.sqlite_create_all` |
| HAT-SCH-105 | [03](03-schema.md) | 2, 17 | CONF `hat_runtime_requires_postgres` |
| HAT-SCH-106 | [03](03-schema.md) | 2, 3 | CONF `schema.prefix_and_boundary` |
| HAT-CRIT-010 | [04](04-criteria-and-bots.md) | 8, 11 | PBT `scoring.stage_order`, PBT `scan.watchlist_gates`, PBT `scan.no_universe_publishes_nothing`, DB `runtime.publish_serialised` |
| HAT-CRIT-011 | [04](04-criteria-and-bots.md) | 8 | PBT `config.stage_slots` |
| HAT-CRIT-020 | [04](04-criteria-and-bots.md) | 8 | PBT `config.instance_fields`, CONF `criterion_instance_schema` |
| HAT-CRIT-021 | [04](04-criteria-and-bots.md) | 8 | PBT `config.invalid_never_saves`, CONF `criterion_type_registry` |
| HAT-CRIT-022 | [04](04-criteria-and-bots.md) | 8 | PBT `scoring.disabled_is_noop` |
| HAT-CRIT-030 | [04](04-criteria-and-bots.md) | 8 | PBT `scoring.weighted_mean` |
| HAT-CRIT-031 | [04](04-criteria-and-bots.md) | 8 | PBT `scoring.threshold_gate` |
| HAT-CRIT-032 | [04](04-criteria-and-bots.md) | 8 | PBT `scoring.hard_short_circuit` |
| HAT-CRIT-040 | [04](04-criteria-and-bots.md) | 8, 57 | PBT `criteria.vcp_contract`, PBT `criteria.vcp_parity`, PBT `criteria.vcp_structure_narrows`, PBT `criteria.vcp_base_high` |
| HAT-CRIT-100 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.series_in_contract` |
| HAT-CRIT-101 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.min_turnover_contract` |
| HAT-CRIT-102 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.min_price_contract` |
| HAT-CRIT-103 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.price_band_allowed_contract` |
| HAT-CRIT-104 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.adr_min_contract` |
| HAT-CRIT-105 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.sma_above_contract` |
| HAT-CRIT-106 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.sma_rising_contract` |
| HAT-CRIT-107 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.pct_from_52w_low_contract` |
| HAT-CRIT-108 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.pct_from_52w_high_contract` |
| HAT-CRIT-109 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.rs_rank_contract` |
| HAT-CRIT-110 | [04](04-criteria-and-bots.md) | 8, 11 | PBT `criteria.momentum_rank_contract`, PBT `scan.rank_within_members` |
| HAT-CRIT-111 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.consolidation_contract` |
| HAT-CRIT-112 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.ma_surf_contract` |
| HAT-CRIT-113 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.prior_move_contract` |
| HAT-CRIT-114 | [04](04-criteria-and-bots.md) | 8 | PBT `criteria.earnings_growth_contract`, CONF `bot_seed_s1_matches_spec` |
| HAT-CRIT-115 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.gap_up_contract` |
| HAT-CRIT-116 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.volume_pace_contract` |
| HAT-CRIT-117 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.early_volume_contract` |
| HAT-CRIT-118 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.catalyst_attached_contract` |
| HAT-CRIT-119 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.price_breaks_level_contract` |
| HAT-CRIT-120 | [04](04-criteria-and-bots.md) | 8, 15, 56 | PBT `criteria.neglect_contract` |
| HAT-CRIT-121 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.adtr_filter_contract` |
| HAT-CRIT-122 | [04](04-criteria-and-bots.md) | 8, 15 | PBT `criteria.max_chase_contract`, TLA `ExecutionRouter!MaxChaseAlwaysApplied` |
| HAT-CRIT-123 | [04](04-criteria-and-bots.md) | 54 | PBT `criteria.close_vs_pivot_contract`, PBT `criteria.close_vs_pivot_stage`, PBT `scoring.stage_levels`, PBT `scoring.numeric_levels`, PBT `config.close_vs_pivot_needs_vcp` |
| HAT-BOT-050 | [04](04-criteria-and-bots.md) | 8, 11 | PBT `config.playbook_shape`, PBT `trades.shared_bot_cap` |
| HAT-BOT-051 | [04](04-criteria-and-bots.md) | 8, 15 | DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal` |
| HAT-BOT-052 | [04](04-criteria-and-bots.md) | 14, 15 | PBT `criteria.ep_candidate_bands` |
| HAT-BOT-053 | [04](04-criteria-and-bots.md) | 8, 11, 55 | PBT `regime.strategy_rules` |
| HAT-BOT-060 | [04](04-criteria-and-bots.md) | 8, 54 | CONF `bot_seed_s1_matches_spec` |
| HAT-BOT-061 | [04](04-criteria-and-bots.md) | 8, 45 | CONF `bot_seed_default_mode`, PBT `config.new_bot_mode_confirm` |
| HAT-BOT-062 | [04](04-criteria-and-bots.md) | 11 | PBT `stops.final_contraction_low` |
| HAT-BOT-070 | [04](04-criteria-and-bots.md) | 8, 55 | CONF `bot_seed_s2_matches_spec` |
| HAT-BOT-071 | [04](04-criteria-and-bots.md) | 15 | PBT `criteria.level_expression` |
| HAT-BOT-072 | [04](04-criteria-and-bots.md) | 8, 10 | PBT `config.no_risk_keys`, CONF `risk_policy_seed_bot_limits` |
| HAT-BOT-080 | [04](04-criteria-and-bots.md) | 2, 8 | PBT `config.save_creates_version`, DB `governance.version_config_immutable` |
| HAT-BOT-081 | [04](04-criteria-and-bots.md) | 8 | PBT `config.hash_canonical` |
| HAT-BOT-082 | [04](04-criteria-and-bots.md) | 8 | PBT `config.yaml_round_trip` |
| HAT-BOT-083 | [04](04-criteria-and-bots.md) | 11, 15 | PBT `config.trade_keeps_entry_version`, TLA `TradeLifecycle!VersionPinned` |
| HAT-BOT-084 | [04](04-criteria-and-bots.md) | 8, 9, 12, 35 | TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`, PBT `config.release_gate` |
| HAT-PORT-010 | [05](05-portfolio-regime-allocation.md) | 2, 9 | CONF `bot_sleeve_columns`, PBT `allocator.sleeve_count_agnostic` |
| HAT-PORT-011 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `allocator.cluster_cap` |
| HAT-PORT-020 | [05](05-portfolio-regime-allocation.md) | 2, 9 | DB `governance.universe_version_immutable`, DB `governance.one_live_version` |
| HAT-PORT-021 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `universe.point_in_time_members` |
| HAT-PORT-022 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `universe.include_exclude` |
| HAT-PORT-023 | [05](05-portfolio-regime-allocation.md) | 9, 11 | PBT `universe.leave_no_force_sell`, PBT `scan.no_row_for_removed` |
| HAT-PORT-030 | [05](05-portfolio-regime-allocation.md) | 9 | TLA `RegimeConfirmation!ThreeDayRule`, PBT `regime.three_day_confirmation` |
| HAT-PORT-031 | [05](05-portfolio-regime-allocation.md) | 6, 9 | PBT `regime.inputs` |
| HAT-PORT-032 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `regime.classification_total` |
| HAT-PORT-033 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `regime.multiplier_table` |
| HAT-PORT-034 | [05](05-portfolio-regime-allocation.md) | 9 | DB `portfolio.unique_keys`, TLA `SleeveLifecycle!RegimeNeverChangesLifecycle` |
| HAT-PORT-040 | [05](05-portfolio-regime-allocation.md) | 9, 46 | PBT `health.bands`, TLA `SleeveLifecycle!ProbationOnHealthZero` |
| HAT-PORT-050 | [05](05-portfolio-regime-allocation.md) | 9, 34 | PBT `allocator.formula_and_bounds`, PBT `allocator.virtual_allocation`, PBT `allocator.virtual_entries_sized` |
| HAT-PORT-051 | [05](05-portfolio-regime-allocation.md) | 9, 10 | PBT `allocator.bounds_from_policy`, DB `governance.risk_increase_cooling_off` |
| HAT-PORT-052 | [05](05-portfolio-regime-allocation.md) | 9, 10 | PBT `allocator.capital_and_risk` |
| HAT-PORT-053 | [05](05-portfolio-regime-allocation.md) | 9, 17 | TLA `DailyCycle!AllocateAfterRegime`, PBT `allocator.no_effect_on_open_positions` |
| HAT-PORT-054 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `allocator.smoothing` |
| HAT-PORT-055 | [05](05-portfolio-regime-allocation.md) | 9 | PBT `allocator.cash_remainder` |
| HAT-PORT-056 | [05](05-portfolio-regime-allocation.md) | 2, 9 | DB `portfolio.unique_keys`, TLA `JobScheduler!AtMostOnceSuccess` |
| HAT-PORT-060 | [05](05-portfolio-regime-allocation.md) | 9, 12, 35 | TLA `SleeveLifecycle!AllowedTransitions`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`, PBT `admin.first_deploy` |
| HAT-PORT-061 | [05](05-portfolio-regime-allocation.md) | 9, 12 | PBT `lifecycle.backtest_gate`, TLA `SleeveLifecycle!AllowedTransitions` |
| HAT-PORT-062 | [05](05-portfolio-regime-allocation.md) | 9 | TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.shadow_days` |
| HAT-PORT-063 | [05](05-portfolio-regime-allocation.md) | 9, 18 | PBT `lifecycle.probation_trigger`, TLA `SleeveLifecycle!ProbationOnHealthZero`, PBT `router.non_live_sleeve_routes_as_shadow` |
| HAT-PORT-064 | [05](05-portfolio-regime-allocation.md) | 9 | TLA `SleeveLifecycle!AllowedTransitions`, PBT `lifecycle.retire_gate` |
| HAT-PORT-065 | [05](05-portfolio-regime-allocation.md) | 9, 12 | PBT `lifecycle.discounted_benchmark` |
| HAT-PORT-066 | [05](05-portfolio-regime-allocation.md) | 9 | CONF `lifecycle_single_writer`, PBT `lifecycle.transition_records_its_event` |
| HAT-PORT-070 | [05](05-portfolio-regime-allocation.md) | P2 | PBT `regime_v2.no_lookahead` |
| HAT-PORT-071 | [05](05-portfolio-regime-allocation.md) | P2 | PBT `regime_v2.relabel_stable` |
| HAT-PORT-072 | [05](05-portfolio-regime-allocation.md) | P2 | PBT `regime_v2.multiplier_convex` |
| HAT-PORT-073 | [05](05-portfolio-regime-allocation.md) | P2 | TLA `SleeveLifecycle!NoLiveWithoutEvidence`, PBT `regime_v2.fallback_to_v1` |
| HAT-RISK-010 | [06](06-risk-and-governance.md) | 10 | CONF `risk.policy_schema_fields`, CONF `risk.no_risk_keys_in_strategy_schema`, PBT `sizing.uses_effective_policy` |
| HAT-RISK-011 | [06](06-risk-and-governance.md) | 10, 46 | CONF `risk.initial_seed_equals_spec`, PBT `policy.seed_supersedes_old_initial`, DB `migration.ensure_idempotent` |
| HAT-RISK-020 | [06](06-risk-and-governance.md) | 10 | PBT `sizing.formula_exact`, PBT `sizing.risk_bounded`, PBT `sizing.monotone` |
| HAT-RISK-021 | [06](06-risk-and-governance.md) | 10 | PBT `sizing.zero_skips_with_reason` |
| HAT-RISK-022 | [06](06-risk-and-governance.md) | 10 | PBT `sizing.stop_below_entry_required` |
| HAT-RISK-030 | [06](06-risk-and-governance.md) | 10, 19, 46 | PBT `caps.global_open_risk`, PBT `trades.global_open_risk_across_bots`, DB `runtime.owner_serialised` |
| HAT-RISK-031 | [06](06-risk-and-governance.md) | 10 | PBT `caps.max_positions` |
| HAT-RISK-032 | [06](06-risk-and-governance.md) | 10 | PBT `caps.industry` |
| HAT-RISK-033 | [06](06-risk-and-governance.md) | 10 | PBT `caps.daily_entries` |
| HAT-RISK-034 | [06](06-risk-and-governance.md) | 10 | PBT `caps.daily_stopouts` |
| HAT-RISK-035 | [06](06-risk-and-governance.md) | 10 | PBT `caps.exits_never_blocked` |
| HAT-RISK-040 | [06](06-risk-and-governance.md) | 10 | PBT `exposure.ladder_transitions` |
| HAT-RISK-041 | [06](06-risk-and-governance.md) | 10 | PBT `exposure.level_applies` |
| HAT-RISK-042 | [06](06-risk-and-governance.md) | 10 | PBT `exposure.virtual_ignored` |
| HAT-RISK-050 | [06](06-risk-and-governance.md) | 10, 11, 15 | PBT `caps.regime_gate_blocks_entries`, TLA `ExecutionRouter!GateOffNoEntry` |
| HAT-RISK-051 | [06](06-risk-and-governance.md) | 11, 15 | TLA `ExecutionRouter!GateFreshBeforeFirstEntry` |
| HAT-RISK-052 | [06](06-risk-and-governance.md) | 10, 16 | CONF `risk.no_gate_override_path`, TLA `ExecutionRouter!GateOffNoEntry` |
| HAT-RISK-060 | [06](06-risk-and-governance.md) | 10, 46 | PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly` |
| HAT-RISK-061 | [06](06-risk-and-governance.md) | 10, 46 | PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly` |
| HAT-RISK-062 | [06](06-risk-and-governance.md) | 10, 16 | TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!TripImpliesManageOnly` |
| HAT-RISK-063 | [06](06-risk-and-governance.md) | 10, 14, 17, 30 | TLA `KillSwitch!HealthAutoRearm`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `kill.kill_service_matches_model`, PBT `router.broker_health_follows_the_session` |
| HAT-RISK-064 | [06](06-risk-and-governance.md) | 10, 16 | TLA `KillSwitch!NoAutoRearm` |
| HAT-RISK-065 | [06](06-risk-and-governance.md) | 10, 15 | TLA `KillSwitch!ExitsContinue` |
| HAT-RISK-066 | [06](06-risk-and-governance.md) | 9, 33 | PBT `kill.reallocation_is_not_drawdown`, PBT `kill.snapshot_latest_allocation` |
| HAT-RISK-070 | [06](06-risk-and-governance.md) | 3, 10, 11 | TLA `StopPlacement!StopOnlyTightens`, DB `governance.stop_only_rises`, PBT `exits.never_lowers_stop` |
| HAT-RISK-071 | [06](06-risk-and-governance.md) | 10 | PBT `caps.no_add_to_loser` |
| HAT-RISK-072 | [06](06-risk-and-governance.md) | 10 | PBT `caps.no_pyramiding` |
| HAT-RISK-073 | [06](06-risk-and-governance.md) | 10, 15 | PBT `caps.entry_requires_stop`, TLA `ExecutionRouter!EntryHasStop` |
| HAT-RISK-074 | [06](06-risk-and-governance.md) | 10, 15 | TLA `ExecutionRouter!NoOrderWhenStale`, PBT `caps.stale_blocks_entry` |
| HAT-RISK-075 | [06](06-risk-and-governance.md) | 10, 15 | PBT `caps.market_hours_only`, TLA `ExecutionRouter!NoOrderOutsideHours` |
| HAT-RISK-076 | [06](06-risk-and-governance.md) | 10, 15 | PBT `caps.max_chase_always` |
| HAT-GOV-080 | [06](06-risk-and-governance.md) | 10, 16 | CONF `gov.lever_catalogue`, TLA `ExecutionRouter!UserCannotRaiseRisk` |
| HAT-GOV-081 | [06](06-risk-and-governance.md) | 10, 16 | TLA `KillSwitch!PauseBlocksEntriesOnly` |
| HAT-GOV-082 | [06](06-risk-and-governance.md) | 16 | TLA `KillSwitch!FlattenNeedsConfirm` |
| HAT-GOV-083 | [06](06-risk-and-governance.md) | 2, 9, 35 | TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`, PBT `config.release_gate` |
| HAT-GOV-084 | [06](06-risk-and-governance.md) | 10 | TLA `ExecutionRouter!ModeChangeNextSession`, PBT `router.mode_change_takes_effect_next_session` |
| HAT-GOV-085 | [06](06-risk-and-governance.md) | 45 | PBT `router.mode_gates_hold_bots_in_shadow`, PBT `router.open_settles_mode_changes`, PBT `broker.confirm_and_auto_need_gtt`, PBT `admin.first_deploy`, PBT `admin.refusal_writes_nothing` |
| HAT-GOV-090 | [06](06-risk-and-governance.md) | 10 | PBT `caps.policy_direction_classification` |
| HAT-GOV-091 | [06](06-risk-and-governance.md) | 2, 10, 12, 17, 34 | DB `governance.risk_increase_cooling_off`, PBT `caps.increase_gate`, PBT `dsr.gate_evidence`, PBT `caps.policy_change_inside_session_or_without_evidence_is_refused` |
| HAT-GOV-092 | [06](06-risk-and-governance.md) | 2 | DB `governance.risk_version_immutable` |
| HAT-GOV-093 | [06](06-risk-and-governance.md) | 9, 10 | PBT `caps.policy_direction_classification` |
| HAT-GOV-094 | [06](06-risk-and-governance.md) | 10 | PBT `caps.policy_change_emits_event` |
| HAT-RISK-100 | [06](06-risk-and-governance.md) | 10, 15 | CONF `risk.single_sizing_path`, TLA `ExecutionRouter!RiskBeforeRoute` |
| HAT-RISK-101 | [06](06-risk-and-governance.md) | 10 | PBT `caps.decision_total` |
| HAT-RISK-102 | [06](06-risk-and-governance.md) | 10 | PBT `caps.fail_closed` |
| HAT-RISK-103 | [06](06-risk-and-governance.md) | 10, K4 | CONF `risk.hat_safety_properties_pinned`, CONF `risk.no_manual_trading_path` |
| HAT-RISK-104 | [06](06-risk-and-governance.md) | 10, K1 | PBT `caps.hat_kill_is_authority`, CONF `risk.no_legacy_halt_read` |
| HAT-EXEC-010 | [07](07-execution-and-brokers.md) | 11, 15 | TLA `ExecutionRouter!ShadowNeverCallsBroker`, PBT `exits.shadow_rows_virtual` |
| HAT-EXEC-011 | [07](07-execution-and-brokers.md) | 8, 15, 45 | CONF `exec.default_mode_confirm` |
| HAT-EXEC-012 | [07](07-execution-and-brokers.md) | 13, 15, 25, 29 | TLA `ExecutionRouter!AutoNeedsGtt`, PBT `router.routes_by_mode`, PBT `router.a_stored_capability_never_enables_a_real_entry`, PBT `tags.refusals_happen_before_the_broker` |
| HAT-EXEC-013 | [07](07-execution-and-brokers.md) | 13 | PBT `tags.intent_product_cnc` |
| HAT-EXEC-014 | [07](07-execution-and-brokers.md) | 45 | PBT `router.auto_places_only_high_confidence` |
| HAT-EXEC-020 | [07](07-execution-and-brokers.md) | 15 | TLA `ExecutionRouter!RouteByMode`, TLA `ExecutionRouter!RiskBeforeRoute`, PBT `router.routes_by_mode`, PBT `router.refused_entry_is_not_sent` |
| HAT-EXEC-021 | [07](07-execution-and-brokers.md) | 3, 11, 15 | TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`, PBT `trades.retry_returns_existing_signal` |
| HAT-EXEC-022 | [07](07-execution-and-brokers.md) | 15 | TLA `ExecutionRouter!NoOrderWhenStale`, TLA `ExecutionRouter!NoOrderOutsideHours`, PBT `router.take_rechecks_max_chase` |
| HAT-EXEC-023 | [07](07-execution-and-brokers.md) | 15 | TLA `OrderReconciliation!RetryAtMostOnce`, PBT `router.rejected_entry_is_retried_once` |
| HAT-EXEC-030 | [07](07-execution-and-brokers.md) | 15 | TLA `ApprovalToken!NoTakeAfterExpiry` |
| HAT-EXEC-031 | [07](07-execution-and-brokers.md) | 15 | TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `caps.max_chase_always`, PBT `router.take_rechecks_max_chase` |
| HAT-EXEC-032 | [07](07-execution-and-brokers.md) | 15 | TLA `ExecutionRouter!SkipBecomesShadow`, PBT `router.skip_and_expiry_become_one_veto_trade` |
| HAT-EXEC-033 | [07](07-execution-and-brokers.md) | 15 | TLA `ExecutionRouter!ExitsNeverAsk` |
| HAT-EXEC-034 | [07](07-execution-and-brokers.md) | 15 | PBT `criteria.catalyst_required_in_confirm` |
| HAT-EXEC-040 | [07](07-execution-and-brokers.md) | 11 | TLA `TradeLifecycle!TypeOK`, TLA `TradeLifecycle!OnlyAllowedTransitions`, CONF `lifecycle_transitions_match_spec` |
| HAT-EXEC-041 | [07](07-execution-and-brokers.md) | 11, 15 | TLA `TradeLifecycle!EndingsTerminal`, TLA `TradeLifecycle!EventuallyEnds`, PBT `manage.entry_ended_by_broker` |
| HAT-EXEC-042 | [07](07-execution-and-brokers.md) | 3, 11 | TLA `TradeLifecycle!StateMatchesLog`, PBT `exits.replay_reproduces_state` |
| HAT-EXEC-043 | [07](07-execution-and-brokers.md) | 15, 16 | TLA `ExecutionRouter!RejectNoCard` |
| HAT-EXEC-044 | [07](07-execution-and-brokers.md) | 11 | TLA `TradeLifecycle!ShadowSameMachine` |
| HAT-EXEC-045 | [07](07-execution-and-brokers.md) | 11 | TLA `TradeLifecycle!PerBotCapHolds`, DB `runtime.per_bot_serialised`, PBT `trades.shared_bot_cap` |
| HAT-EXEC-050 | [07](07-execution-and-brokers.md) | 11, 54 | PBT `stops.final_contraction_low`, PBT `stops.contraction_stop_at_the_fill` |
| HAT-EXEC-051 | [07](07-execution-and-brokers.md) | 11 | PBT `stops.low_of_day` |
| HAT-EXEC-052 | [07](07-execution-and-brokers.md) | 11 | PBT `stops.custom_rules` |
| HAT-EXEC-053 | [07](07-execution-and-brokers.md) | 15 | PBT `stops.tick_size_slabs`, PBT `ops.daily_tick_matches_reference` |
| HAT-EXEC-060 | [07](07-execution-and-brokers.md) | 15, 20, 26 | TLA `StopPlacement!StopAfterFill`, TLA `StopPlacement!SharesTracked`, TLA `StopPlacement!RestCancelledOnExit`, TLA `StopPlacement!EmptiedCloses`, PBT `manage.real_fill_protected`, PBT `manage.a_resting_sale_is_never_stopped_over`, PBT `manage.entry_remainder_tracked`, PBT `router.fill_sync_cadence` |
| HAT-EXEC-061 | [07](07-execution-and-brokers.md) | 15 | TLA `StopPlacement!NoUnprotectedGap`, PBT `manage.swap_leaves_no_gap` |
| HAT-EXEC-062 | [07](07-execution-and-brokers.md) | 13, 15 | PBT `stops.gtt_limit_offset`, TLA `StopPlacement!GttUnfilledAlerted`, PBT `manage.gtt_trigger_handled` |
| HAT-EXEC-063 | [07](07-execution-and-brokers.md) | 15 | TLA `StopPlacement!NightlyVerifyCovers`, PBT `manage.verify_covers` |
| HAT-EXEC-064 | [07](07-execution-and-brokers.md) | 15, 20 | TLA `StopPlacement!BrokerStopMatchesCurrent`, TLA `StopPlacement!StopOnlyTightens`, PBT `manage.broker_stop_only_tightens`, PBT `manage.verify_covers` |
| HAT-EXEC-065 | [07](07-execution-and-brokers.md) | 18, 25 | TLA `StopPlacement!LostGttSettled`, TLA `StopPlacement!LostNeverBlocksSale`, PBT `manage.lost_answers_never_freeze`, PBT `manage.a_settled_answer_never_sells_twice`, PBT `manage.the_close_ends_only_settled_answers`, PBT `tags.the_gtt_book_reads_only_resting_stops`, PBT `tags.a_reserved_tag_is_sent_by_one_sender`, PBT `tags.a_lost_gtt_is_found_by_its_tag` |
| HAT-EXEC-066 | [07](07-execution-and-brokers.md) | 25, 27 | TLA `StopPlacement!RestingSold`, PBT `manage.gtt_resting_marketed` |
| HAT-EXEC-070 | [07](07-execution-and-brokers.md) | 11 | PBT `exits.full_beats_partial` |
| HAT-EXEC-071 | [07](07-execution-and-brokers.md) | 11 | PBT `exits.fire_once` |
| HAT-EXEC-072 | [07](07-execution-and-brokers.md) | 11 | PBT `exits.fraction_arithmetic` |
| HAT-EXEC-073 | [07](07-execution-and-brokers.md) | 11, 15 | PBT `exits.rule_semantics` |
| HAT-EXEC-074 | [07](07-execution-and-brokers.md) | 15, 17 | TLA `JobScheduler!ExitsCloseInWindow`, PBT `manage.no_sale_after_close`, PBT `ops.window_not_starved` |
| HAT-EXEC-075 | [07](07-execution-and-brokers.md) | 18 | PBT `manage.async_sale_rejection_retried` |
| HAT-BRK-080 | [07](07-execution-and-brokers.md) | 13 | CONF `brk.interface_methods`, CONF `brk.import_boundary` |
| HAT-BRK-081 | [07](07-execution-and-brokers.md) | 13, 18, 25 | PBT `tags.broker_errors_fail_closed`, PBT `tags.a_keyless_book_never_reads_as_absent`, PBT `fyers_broker.an_ok_reply_without_its_list_is_unreadable`, PBT `fyers_broker.a_circuit_opening_mid_place_is_not_sent`, PBT `fyers_broker.a_rejection_with_an_id_stays_rejected` |
| HAT-BRK-082 | [07](07-execution-and-brokers.md) | 13, 16, 23, 25 | TLA `ExecutionRouter!NoEntryWithoutSession`, PBT `broker.token_refused_is_session_expired`, PBT `broker.token_refused_on_a_read_ends_the_check`, PBT `fyers_auth.a_good_exchange_saves_once_and_reports_in_order`, PBT `broker.rate_limit_is_its_own_code`, PBT `bars.minute_history_rate_limit_stops_run`, PBT `tags.an_unreachable_broker_is_not_a_rejection` |
| HAT-BRK-090 | [07](07-execution-and-brokers.md) | 13, 25 | PBT `tags.fyers_payload_mapping`, PBT `tags.gtt_payloads`, PBT `tags.the_gtt_book_reads_only_resting_stops`, CONF `brk.recorded_writes` |
| HAT-BRK-091 | [07](07-execution-and-brokers.md) | 13, 25 | CONF `brk.fyers_capabilities`, CONF `brk.recorded_writes`, PBT `broker.the_fyers_adapter_unlocks_modes_on_an_active_account` |
| HAT-BRK-092 | [07](07-execution-and-brokers.md) | 13, 29 | TLA `OrderReconciliation!AccountActiveAfterCheck`, PBT `broker.session_check_refreshes_capabilities`, PBT `router.desks_start_with_the_adapters_capabilities` |
| HAT-BRK-093 | [07](07-execution-and-brokers.md) | 25 | PBT `fyers_ipv4.fyers_http_leaves_over_ipv4`, PBT `fyers_ipv4.is_fyers_host_exact`, CONF `brk.fyers_adapter_wraps_existing` |
| HAT-BRK-100 | [07](07-execution-and-brokers.md) | 13 | PBT `tags.shape_and_roundtrip`, DB `runtime.order_tag_unique` |
| HAT-BRK-101 | [07](07-execution-and-brokers.md) | 13 | TLA `OrderReconciliation!PersistBeforeCall`, TLA `OrderReconciliation!AtMostOneEntryPerSignal` |
| HAT-BRK-102 | [07](07-execution-and-brokers.md) | 13, 28 | TLA `OrderReconciliation!UnconfirmedResolvedByTag`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`, TLA `OrderReconciliation!LostEntryAdopted`, PBT `tags.a_timeout_is_looked_up_before_any_retry`, PBT `tags.idempotent_order_submission`, PBT `router.unresolved_entry_is_alerted_not_resent` |
| HAT-BRK-103 | [07](07-execution-and-brokers.md) | 25, 28 | CONF `brk.recorded_writes`, PBT `tags.a_tag_twice_at_the_broker_blocks_the_account`, PBT `manage.a_second_order_under_one_tag_is_tracked`, PBT `manage.a_dead_second_order_never_ends_the_entry`, PBT `tags.a_duplicated_tag_is_read_one_way` |
| HAT-BRK-110 | [07](07-execution-and-brokers.md) | 13, 15, 24 | TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.reconcile_before_first_order`, PBT `router.routine_reconcile_keeps_gate`, PBT `router.entry_gate_keyed_by_trade_date`, PBT `router.failed_first_reconcile_alerts_once_per_date` |
| HAT-BRK-111 | [07](07-execution-and-brokers.md) | 13, 24, 28 | TLA `OrderReconciliation!MismatchBlocksEntries`, PBT `manage.real_exits_never_blocked`, PBT `tags.held_quantity_nets_today`, PBT `router.older_read_never_overrides_newer` |
| HAT-BRK-112 | [07](07-execution-and-brokers.md) | 13, 24 | PBT `tags.reconcile_scope`, PBT `tags.unreadable_foreign_rows_skipped` |
| HAT-EXEC-120 | [07](07-execution-and-brokers.md) | 15 | PBT `bars.trigger_only_on_final_bars` |
| HAT-EXEC-121 | [07](07-execution-and-brokers.md) | 15 | PBT `criteria.trigger_conjunction` |
| HAT-EXEC-122 | [07](07-execution-and-brokers.md) | 15 | PBT `criteria.entry_filters_skip` |
| HAT-EXEC-130 | [07](07-execution-and-brokers.md) | 15, 16 | PBT `exits.veto_score` |
| HAT-EXEC-131 | [07](07-execution-and-brokers.md) | 16 | PBT `exits.veto_verdict_threshold` |
| HAT-EXEC-140 | [07](07-execution-and-brokers.md) | 21 | PBT `rescale.next_session_lookup` |
| HAT-EXEC-141 | [07](07-execution-and-brokers.md) | 21 | PBT `rescale.preserves_position`, PBT `rescale.applied_once`, PBT `rescale.factor_exact`, DB `governance.rescale_moves_stop` |
| HAT-EXEC-142 | [07](07-execution-and-brokers.md) | 21, 31 | PBT `rescale.pending_is_manage_only` |
| HAT-EXEC-143 | [07](07-execution-and-brokers.md) | 21 | PBT `rescale.broker_stop_follows` |
| HAT-EXEC-144 | [07](07-execution-and-brokers.md) | 31 | PBT `rescale.unhandled_is_manage_only`, PBT `manage.flatten_capped_at_holdings`, PBT `corporate_actions.factor_parse_unparseable_is_reported` |
| HAT-TG-005 | [08](08-telegram.md) | 16 | PBT `telegram.unknown_chat_has_no_effect` |
| HAT-TG-010 | [08](08-telegram.md) | 16 | CONF `telegram.no_webhook_no_listener` |
| HAT-TG-011 | [08](08-telegram.md) | 16, 32 | PBT `telegram.offset_commits_with_update` |
| HAT-TG-012 | [08](08-telegram.md) | 16 | PBT `telegram.every_command_logged` |
| HAT-TG-013 | [08](08-telegram.md) | 16 | PBT `telegram.user_events_attributed` |
| HAT-TG-014 | [08](08-telegram.md) | 16 | PBT `telegram.no_secret_in_output` |
| HAT-TG-020 | [08](08-telegram.md) | 16 | PBT `telegram.send_only_to_allowlist` |
| HAT-TG-021 | [08](08-telegram.md) | 16, 30 | TLA `JobScheduler!LoginPromptOrder`, PBT `telegram.login_prompt_links` |
| HAT-TG-022 | [08](08-telegram.md) | 16 | PBT `telegram.watchlist_digest_content`, PBT `telegram.watchlist_digest_once` |
| HAT-TG-023 | [08](08-telegram.md) | 16 | PBT `telegram.card_matches_signal` |
| HAT-TG-024 | [08](08-telegram.md) | 16 | TLA `JobScheduler!ReportsAfterClose`, PBT `telegram.report_content` |
| HAT-TG-030 | [08](08-telegram.md) | 16 | CONF `telegram.command_catalogue`, PBT `telegram.unknown_command_rejected` |
| HAT-TG-031 | [08](08-telegram.md) | 16 | PBT `telegram.no_free_text_orders`, CONF `telegram.no_order_import_in_text_path` |
| HAT-TG-032 | [08](08-telegram.md) | 16 (command), 10 (mode-change queue) | TLA `ExecutionRouter!ModeChangeNextSession`, PBT `telegram.mode_outside_hours` |
| HAT-TG-033 | [08](08-telegram.md) | 16 | TLA `ApprovalToken!SecondConfirmRequired`, PBT `telegram.second_confirm` |
| HAT-TG-034 | [08](08-telegram.md) | 16 (command), 10 (kill service) | TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!ExitsContinue` |
| HAT-TG-035 | [08](08-telegram.md) | 16 | TLA `KillSwitch!FlattenExitOnly`, PBT `telegram.flatten_logged` |
| HAT-TG-036 | [08](08-telegram.md) | 16 (command), 10 (service) | TLA `KillSwitch!PauseBlocksEntriesOnly` |
| HAT-TG-037 | [08](08-telegram.md) | 16 | PBT `telegram.read_commands_side_effect_free` |
| HAT-TG-038 | [08](08-telegram.md) | 45 | PBT `telegram.mode_names_a_bot_and_its_gate` |
| HAT-TG-040 | [08](08-telegram.md) | 16 | PBT `telegram.token_forgery_rejected` |
| HAT-TG-041 | [08](08-telegram.md) | 16 | TLA `ApprovalToken!SingleDecision` |
| HAT-TG-042 | [08](08-telegram.md) | 16 (tokens), 15 (expiry service) | TLA `ApprovalToken!NoTakeAfterExpiry`, PBT `telegram.expiry_window` |
| HAT-TG-043 | [08](08-telegram.md) | 16 (card), 15 (re-check) | TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `telegram.take_chase_recheck` |
| HAT-TG-044 | [08](08-telegram.md) | 16 | PBT `telegram.token_never_persisted` |
| HAT-TG-045 | [08](08-telegram.md) | 15 | TLA `ExecutionRouter!SkipBecomesShadow` |
| HAT-TG-050 | [08](08-telegram.md) | 3 | PBT `telegram.retention_purge` |
| HAT-BT-010 | [09](09-backtest.md) | 11, 12 | CONF `backtest_shares_engine_imports`, PBT `backtest.scan_parity` |
| HAT-BT-011 | [09](09-backtest.md) | 12 | PBT `backtest.no_lookahead` |
| HAT-BT-012 | [09](09-backtest.md) | 12 | PBT `backtest.survivorship_free`, PBT `backtest.market_point_in_time` |
| HAT-BT-013 | [09](09-backtest.md) | 12 | PBT `backtest.version_pinned` |
| HAT-BT-020 | [09](09-backtest.md) | 12 | PBT `backtest.entry_fill_proxy`, PBT `backtest.entry_fill_proxy_chase`, PBT `backtest.stop_on_instrument_tick` |
| HAT-BT-021 | [09](09-backtest.md) | 12 | PBT `backtest.exit_fill_proxy`, PBT `backtest.exit_fill_proxy_close` |
| HAT-BT-022 | [09](09-backtest.md) | 12 | PBT `costs.slippage_adverse`, PBT `costs.slippage_adverse_net`, PBT `costs.slippage_adverse_bounds` |
| HAT-BT-023 | [09](09-backtest.md) | 12 | PBT `costs.charges_contract`, PBT `costs.charges_contract_run`, PBT `costs.charges_contract_float_rates` |
| HAT-BT-024 | [09](09-backtest.md) | 12 | PBT `backtest.decimal_money` |
| HAT-BT-030 | [09](09-backtest.md) | 12 | PBT `dsr.trial_counting` |
| HAT-BT-031 | [09](09-backtest.md) | 12 | PBT `dsr.family_key` |
| HAT-BT-032 | [09](09-backtest.md) | 3, 12 | CONF `backtest_run_columns`, PBT `backtest.trades_match_metrics` |
| HAT-BT-040 | [09](09-backtest.md) | 11, 12 | PBT `backtest.r_and_expectancy` |
| HAT-BT-041 | [09](09-backtest.md) | 12 | PBT `backtest.metrics_bounds` |
| HAT-BT-042 | [09](09-backtest.md) | 12 | PBT `dsr.bounds_and_monotonicity`, PBT `dsr.bounds_and_monotonicity_in_sr`, PBT `dsr.bounds_and_monotonicity_first_trial`, PBT `dsr.bounds_and_monotonicity_undefined`, PBT `dsr.bounds_and_monotonicity_bad_denominator`, CONF `dsr_published_example` |
| HAT-BT-050 | [09](09-backtest.md) | 12 | PBT `backtest.deterministic` |
| HAT-BT-051 | [09](09-backtest.md) | 12 | CONF `backtest_golden_replay` |
| HAT-BT-060 | [09](09-backtest.md) | 9, 12 | PBT `lifecycle.backtest_gate`, PBT `dsr.evidence_current_trials`, TLA `SleeveLifecycle!NoLiveWithoutEvidence` |
| HAT-BT-061 | [09](09-backtest.md) | 2, 12 | DB `governance.live_requires_evidence`, PBT `backtest.version_status_requires_run` |
| HAT-OPS-001 | [10](10-ops.md) | 2 (setting), 17 (lifespan wiring) | PBT `ops.disabled_starts_nothing` |
| HAT-OPS-002 | [10](10-ops.md) | 17 | PBT `ops.refuses_non_postgres` |
| HAT-OPS-003 | [10](10-ops.md) | 17 (wiring), 13 (reconciliation) | TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.resumed_entry_waits_for_feed`, PBT `router.one_login_per_broker`, PBT `ops.stop_names_abandoned_jobs`, PBT `ops.stop_after_failed_start` |
| HAT-OPS-004 | [10](10-ops.md) | 17 | TLA `NightlyPipeline!ExitsIndependentOfPipeline` |
| HAT-OPS-010 | [10](10-ops.md) | 2 | TLA `JobScheduler!AtMostOnceSuccess`, DB `ops.job_run_unique` |
| HAT-OPS-011 | [10](10-ops.md) | 2 | TLA `JobScheduler!FailedJobRetryable`, PBT `ops.job_run_recording`, PBT `ops.retry_after_failed_run`, PBT `ops.chain_carries_past_midnight` |
| HAT-OPS-012 | [10](10-ops.md) | 17 | PBT `ops.calendar_skip` |
| HAT-OPS-013 | [10](10-ops.md) | 2 | PBT `ops.disabled_starts_nothing` |
| HAT-OPS-020 | [10](10-ops.md) | 17 | CONF `ops.job_registry` |
| HAT-OPS-021 | [10](10-ops.md) | 17, 33 | TLA `NightlyPipeline!NightlyPipelineOrder`, TLA `NightlyPipeline!AdjustIndependentOfGate`, TLA `NightlyPipeline!SnapshotEveryNight`, PBT `ops.entry_inputs_fail_closed` |
| HAT-OPS-022 | [10](10-ops.md) | 7 (logic), 17 (wiring) | TLA `NightlyPipeline!FallbackAt2200` |
| HAT-OPS-023 | [10](10-ops.md) | 14, 15 (logic), 17 (wiring) | TLA `DailyCycle!IntradayOrder`, PBT `router.entry_needs_live_session`, CONF `ops.route_matches_protocol` |
| HAT-OPS-024 | [10](10-ops.md) | 15 (logic), 17 (wiring) | TLA `StopPlacement!NightlyVerifyCovers` |
| HAT-OPS-030 | [10](10-ops.md) | 7, 10 | TLA `KillSwitch!DataHealthManageOnly`, TLA `NightlyPipeline!ExitsIndependentOfPipeline` |
| HAT-OPS-031 | [10](10-ops.md) | 13 (session), 16 (alert), 30 (08:30 start) | TLA `KillSwitch!DataHealthManageOnly`, PBT `ops.session_backoff_alert`, PBT `ops.no_outage_before_the_login_window` |
| HAT-OPS-032 | [10](10-ops.md) | 15 | TLA `OrderReconciliation!RetryAtMostOnce` |
| HAT-OPS-033 | [10](10-ops.md) | 3 (constraint), 15 (router) | TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day` |
| HAT-OPS-034 | [10](10-ops.md) | 15 | PBT `ops.close_gap_recorded` |
| HAT-OPS-040 | [10](10-ops.md) | 17 | CONF `ops.compose_services` |
| HAT-OPS-041 | [10](10-ops.md) | 17 | OPS `ops.host_checklist` |
| HAT-OPS-042 | [10](10-ops.md) | 17 | OPS `ops.deploy_window`, PBT `ops.deploy_window_guard` |
| HAT-OPS-043 | [10](10-ops.md) | 17 | OPS `ops.failover_drill` |
| HAT-OPS-050 | [10](10-ops.md) | 2, 17 | CONF `ops.no_secrets_in_repo`, OPS `ops.env_permissions` |
| HAT-OPS-051 | [10](10-ops.md) | 13 | CONF `ops.single_fyers_secret_store` |
| HAT-OPS-060 | [10](10-ops.md) | 17 | OPS `ops.backup_restore_drill`, PBT `ops.backup_manifest` |
| HAT-OPS-070 | [10](10-ops.md) | 2, 17 | PBT `ops.log_records_structured` |
| HAT-OPS-071 | [10](10-ops.md) | 16, 17 | PBT `telegram.report_content` |
| HAT-OPS-072 | [10](10-ops.md) | 16, 17 | PBT `ops.status_matches_db` |
| HAT-OPS-090 | [10](10-ops.md) | 17 | CONF `ops.runbook_complete` |
| HAT-OPS-091 | [10](10-ops.md) | 35 | PBT `admin.first_deploy`, PBT `admin.refusal_writes_nothing`, CONF `ops.admin_cli_calls_services` |
| HAT-VER-010 | [11](11-verification.md) | 1 | CONF `spec_requirements_well_formed` |
| HAT-VER-011 | [11](11-verification.md) | 2-17 | CONF `traceability_points_to_methods` |
| HAT-VER-020 | [11](11-verification.md) | 10, 11, 13, 15, 16, 17 | CONF `ci_runs_models_for_merged_targets` |
| HAT-VER-030 | [11](11-verification.md) | 2 (toolchain and runner), then each PR in the §3 table | CONF `tla_toolchain_pinned` |
| HAT-VER-031 | [11](11-verification.md) | each PR in the §3 table | CONF `tla_actions_mapped` |
| HAT-VER-032 | [11](11-verification.md) | each PR in the §3 table | CONF `tla_broken_variants_fail` |
| HAT-VER-033 | [11](11-verification.md) | each PR in the §3 table | CONF `tla_bounds_declared` |
| HAT-VER-034 | [11](11-verification.md) | 2-17 | CONF `verify_skill_findings_recorded` |
| HAT-VER-040 | [11](11-verification.md) | 2 (profile and dependency), then every PR with PBT properties | CONF `hypothesis_profile_registered` |
| HAT-VER-041 | [11](11-verification.md) | 9, 10, 12 | CONF `pbt_money_strategies_decimal` |
| HAT-VER-050 | [11](11-verification.md) | 2, 3 | CONF `db_checks_match_governance_table` |
| HAT-VER-060 | [11](11-verification.md) | 2 | CONF `ci_verification_job` |
| HAT-VER-070 | [11](11-verification.md) | the first PR that ships code in the skill's area (10, 13, 15, 2, 7, 9) | CONF `verify_skills_complete` |
| HAT-VER-071 | [11](11-verification.md) | same as HAT-VER-070 | CONF `verify_skill_sync_current` |
| HAT-VER-072 | [11](11-verification.md) | same as HAT-VER-070 | CONF `verify_skill_break_recorded` |
<!-- traceability:end -->
