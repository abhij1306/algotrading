---
name: verify-governance
description: How to run, read and update the checks behind Hire a Trader's database schema and governance rules (immutable versions, risk-policy cooling-off, one live version, kill switch and exposure domains, unique keys, idempotent schema migration), the foundation helpers (uuid7, trade dates, config hash, cooling-off rule) and the schema and boundary conformance checks. Use before and after any change to backend/app/hat/models, schema.py, ids.py, time.py, confighash.py, risk/policy.py, settings, paths or the import boundaries of backend/app/hat.
---

# verify-governance

Checks that the HAT database enforces its own rules (so no bug or manual edit can break them), that the SQLAlchemy models match `docs/specs/hat/03-schema.md`, and that the foundation helpers are correct for every input. Specs: `03-schema.md` §1 to §4, §7 to §10; `00-overview.md` (HAT-OVR-003/004/005/007/052); `01-decisions.md` (T1, T2, T5, T13, BD1, BD3, PL1); `06-risk-and-governance.md` (HAT-GOV-091/092); `11-verification.md` §4 and §5.

## Properties

- `DB governance.*` Postgres rejects: a changed config or `config_hash` of a published strategy or universe version (and returning one to draft), a second live version per strategy, a risk-policy edit other than setting `superseded_at`, a risk increase effective sooner than 168 hours after it was requested, a delete of a risk policy, domain violations (status, mode, state values), a kill-switch scope without its bot, and an exposure row outside L1 to L3 or with a negative counter. Harm: a live strategy changes under a running bot, or risk is raised with no cooling-off, and nothing stops it.
- `DB reference.*` Postgres rejects a duplicate instrument, broker symbol, corporate action or announcement GUID, and a price band outside its range. Harm: duplicated reference rows split history and double-apply corporate actions.
- `DB portfolio.*` Postgres rejects duplicate keys and out-of-domain values in universes, regime states, allocations and fundamentals. Harm: two weights or regimes for one day, so sizing is ambiguous.
- `DB migration.ensure_idempotent` Running `ensure_hat_schema` or `ensure_hat_columns` any number of times gives the catalog (tables, indexes, constraints, triggers) of running it once and loses no data, and `ensure_hat_schema` issues no DDL on SQLite. A database from before PR 45 (bots defaulting to shadow, no DDPI columns) gets the `confirm` default and the columns back, and a user still on the PR 10 policy seed is moved to the 06 §1 seed once (PR 46). Harm: a restart fails or duplicates triggers during startup.
- `PBT ids.uuid7_ordered` Every `uuid7()` is version 7, variant RFC 9562, and strictly increasing within a process, also across threads and inside one millisecond. Harm: ids collide or lose time order, breaking idempotency keys and index locality.
- `PBT time.trade_date_ist` For every UTC instant, `trade_date_of` is the Asia/Kolkata calendar date; naive instants are rejected. Harm: an evening job writes rows under tomorrow's or yesterday's date.
- `PBT config.hash_canonical` The config hash is a sha256 of canonical JSON: key order and spacing never change it, any value change does, equal hashes mean equal configs, and 0.0 and -0.0 hash the same. Harm: a strategy looks unchanged when it changed, or changed when it did not, so version immutability is meaningless.
- `PBT risk_policy.cooling_off` For every request and effective time, an increase is allowed only at 7 days (168 hours) or more, decreases at any time, and the app-level guard rejects the write the database would. Harm: risk is raised without the cooling-off gate.
- `CONF schema.*` The models equal the spec tables: columns, types, nullability, keys, foreign keys and unique sets read from `03-schema.md`; hat_ prefix and only hat_ targets; uuid keys; user_id and market_id columns; Numeric(18,4) money and no Float; timezone-aware timestamps; portable types only; `create_all` builds all 41 tables on SQLite. Harm: the code and the spec drift and a column the engine relies on is missing or of the wrong type.
- `CONF boundaries` The import graph keeps criteria free of broker, DB and UI modules and keeps engine, data and risk away from the broker layer; HAT code lives under `backend/app/hat`; data paths come from `HAT_DATA_ROOT`; no OpenAlgo; HAT refuses non-Postgres; the NSE market row is seeded once with the right values; no secret is committed and `.env.example` lists every HAT variable empty. Harm: a code path can place an order without the risk check, or a secret is pushed.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties -q --no-cov          # ids, time, config, risk policy
python -m pytest tests/hat/conformance -q --no-cov              # CONF schema, boundaries, harness
# database checks need Postgres (a throwaway database whose name ends in _test, never a real one):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  python -m pytest -m postgres tests/hat/db -q --no-cov
```

`tests/hat/db/test_db_broken_variants.py` removes each trigger, index and constraint inside a transaction and requires the matching check to fail: if you change a rule, a variant there must change too.

## Bug or model error

- A failing `DB` check names what the database accepted (`the database accepted: changing the config of a backtested version`). First check the rule is still in `PG_DDL` in `backend/app/hat/schema.py` or on the model: if a refactor removed or weakened it, that is a bug in the code, restore it. If the spec changed the rule, the check is out of date: change the check and its broken variant together.
- A failing `CONF schema.*` check names the table and column (`('hat_instrument', 'face_value')`) and what differs from the spec. The spec in `03-schema.md` is the source of truth: fix the model, or fix the spec in a separate, called-out change when the spec is wrong.
- A failing property prints the falsifying example (for instance the two instants a trade date got wrong, or the two configs whose hashes differ). Reproduce it with the real function. If the function is wrong, fix it (the `-0.0` finding was fixed this way); never narrow the strategy to avoid the example.
- A failing `boundaries` check prints the import chain from a forbidden module. Remove the import; do not add an allow-list entry.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- New table or column: change the model and `docs/specs/hat/03-schema.md` together; `CONF schema.*` reads the spec, so it fails until both agree. A new database rule goes in `PG_DDL` (with `CREATE OR REPLACE`), gets a `check_<name>` in `hat_db_rules.py`, a `test_<name>` in `tests/hat/db/` marked `postgres`, and an entry in `VARIANTS` in `test_db_broken_variants.py`.
- A column added after first release: add it to the model and to `ensure_hat_columns`.
- New property in a foundation helper: add `test_<prop>` to `tests/hat/properties/test_<area>.py` importing the real function.
- After re-confirming everything, update the sync commit and digest: `python backend/tests/hat/conformance/hat_conf_skills.py verify-governance` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/ids.py`: `Uuid7Generator`, `uuid7`
- `backend/app/hat/time.py`: `trade_date_of`, `as_utc`, `utc_now`
- `backend/app/hat/confighash.py`: `canonical_json`, `config_hash`
- `backend/app/hat/risk/policy.py`: `cooling_off_satisfied`, `require_cooling_off`
- `backend/app/hat/settings.py`: `load_settings`, `operator_settings`, `require_postgres`
- `backend/app/hat/schema.py`: `PG_DDL`, `ensure_hat_schema`, `ensure_hat_columns`, `seed_hat_reference`
- `backend/app/hat/models/_types.py`
- `backend/app/hat/models/reference.py`
- `backend/app/hat/models/strategy.py`
- `backend/app/hat/models/portfolio.py`
- `backend/app/hat/models/backtest.py`
- `backend/app/hat/models/ops.py`
- `backend/app/paths.py`: `HAT_DATA_ROOT`
- `backend/app/database.py`: `ensure_hat_database`
- `.env.example`
- `.gitignore`

