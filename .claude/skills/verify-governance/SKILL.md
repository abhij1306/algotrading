---
name: verify-governance
description: How to run, read and update the checks behind Hire a Trader's database schema and governance rules (immutable versions, risk-policy cooling-off, one live version, kill switch and exposure domains, unique keys, idempotent schema migration), the foundation helpers (uuid7, trade dates, config hash, cooling-off rule) and the schema and boundary conformance checks. Use before and after any change to backend/app/hat/models, schema.py, ids.py, time.py, confighash.py, risk/policy.py, settings, paths or the import boundaries of backend/app/hat.
---

# verify-governance

Checks that the HAT database enforces its own rules (so no bug or manual edit can break them), that the SQLAlchemy models follow the schema rules, and that the foundation helpers are correct for every input. Requirement IDs (index: `docs/invariants.md`):; (T1, T2, T5, T13, BD1, BD3, PL1);.

## Properties

- `DB governance.*` Postgres rejects: a changed config or `config_hash` of a published strategy or universe version (and returning one to draft), a second live version per strategy, a risk-policy edit other than setting `superseded_at`, a risk increase effective sooner than 168 hours after it was requested, a delete of a risk policy, domain violations (status, mode, state values), a kill-switch scope without its bot, and an exposure row outside L1 to L3 or with a negative counter. Harm: a live strategy changes under a running bot, or risk is raised with no cooling-off, and nothing stops it.
- `DB reference.*` Postgres rejects a duplicate instrument, broker symbol, corporate action or announcement GUID, and a price band outside its range. Harm: duplicated reference rows split history and double-apply corporate actions.
- `DB portfolio.*` Postgres rejects duplicate keys and out-of-domain values in universes, regime states, allocations and fundamentals. Harm: two weights or regimes for one day, so sizing is ambiguous.
- `DB migration.ensure_idempotent` Running `ensure_hat_schema` or `ensure_hat_columns` any number of times gives the catalog (tables, indexes, constraints, triggers) of running it once and loses no data, and `ensure_hat_schema` issues no DDL on SQLite. A database from before (bots defaulting to shadow, no DDPI columns) gets the `confirm` default and the columns back, and a user still on the policy seed is moved to the current seed once. Harm: a restart fails or duplicates triggers during startup.
- `PBT ids.uuid7_ordered` Every `uuid7()` is version 7, variant RFC 9562, and strictly increasing within a process, also across threads and inside one millisecond. Harm: ids collide or lose time order, breaking idempotency keys and index locality.
- `PBT time.trade_date_ist` For every UTC instant, `trade_date_of` is the Asia/Kolkata calendar date; naive instants are rejected. Harm: an evening job writes rows under tomorrow's or yesterday's date.
- `PBT config.hash_canonical` The config hash is a sha256 of canonical JSON: key order and spacing never change it, any value change does, equal hashes mean equal configs, and 0.0 and -0.0 hash the same. Harm: a strategy looks unchanged when it changed, or changed when it did not, so version immutability is meaningless.
- `PBT risk_policy.cooling_off` For every request and effective time, an increase is allowed only at 7 days (168 hours) or more, decreases at any time, and the app-level guard rejects the write the database would. Harm: risk is raised without the cooling-off gate.
- `CONF schema.*` The models follow the schema rules: hat_ prefix and only hat_ targets; uuid keys; user_id and market_id columns; Numeric(18,4) money and no Float; timezone-aware timestamps; portable types only; `create_all` builds all 41 tables on SQLite. Harm: a column the engine relies on is of the wrong type or a table escapes the rules.
- `CONF boundaries` The import graph keeps criteria free of broker, DB and UI modules and keeps engine, data and risk away from the broker layer; HAT code lives under `backend/app/hat`; data paths come from `HAT_DATA_ROOT`; no OpenAlgo; HAT refuses non-Postgres; the NSE market row is seeded once with the right values; no secret is committed and `.env.example` lists every HAT variable empty. Harm: a code path can place an order without the risk check, or a secret is pushed.
- `CONF no_private_imports` No module under `app/hat` imports an underscore name from another HAT module (`from x import _name`, relative or absolute) or reads one off a HAT module it imported (`x._name`); its own private names and non-HAT ones pass. Harm: `live_trades` took seven private helpers of the shadow engine, so a shadow refactor could change real-position accounting with nothing at the import level to say so.

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
- A failing `CONF schema.*` check names the table and column (`('hat_instrument', 'face_value')`) and what differs. The models are the source of truth: fix the model, or the check in a separate, called-out change.
- A failing property prints the falsifying example (for instance the two instants a trade date got wrong, or the two configs whose hashes differ). Reproduce it with the real function. If the function is wrong, fix it (the `-0.0` finding was fixed this way); never narrow the strategy to avoid the example.
- A failing `boundaries` check prints the import chain from a forbidden module. Remove the import; do not add an allow-list entry.
- Staleness: if `test_verify_skill_sync_current` says this skill is stale, a covered file changed. Re-run everything above, update the skill if behaviour changed, then refresh the digest.

## Updating

- New table or column: change the model; the models are the source of truth for columns. A new database rule goes in `PG_DDL` (with `CREATE OR REPLACE`), gets a `check_<name>` in `hat_db_rules.py`, a `test_<name>` in `tests/hat/db/` marked `postgres`, and an entry in `VARIANTS` in `test_db_broken_variants.py`.
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
- `backend/tests/hat/conformance/hat_conf_repo.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `80a1d16a957c7cbd856320b7cbc87201466e4c0e`
Covered digest: `58b76bed4f125c658071adf8253761699099bdfc5fd58884e2ed56c3820f468a`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code, then the break was removed and the check returned to passing.


### CONF no_private_imports

Break: `engine/live_trades.py` and `execution/submit.py` as on main (`from .trades import _day_bounds, _emit, _is_veto, _log_ignored, _raise_stop, _signal_and_bot, _write_result`; `trades._day_bounds(...)`).

```
E   AssertionError: assert {'engine/live..._day_bounds']} == {}
E     Left contains 2 more items:
E     {'engine/live_trades.py': ['line 44: from app.hat.engine.trades import _day_bounds',
```

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
test_schema__money_types: AssertionError: ('hat_instrument', 'face_value')   assert not True
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
