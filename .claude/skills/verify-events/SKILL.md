---
name: verify-events
description: How to run, read and update the checks behind Hire a Trader's runtime tables, the append-only event log and its monthly partitions, the invariant-1 stop trigger, the event catalogue and `record()`, the virtual-flag rule, and the Telegram/command-log retention purge. Use before and after any change to backend/app/hat/events.py, runtime.py, ops/partitions.py, ops/retention.py, models/events.py, models/runtime.py, the hat_position or hat_event triggers in schema.py, or the event catalogue in docs/specs/hat/03-schema.md.
---

# verify-events

Checks that the database refuses what the runtime tables must never hold (a second signal per day, a duplicate order tag or fill, a lowered stop, an edited or deleted event), that `events.record` writes in the caller's transaction and only the catalogued types, that monthly event partitions always exist ahead of time, and that the purge touches only the chat logs. Specs: `docs/specs/hat/03-schema.md` §5, §6, §8 (HAT-SCH-050..058, 060..068, 073, 081, 090, 091), `01-decisions.md` (I15), `06-risk-and-governance.md` (HAT-RISK-070), `08-telegram.md` (HAT-TG-050), `10-ops.md` §2 (job `events.partitions`).

## Properties

- `DB runtime.*` Postgres rejects a second `hat_signal` for one (bot, playbook, instrument, trade_date) (also when two transactions race), a signal with a null or dangling strategy or risk-policy version, an order purpose, side, status, approval decision, signal mode or quantity outside its domain, a second order with the same tag, a second fill for one (order, broker trade), and a second trade result for one position. Harm: a duplicated trigger opens two positions, an order of unknown purpose escapes the exit-only paths, a re-polled fill doubles a position.
- `DB ops.snapshot_unique` Postgres rejects a second `hat_daily_bot_snapshot` for one bot and day and an exposure level outside L1 to L3. Harm: the drawdown kill switch reads an ambiguous equity.
- `DB governance.stop_only_rises` For every pair (old, new) of stops, an UPDATE of `hat_position.current_stop` raises when new is below old and succeeds when it is equal or higher, with the row unchanged after a refusal. Harm: a bug widens a stop and a planned small loss becomes a large one (invariant 1).
- `DB events.append_only` Every UPDATE (any column) and every DELETE on `hat_event`, one row or all rows, in any partition, raises `hat_event is append-only` and leaves the log unchanged. Harm: the audit trail can be rewritten.
- `DB events.partitioned` `hat_event` is a RANGE (recorded_at) partitioned table with the specified columns, an identity id that strictly increases, primary key (id, recorded_at), and the actor CHECK. Harm: one unbounded table, or a log that cannot hold what replay needs.
- `DB events.partitions_job` The `events.partitions` job (registered every day at 00:30 by PR 17): an event two months past a day with no partitions is refused before the job runs for that day and lands in its month's partition after; a run that leaves less than one month of partitions ahead alerts `events.partitions_short` and fails with that code. Harm: the partitions run out unseen and, on the first day of an uncovered month, every trade write fails.
- `DB events.partitions_ahead` After the ensure step runs at any date d, partitions cover d's month and the two after it, and an event at any instant (including the first and last microsecond of a month) lands in the partition of its UTC month. Harm: on the first day of an uncovered month every event insert fails, and with it every state change.
- `PBT events.atomic_with_state` For every sequence of stop moves failing at any point before commit (before the state write, between the two writes, after both, by a refused event type, or rolled back by the caller), the state change and its event are both stored or both absent. Harm: the log says a stop moved when it did not.
- `PBT events.unknown_type_rejected` For every string outside the catalogue (random text and near misses such as case changes and suffixes), `record()` raises and writes nothing; every catalogue type records. Harm: a typo creates an event no replay or discipline metric reads.
- `PBT events.correlation` For every attempted event stream, each stored event has a known aggregate type, every Signals, Orders and Positions event (watchlist.published aside) carries a correlation id (the signal's own id on a signal aggregate), and every causation id names an earlier stored event; an unknown causation id is refused. Harm: a trade's history cannot be reassembled.
- `PBT events.ids_increasing` On SQLite, for every sequence of `record()` calls across sessions, commits and rollbacks, ids are unique and strictly increase in call order. (Postgres identity ids are checked by `DB events.partitioned`.) Harm: replay order differs from the order things happened.
- `PBT events.partition_routing` Every instant lies in the half-open range of exactly one month's partition, consecutive ranges meet with no gap, and the months to create are the month of d and the following ones. Harm: an instant routed to no partition or two.
- `PBT runtime.virtual_consistency` For every set of trades written through `position_is_virtual` and `child_is_virtual`, a fill has its order's `is_virtual`, an order its position's, and a position is virtual exactly when its signal's mode was shadow or it was skipped in confirm mode; an unknown mode raises. Harm: virtual trades count as real P&L or real positions lose their broker stops.
- `PBT retention.purge_scope` For every set of rows and run date, a Telegram message or command-log row is gone exactly when it is older than one year (IST date), rows of every other table and files under the data root are untouched, and a second run deletes nothing. Harm: trade history is deleted or chat logs outlive their year.
- `PBT telegram.retention_purge` Through the real `run_job`, the `retention.purge` job leaves exactly the last year of both chat tables, records one success, and a repeat is a no-op. Harm: as above, with a job that runs twice.
- `PBT events.user_actor_chat_id` `record()` refuses an event with `actor = 'user'` whose payload has no integer `chat_id` (a bool is not one), takes one that has it, and takes any other actor without it; `actor` is typed as the four actors. Harm: a user action in the log cannot be traced to the chat that sent it (HAT-SCH-065).
- `PBT ingest.event_and_state_atomic` An ingest outcome row and its `ingest.*` / `quality.failed` event are both stored or both absent for every failure point before commit. Harm: the night's ingest status and the audit log disagree.
- `CONF events.catalogue` The event types in `app/hat/events.py`, group by group, equal the catalogue table in the spec, and the actors and aggregate types equal the DDL. Harm: code and audits disagree on what can happen.
- `CONF schema.runtime_columns, event_columns, indexes` The eight runtime tables, the seven ops tables and `hat_event` equal the spec columns, types, nullability, keys and uniques, and every §9 index exists with its columns, uniqueness, order and partial predicate. Harm: a trade cannot be replayed, or reconciliation by tag degrades to a full scan.
- `PBT events.correlation_links_the_signal` `record()` refuses an Orders or Positions event whose correlation id is not the signal id of the `hat_order` or `hat_position` row it is about, or that names no such row, and writes nothing; an event that names its own signal is stored. Harm: an order's events are filed under another trade and its history cannot be replayed.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties -q --no-cov          # all PBT, incl. the events ones
python -m pytest tests/hat/conformance -q --no-cov              # CONF, no database needed
# database checks need Postgres (a throwaway database whose name ends in _test, never a real one):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  python -m pytest -m postgres tests/hat/db -q --no-cov
```

`tests/hat/db/test_db_broken_variants.py` removes each trigger, index, constraint or partition inside a transaction and requires the matching check to fail; a faulty ensure step (too few months, wrong month boundary) must fail `events.partitions_ahead`.

## Bug or model error

- A failing `DB` check names what the database accepted or refused (`the database accepted: a second order with the same tag`, `the database refused: keeping or raising the stop from 95 to 95`). First check the rule is still in `PG_DDL` in `backend/app/hat/schema.py` or on the model. A refactor that removed or weakened it is a bug in the code: restore it. If the spec changed the rule, change the check and its broken variant together.
- A failing `events.partitions_ahead` names the month with no partition or the partition an event landed in. The check reads "two months ahead" from the spec, not from the code constant: if it fails after `MONTHS_AHEAD` changed, the constant is wrong, not the check.
- A failing property prints the falsifying example and a `reproduce_failure` blob. Reproduce it with the real `record()`; never narrow a strategy to avoid the example.
- A `CONF schema.*` failure names the table and column. The spec is the source of truth: fix the model, or fix the spec in a separate, called-out change.
- The SQLite id emulation (`_next_sqlite_id`) is test-only; a failure of `ids_increasing` there says nothing about Postgres identity ids.

## Updating

- A new event type: change the catalogue table in `03-schema.md` (a spec change) and `EVENT_GROUPS` in `events.py` together; `CONF events.catalogue` fails until both agree.
- A new runtime table or column: change the model and `03-schema.md` together; add a `check_` in `tests/hat/db/hat_db_runtime.py` for any database rule, a `test_` in `test_db_runtime.py` or `test_db_events.py`, and an entry in `VARIANTS_RUNTIME` in `test_db_broken_variants.py`.
- After re-confirming everything, update the sync commit and digest: `python backend/tests/hat/conformance/hat_conf_skills.py verify-events` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/events.py`: `record`, `EVENT_GROUPS`, `EventError`
- `backend/app/hat/runtime.py`: `position_is_virtual`, `child_is_virtual`
- `backend/app/hat/ops/partitions.py`: `ensure_event_partitions`, `events_partitions`, `existing_partitions`, `months_ahead`, `months_to_cover`, `partition_bounds`
- `backend/app/hat/ops/retention.py`: `purge_chat_logs`, `purge_job`, `purge_cutoff`
- `backend/app/hat/models/events.py`
- `backend/app/hat/models/runtime.py`
- `backend/app/hat/schema.py`: `PG_DDL`, `ensure_hat_schema`

Checks:
- `backend/tests/hat/db/hat_db_runtime.py`
- `backend/tests/hat/db/test_db_runtime.py`
- `backend/tests/hat/db/test_db_events.py`
- `backend/tests/hat/properties/test_events.py`
- `backend/tests/hat/properties/test_runtime.py`
- `backend/tests/hat/properties/test_retention.py`
- `backend/tests/hat/properties/test_telegram.py`
- `backend/tests/hat/properties/test_ingest.py`
- `backend/tests/hat/conformance/test_conf_events.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `2f23b2f377fa3db89906eb01cf61eb230b822b73`
Covered digest: `c8e98fa8a45f23d181b9f15e97bfb58c35f79959a3c90ffad831b0c606cdf565`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `watchlist.published` carries no correlation id: it belongs to a bot and a day, not to one signal, although 03 §6 puts it in the Signals group. `record()` and `events.correlation` exempt it. Recorded as a finding in the PR.
- `record()` requires `payload.chat_id` for `actor = user` but does not decide which event types are user-initiated: that is the Telegram layer's call (PR 16, HAT-SCH-065).
- `PBT events.ids_increasing` runs on SQLite only (the properties job has no Postgres); the Postgres identity is checked by `DB events.partitioned`.
- The order status set is the owner-pending default of HAT-SCH-058; the check mirrors it and changes with the owner's answer.
- `TLA OrderReconciliation!AtMostOneEntryPerSignal` and `TLA StopPlacement!StopOnlyTightens` (named on HAT-SCH-051 and HAT-SCH-081) are not built here: 11 §3 assigns both models to PR 13 and PR 15, whose code their actions map to. They are in `KNOWN_GAPS` in `test_conf_verification.py`; the database checks `runtime.signal_unique_per_day` and `governance.stop_only_rises` cover the same promises at the database.
- `hat_telegram_message` and `hat_command_log` have no foreign key, so they are exempt from the `user_id` chain check (`CHAIN_EXEMPT` in `test_conf_schema.py`), like `hat_kill_switch`: a finding on HAT-SCH-012.
- Sleeve lifecycle, regime and allocation events are not in the catalogue, so they are not recorded; PR 9 adds them with a spec change.

- PR 7 added an optional `on_finish` hook to `run_job` (`ops/jobs.py`), called inside the final-status transaction; the events and the job-run recording it touches are unchanged, and the ingest outcome events (`ingest.completed`, `ingest.failed`, `quality.failed`) are written through the existing `events.record`.
- PR 15b: `record()` checks that an order or position event's correlation id is its row's signal id (`_check_linked_signal`, HAT-SCH-064). `test_events.py`'s other properties now seed the order or position row their events name; none of them was narrowed.
- PR 16 added `events.user_actor_chat_id` and typed `record(actor=...)` as `Actor` (engine, user, broker, scheduler); the catalogue and the other properties are unchanged.
- PR 27 added `gtt.triggered` to the Orders group (HAT-EXEC-066: a GTT triggered with its sale resting below the limit; the payload names the broker order it became). The catalogue now holds 40 types; the text above no longer gives a count. The properties are unchanged.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### DB runtime.*

Break: `UniqueConstraint("tag")` removed from `HatOrder`.

```
tests/hat/db/test_db_runtime.py:33: in test_runtime__order_tag_unique
tests/hat/db/hat_db_runtime.py:253: in check_runtime__order_tag_unique
E   AssertionError: the database accepted: a second order with the same tag
```

### DB ops.snapshot_unique

Break: `trade_date` removed from the primary key of `HatDailyBotSnapshot`.

```
tests/hat/db/hat_db_runtime.py:312: in <lambda>
E   DETAIL:  Key (bot_id)=(01a15bec-6743-7313-9a5c-8efb1a2dcdc8) already exists.
E   AssertionError: the database refused: another day
```

### DB governance.stop_only_rises

Break: `<` became `<=` in the `hat_stop_only_tightens` function in `PG_DDL`.

```
tests/hat/db/hat_db_runtime.py:359: in move
E   AssertionError: the database refused: keeping or raising the stop from 0.0001 to 0.0001
```

### DB events.append_only

Break: the trigger in `PG_DDL` rewired from `BEFORE UPDATE OR DELETE` to `BEFORE DELETE`.

```
tests/hat/db/test_db_events.py:12: in test_events__append_only
tests/hat/db/hat_db_runtime.py:409: in rejects_append_only
E   AssertionError: the database accepted: updating occurred_at of an event
```

### DB events.partitioned

Break: `postgresql_partition_by` removed from `HatEvent`. Schema setup itself refuses first (the real `ensure_hat_schema` cannot attach a partition); the in-transaction variant "hat_event is a plain table" in `test_db_broken_variants.py` makes the check itself fail with `hat_event is not a partitioned table`.

```
tests/hat/db/conftest.py:59: in pg_engine
E   psycopg2.errors.InvalidObjectDefinition: "hat_event" is not partitioned
```

### DB events.partitions_ahead

Break: `MONTHS_AHEAD = 1` in `app/hat/ops/partitions.py`.

```
tests/hat/db/test_db_events.py:22: in test_events__partitions_ahead
tests/hat/db/hat_db_runtime.py:567: in check_events__partitions_ahead
E   AssertionError: no partition for 2026-03 at 2026-01-01
```

### DB events.partitions_job

Break: `MIN_MONTHS_AHEAD = 0` in `app/hat/ops/partitions.py` (a short run neither alerts nor fails).

```
tests/hat/db/hat_db_runtime.py:804: in check_events__partitions_job
E   AssertionError
FAILED tests/hat/db/test_db_events.py::test_events__partitions_job
```

### PBT events.atomic_with_state

Break: `record()` commits the session after the insert.

```
tests/hat/properties/test_events.py: in test_atomic_with_state
E   AssertionError: assert Decimal('0.0002') == Decimal('0.0001')
E   AssertionError: assert 1 == 0
```

### PBT events.unknown_type_rejected

Break: the catalogue test replaced by `if not event_type`.

```
tests/hat/properties/test_events.py:165: in test_unknown_type_rejected
E   AssertionError: record() accepted the event type '0'
```

### PBT events.correlation

Break: the trade-event correlation rule removed from `record()`.

```
tests/hat/properties/test_events.py: in test_correlation
E   AssertionError: assert None is not None
```

### PBT events.ids_increasing

Break: `_next_sqlite_id` returns the last id instead of the next.

```
tests/hat/properties/test_events.py: in test_ids_increasing
E   AssertionError: assert [0, 0] == [0]
```

### PBT events.partition_routing

Break: `next_month` wraps December to January of the same year.

```
tests/hat/properties/test_events.py:332: in test_partition_routing
E   assert datetime.datetime(2000, 12, 1, 0, 0, tzinfo=datetime.timezone.utc) < datetime.datetime(2000, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
```

### PBT runtime.virtual_consistency

Break: `position_is_virtual` ignores the skipped flag.

```
tests/hat/properties/test_runtime.py:150: in test_virtual_consistency
E   AssertionError: assert False == (('confirm' == 'shadow' or ('confirm' == 'confirm' and 'skip' == 'skip')))
```

### PBT retention.purge_scope

Break: the command-log purge deletes every row instead of the old ones.

```
tests/hat/properties/test_retention.py:145: in test_purge_scope
E   assert [] == [datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)]
```

### PBT telegram.retention_purge

Break: `RETENTION_YEARS = 2`.

```
tests/hat/properties/test_telegram.py: in test_retention_purge
E   AssertionError: assert [datetime.datetime(...)] == []
```

### PBT ingest.event_and_state_atomic

Break: `record()` commits the session after the insert.

```
tests/hat/properties/test_ingest.py:79: in test_event_and_state_atomic
E   assert (1, 1) == (0, 0)
```

### CONF events.catalogue

Break: `"order.amended"` added to the Orders group in `events.py`.

```
tests/hat/conformance/test_conf_events.py:14: in test_events__catalogue
E   AssertionError: assert {'Data': ('in...closed'), ...} == {'Data': ('in...closed'), ...}
E     Differing items:
E     {'Orders': ('order.placed', ..., 'gtt.refreshed', ...)} != {'Orders': ('order.placed', ..., 'gtt.refreshed')}
```

### CONF schema.runtime_columns, event_columns, indexes

Break: two separate breaks, each reverted after its check failed: `HatSignal.trigger_price` changed from `Money` to `Ratio`; the `hat_event` correlation index moved to `causation_id`.

```
test_schema__columns_match_spec[runtime_columns]: AssertionError: ('hat_signal', 'trigger_price')   assert 'numeric' == 'numeric(18,4)'
test_schema__indexes: AssertionError: SpecIndex(table='hat_event', columns=('correlation_id',), unique=False, partial=None)
```

### PBT events.correlation_links_the_signal

Break: `record()` no longer calls `_check_linked_signal`.

```
E   AssertionError: ('order', 'exit.partial', 'other')
E   assert True == ('other' == 'own'
E     
E     - own
E     + other)
E   Failing test case: test_correlation_links_the_signal(
```

### PBT events.user_actor_chat_id

Break: in `backend/app/hat/events.py`, the user-actor check accepts any `chat_id` that is not None.

```
E   Failed: DID NOT RAISE EventError
E   Failing test case: test_user_actor_chat_id(
E       actor='user',
E       chat_id=False,
```