Checks:
- `backend/tests/hat/properties/test_ids.py`
- `backend/tests/hat/properties/test_time.py`
- `backend/tests/hat/properties/test_config.py`
- `backend/tests/hat/properties/test_risk_policy.py`
- `backend/tests/hat/db/hat_db_rules.py`
- `backend/tests/hat/db/hat_db_builders.py`
- `backend/tests/hat/db/conftest.py`
- `backend/tests/hat/db/test_db_reference.py`
- `backend/tests/hat/db/test_db_governance.py`
- `backend/tests/hat/db/test_db_portfolio_ops.py`
- `backend/tests/hat/db/test_db_migration.py`
- `backend/tests/hat/db/test_db_broken_variants.py`
- `backend/tests/hat/conformance/test_conf_schema.py`
- `backend/tests/hat/conformance/test_conf_boundaries.py`
- `backend/tests/hat/conformance/hat_conf_model.py`
- `backend/tests/hat/conformance/hat_conf_repo.py`
- `backend/tests/hat/conformance/hat_conf_spec.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `80a1d16a957c7cbd856320b7cbc87201466e4c0e`
Covered digest: `3be5e1c81535f7a13ecfaeae9a010ce34add6d915cceed4ebc3df72eefeafb4a`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 73: the spec gains three event types (03 §6) and the release and DDPI rules of 04, 05, 06 and 10 their PR 73 lines; `test_config.py` checks the assign event (`verify-criteria`). No schema, foundation helper or boundary changed. Nothing was narrowed.
- PR 63: `engine/backtest.py` gained the slot order (`RunSpec.slot_order`, `backtest/slots.py`, recorded in `conventions` and `config_digest`) and `test_config.add_run` records `slot_order: time`; no governance check changed.
- PR 62: `engine/backtest.py` gained the recorded fill conventions (`conventions`, `config_digest` via `confighash.config_hash`, `RunSpec.entry_fill_rule` and `for_promotion`) and `test_config.add_run` flags its run for promotion; no governance check reads them (properties in `verify-backtest`).
- PR 48: `HatSettings` gains `mpp_resend` (`HAT_MPP_RESEND`, default off) and `mpp_page_after` (`HAT_MPP_PAGE_AFTER`, default 3); their parse is checked by `verify-execution`'s `mpp.resend_flag_parsed`. No governance check changed.
- PR 58: `risk/policy.py` gained `cooldown_sessions` (06 §1, HAT-RISK-036): a whole number in [0, 250], seed 0, optional in a stored document (reads as 0), and less permissive when higher. `OLD_SEED_POLICY` (the PR 10 seed) leaves it out, as that seed had none. The cooling-off functions are untouched; the properties are in verify-risk.
- PR 10 extended `risk/policy.py` (the policy record, direction classification and the evidence gate) without touching `cooling_off_satisfied` or `require_cooling_off`; the new rules are checked by `verify-risk`.
- The cooling-off CHECK in the database uses `interval '168 hours'` instead of `'7 days'`: a day interval depends on the session time zone around daylight-saving changes, 168 hours does not. The property is unchanged (seven days).
- The strategy and universe version triggers also forbid moving a published version back to `draft`, and forbid changing `config_hash` or `definition`: without this, config immutability could be bypassed by flipping status first. This widens the check.
- `CONF schema.user_id_columns` exempts `hat_kill_switch` from the "non-null FK chain to a user_id root" rule: an account-scope kill-switch row has no bot, so its `bot_id` is null, which contradicts the spec wording of HAT-SCH-012 (an `OWNER-PENDING` item). Recorded as a finding in the PR.
- `CONF schema.*` covered 28 tables in PR 2; PR 3 built the last 13 (the runtime tables, `hat_event` and the four ops tables), so `LATER` in `test_conf_schema.py` is now empty and the checks cover all 41. The runtime and event checks live in `verify-events`.
- `PG_DDL` gained the `hat_position_stop_guard` and `hat_event_no_update` triggers and the announcement index in PR 3; their checks are in `verify-events`. The catalog comparison in `check_migration__ensure_idempotent` now orders by name and definition (identical trigger names on event partitions made the old ordering unstable).
- `KNOWN_GAPS` gained two TLA names in PR 3 (`OrderReconciliation!AtMostOneEntryPerSignal`, `StopPlacement!StopOnlyTightens`): their models belong to PR 13 and PR 15. The list may still only shrink afterwards.
- `HAT-VER-011` (`traceability_points_to_methods`) lists the README rows whose method does not exist yet in `KNOWN_GAPS` instead of failing: `PBT criteria.fundamentals_point_in_time` (needs the criteria engine) and `CONF old_docs_removed` (a PR 1 item). The list may only shrink.
- `conflict_resolutions_traced` accepts C6 as untraced (`UNTRACED_RESOLUTIONS = {6}`): no requirement in files 01 to 10 cites `PLAN C6`, which is a spec gap, not a code gap.
- `PBT config.no_risk_keys`, `calendar.session_from_reference_data` and the other PBT names on PR 2 rows that need later features are not built here (their code does not exist yet).
- PR 8 added the config-model properties to `test_config.py` (they are checked by `verify-criteria`; the hash properties here are unchanged) and a non-vacuity assertion to `test_import_graph_boundaries` (the `app.hat.engine.criteria` package must exist in the graph; its scratch break is recorded in `verify-criteria`).
- PR 43 added `settings.operator_settings()` (loads `.env`, then `load_settings()`) for the command lines; `load_settings` is unchanged. Its checks are `verify-data`'s `store.cli_root_after_env` and `history_build.cli_root_and_report` and `verify-live-data`'s `minute_history_cli.root_and_report`; no check here was narrowed.
- PR 16 added `time.day_bounds` (the UTC window of an IST trade date), now used by `engine/trades.py` and the Telegram reads and reports instead of three copies; no check was narrowed.
- PR 33 added the `hat_quality_quarantine` table (`models/ops.py`; 03 §7, HAT-SCH-070): `schema.backtest_ops_columns` now covers nine ops tables and `schema.sqlite_create_all` expects 43 tables. Its `released_by` CHECK is new; the quarantine itself is checked by `verify-data`. Nothing was narrowed.
- PR 34 (design review item 8a, HAT-PORT-050): `hat_allocation_daily` gains the nullable `virtual_capital` and `virtual_risk_pct` (03 §portfolio); an existing Postgres database gets them from `ensure_hat_columns`. Checked by `verify-portfolio`'s allocator properties. Nothing was narrowed.
- PR 47: `HatSettings.exdate_gtt_check` (`HAT_EXDATE_GTT_CHECK`, default off); its break is recorded under `ops.ex_date_job_behind_flag` in `verify-scheduler`. Nothing was narrowed.
- PR 49: `HatSettings.entry_timeout_minutes` (`HAT_ENTRY_TIMEOUT_MINUTES`, a positive whole number, else 60); proved by `bars.live_flags_parsed` in `verify-live-data` (break recorded there). Nothing was narrowed.
- PR 50: `HatSettings.broker_calls_per_second`, `_per_minute`, `_per_day` (`HAT_BROKER_CALLS_PER_*`, defaults 8, 180, 5,000; `0` or `off` lifts the day cap only); proved by `fyers_broker.limits_from_settings`, whose breaks are recorded in `verify-execution`. Nothing was narrowed.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### DB governance.*

Break: the immutability trigger of `hat_strategy_version` rewired from `BEFORE UPDATE` to `BEFORE DELETE` in `PG_DDL`.

```
tests/hat/db/test_db_governance.py:27: in test_governance__version_config_immutable
tests/hat/db/hat_db_rules.py:576: in check_governance__version_config_immutable
E   AssertionError: the database accepted: changing the config of a backtested version
```

### DB reference.*

Break: the instrument unique key widened to include `symbol` in the model.

```
tests/hat/db/test_db_reference.py:12: in test_reference__instrument_unique
tests/hat/db/hat_db_rules.py:57: in check_reference__instrument_unique
E   AssertionError: the database accepted: a second instrument with the same (market, isin, series)
```

### DB portfolio.*

Break: `UniqueConstraint("universe_id", "version")` removed from `HatUniverseVersion`.

```
tests/hat/db/test_db_portfolio_ops.py:12: in test_portfolio__unique_keys
tests/hat/db/hat_db_rules.py:352: in check_portfolio__unique_keys
E   AssertionError: the database accepted: a duplicate (universe, version)
```

### DB migration.ensure_idempotent

Break: the first trigger in `PG_DDL` created with `CREATE TRIGGER` instead of `CREATE OR REPLACE TRIGGER`.

```
tests/hat/db/test_db_migration.py
E   psycopg2.errors.DuplicateObject: trigger "hat_strategy_version_immutable_guard" for relation "hat_strategy_version" already exists
tests/hat/db/hat_db_rules.py:858: in check_migration__ensure_idempotent
```

Break: in `backend/app/hat/schema.py`: `PG_DDL` no longer adds `hat_signal_state_check` and `hat_position_state_check` to an existing table.

```
FAILED tests/hat/db/test_db_migration.py::test_migration__ensure_idempotent
```

Break: (PR 45 review) `PG_DDL` without the `ALTER TABLE hat_bot ALTER COLUMN execution_mode SET DEFAULT 'confirm'` statement: an upgraded database keeps creating shadow bots.

```
E   AssertionError: assert False
E    +  where False = "'shadow'::text".startswith("'confirm'")
```

Break: (PR 46) `ensure_hat_database` no longer runs `ensure_hat_policies`: a user on the PR 10 seed keeps it.

```
E   AssertionError: assert [(1, 'initial', False)] == [(1, 'initial...itial', True)]
E     Right contains one more item: (2, 'initial', True)
```

### PBT ids.uuid7_ordered

Break: inside one millisecond the counter jumps to a random value instead of incrementing.

```
tests/hat/properties/test_ids.py:29: in test_uuid7_ordered
E   AssertionError: assert b'\x00\x00\x00\x00\x05\xbf}{...' < b'\x00\x00\x00\x00\x05\xbfv8...'
E   Failing test case: test_uuid7_ordered(
```

### PBT time.trade_date_ist

Break: `trade_date_of` takes the date in UTC.

```
tests/hat/properties/test_time.py:30: in test_trade_date_ist
E   assert datetime.date(2000, 1, 1) == datetime.date(2000, 1, 2)
E    +  where datetime.date(2000, 1, 1) = trade_date_of(datetime.datetime(2000, 1, 1, 19, 0, tzinfo=datetime.timezone.utc))
```

### PBT config.hash_canonical

Break: the normalisation of -0.0 removed from `canonical_json`.

```
tests/hat/properties/test_config.py:112: in test_hash_ignores_the_sign_of_zero
E   AssertionError: assert 'a8092b66271c...' == 'ba83b47c3d67...'
```

### PBT risk_policy.cooling_off

Break: `COOLING_OFF = timedelta(days=6)`.

```
tests/hat/properties/test_risk_policy.py:32: in test_cooling_off
E   AssertionError: assert True is False
E    +  where True = cooling_off_satisfied('increase', datetime.datetime(2000, 1, 1, 0, 0, tzinfo=datetime.timezone.utc), datetime.datetime(2000, 1, 7, 23, 59, 59, 999997, tzinfo=datetime.timezone.utc))
```

### CONF schema.*

Break: four separate breaks, each reverted after its check failed: `HatInstrument.face_value` changed from `Money` to `Float`; a trigger in `PG_DDL` renamed without the `hat_` prefix; `Timestamptz` made timezone-naive; `HatStrategy.user_id` made nullable.

```
test_schema__columns_match_spec: AssertionError: ('hat_instrument', 'face_value')   assert 'Float()' == 'numeric(18,4)'
test_schema__prefix_and_boundary: AssertionError: ('strategy_version_immutable_guard', 'CREATE OR REPLACE TRIGGER ...')
test_schema__time_types: AssertionError: ('hat_job_run', 'started_at')
test_schema__user_id_columns: AssertionError: hat_strategy
```

### CONF boundaries

Break: five separate breaks, each reverted after its check failed: `app.hat.risk.policy` imports `app.brokers.base`; a made-up non-empty value given to the Telegram token variable in `.env.example`; the NSE seed timezone changed to `UTC`; a `data_system/hat` literal in a HAT module; `import openalgo_client` in a HAT module.

```
test_import_graph_boundaries: AssertionError: app.hat.risk   {'app.hat.risk.policy': ['app.hat.risk.policy', 'app.brokers.base']}
test_ops_no_secrets_in_repo: AssertionError: ('.env.example', 'HAT_TELEGRAM_TOKEN', <the value>)
test_seed_market_nse: AssertionError: assert ('UTC', 'INR') == ('Asia/Kolkata', 'INR')
test_hat_data_root_paths: AssertionError: .../app/hat/ops/jobs.py   assert 'data_system' not in <the module text>
test_no_deferred_integrations: Failed: .../app/hat/ops/jobs.py: import openalgo_client
```
