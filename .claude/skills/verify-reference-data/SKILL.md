---
name: verify-reference-data
description: How to run, read and update the checks behind Hire a Trader's reference data (instrument master, symbol history, Fyers symbols, price bands, industries), trading calendar and NSE MCP client (retry, concurrency, call log, error text, date check, batching, server allowlist). Use before and after any change to backend/app/hat/data, backend/app/utils/market_hours.py, the weekly, nightly and calendar job bodies or the recorded MCP fixtures.
---

# verify-reference-data

Checks that the instrument master keeps one stable identity per stock through renames and delistings, that old symbols resolve to the right company, that Fyers symbols never point at another stock, that a calendar holiday is closed in every order path, and that the NSE MCP client retries, limits, logs and reports errors the way the specs promise. Specs: `docs/specs/hat/02-data.md` §2 and §3 (HAT-DATA-010, 020..027, 030..036), `01-decisions.md` (I1, I16), `00-overview.md` (HAT-OVR-001, C7).

## Properties

- `PBT mcp.retry_bounded` On the real `McpClient.call`, any sequence of transport failures makes at most 4 attempts (1 + 3 retries), waits strictly longer before each retry, and ends in a typed `McpError` rather than an empty result. Harm: unbounded retries stall the nightly window, or an empty result passes as a holiday.
- `PBT mcp.call_logged` Every attempt (success, retry or failure) writes exactly one `CallRecord` with tool, symbol count and returned date or failure code, and the `hat_job_run.calls` a job records equals the attempts. Harm: a short or stale pull cannot be diagnosed.
- `PBT mcp.no_raw_error_text` For any error payload a transport raises, returns as a tool error or sends in an HTTP 500, no `McpError` message, repr, cause chain or log line holds a substring of it longer than 8 characters. Harm: internal URLs or tokens reach an alert, API response or Telegram.
- `PBT ingest.batches_le_50` `batches` partitions any list in order into batches of at most 50 (exactly 50 but the last; 63 for 3,121 symbols), also `ingest.batches_of_50`. Harm: oversize calls are rejected and stocks go missing.
- `PBT ingest.concurrency_bounded` For any workload and interleaving the calls in flight seen by the transport never exceed the limit (default 4). Harm: the server throttles the client and the night's data is late.
- `PBT ingest.date_mismatch_rejected` A bulk-quote row is kept only if its date is the trade date; every other row (another, missing or unreadable date) is in the mismatch report, and every requested symbol is kept, mismatched or missing. Harm: yesterday's prices are stored as today's.
- `PBT reference.instrument_identity_stable` Over any sequence of weekly refreshes with renames, name and face-value changes an instrument's id never changes and no two rows share (market, ISIN, series); repeating a refresh changes nothing. Harm: a rename splits one stock's history into two instruments.
- `PBT reference.delisted_never_deleted` A disappearing instrument stays, inactive with `delisted_on` set, and the row count never decreases. Harm: backtests become survivorship-biased.
- `PBT reference.symbol_history_resolves` Symbol intervals of an instrument never overlap, every held symbol has a history row, a (symbol, date) resolves to at most one instrument (the covering one), and seeding from legacy `symbol_history` is idempotent. Harm: an old symbol in a 2015 bhavcopy attaches to the wrong company.
- `PBT reference.prefix_paging_complete` Prefix paging yields every listed symbol exactly once, a page at the 500-row cap is split again, and the CSV fallback gives the same instrument keys as the MCP path. Harm: truncated pages drop stocks from the universe.
- `PBT reference.fyers_symbol_roundtrip` Every Fyers row maps back through `symbol_master.to_db` to the instrument's NSE symbol, no two instruments share one, and a symbol that does not round-trip (an alias such as LTIM) is reported and given no row. Harm: an order for one stock goes out under another's symbol.
- `PBT reference.current_band` The band in force on a date is the row with the greatest `effective_from` on or before it (null = no band), always 2, 5, 10, 20 or null. Harm: an EP candidate in a 5% band is treated as gap-eligible.
- `PBT reference.current_industry` The industry in force is the latest row on or before the date, and an instrument with none is reported, never given a shared one. Harm: the per-industry cap is bypassed.
- `PBT calendar.holiday_closed` A date with `is_trading = false` is closed in `MarketHoursPolicy` at every instant (built from the calendar and installed process-wide, so a bare policy and `DEFAULT_POLICY` also refuse it), a trading date follows its own session, and the scheduler's trading days equal the dates with `is_trading = true`. Harm: orders attempted on an exchange holiday.
- `PBT calendar.session_from_reference_data` The session the engine uses on any date equals the `hat_trading_calendar` row, or the `hat_market` default on a normal weekday, never a constant. Harm: hard-coded NSE hours place a P2 market's orders at the wrong time.
- `DB reference.refresh_on_postgres` On real Postgres a refresh with renames, delisting and a broker symbol moving between instruments never trips the unique keys. Harm: the weekly refresh fails half-way and leaves the master stale.
- `CONF imports.engine_data_sources` The only MCP server names in HAT code are `nse-bhavcopy` and `cm-market`, and engine, risk and execution modules reach no research or chat MCP client. Harm: an unvetted source changes which stocks are scanned.
- `CONF imports.mcp_transport_only` `McpClient` takes an `McpTransport`; only `HttpTransport` in `mcp_client.py` opens a connection, and no other HAT module addresses an MCP endpoint. Harm: a hidden network path calls real NSE from tests.
- `CONF mcp_endpoints_allowlist` The client can be configured only with the two vetted servers; the env variables are exactly the two the ops spec lists. Harm: unverified third-party data reaches a scan.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_mcp.py tests/hat/properties/test_ingest_mcp.py \
  tests/hat/properties/test_reference.py tests/hat/properties/test_calendar.py -q --no-cov
python -m pytest tests/hat/conformance -q --no-cov          # imports, allowlist, skill digests
python -m pytest tests/test_market_hours_policy.py -q --no-cov   # existing market-hours tests
# the Postgres refresh check (a throwaway database whose name ends in _test):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  python -m pytest -m postgres tests/hat/db -q --no-cov
```

Everything runs offline: the transports are `ScriptedTransport` and `ListingServer` in `backend/tests/hat/hat_mcp_support.py`, and the recorded server shapes are in `backend/tests/hat/fixtures/`.

## Bug or model error

- A failing property prints the falsifying example and a `reproduce_failure` blob. Replay the example against the real function (`upsert_instruments`, `apply_symbol_change`, `McpClient.call`, `MarketCalendar.policy`). If the real function does it, it is a bug: fix the code, or record it as a finding in the PR.
- A model error is a fake that is less strict than the real server: `ListingServer` returns rows sorted by symbol and cut at the limit (the real order is unverified, see Scope changes); `ScriptedTransport` plays one outcome per call. Change the fake only with a recorded reason.
- Never weaken a property or shrink a strategy to make a check pass.
- Known limits kept visible: the price-band file and the holiday-list format are `[A]` shapes coded against fixtures and validated on the first live download; a symbol exactly equal to a capped prefix is found only if the server lists it inside the capped page.

## Updating

- A new reference table or loader: keep it idempotent (a second run with the same input writes nothing), add a strategy to `test_reference.py`, and a break to this skill.
- A new MCP tool wrapper: route it through `McpClient.call` so retry, limits and logging apply, and use only `nse-bhavcopy` or `cm-market`.
- New or changed fixtures are recorded from the real servers (read-only tools) and kept small.
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-reference-data` prints the digest.

## Coverage

Code under check:
- `backend/app/hat/data/mcp_client.py`: `McpClient`, `HttpTransport`, `unwrap_result`, `batches`, `backoff_delay`
- `backend/app/hat/data/reference.py`: `page_listing`, `parse_equity_list_csv`, `build_listing_from_mcp`, `build_listing_from_csv`, `parse_price_band_csv`, `band_from_circuit_limits`, `parse_industry_csv`
- `backend/app/hat/data/instruments.py`: `upsert_instruments`, `apply_symbol_change`, `resolve_symbol`, `seed_symbol_history`, `refresh_fyers_symbols`, `load_price_bands`, `current_band`, `load_industries`, `current_industry`
- `backend/app/hat/data/calendar.py`: `parse_nse_holidays`, `upsert_calendar`, `MarketCalendar`, `trading_days`, `install_from_session`
- `backend/app/hat/data/reference_jobs.py`: `reference_weekly`, `bands_nightly`, `calendar_refresh`
- `backend/app/utils/market_hours.py`: `MarketHoursPolicy.phase`, `install_calendar`

Checks:
- `backend/tests/hat/properties/test_mcp.py`
- `backend/tests/hat/properties/test_ingest_mcp.py`
- `backend/tests/hat/properties/test_reference.py`
- `backend/tests/hat/properties/test_calendar.py`
- `backend/tests/hat/db/test_db_reference_upsert.py`
- `backend/tests/hat/conformance/test_conf_imports.py`
- `backend/tests/hat/conformance/test_conf_fixtures.py`
- `backend/tests/hat/hat_mcp_support.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `21ddd29b3546424f9d45946ca14bb9f0911371f2`
Covered digest: `49fe17c5dace1697a34fcb8bf3a55e2a008f8c0ff1dbc3cdb284a41ef8d8430c`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `reference.prefix_paging_complete` models the server as returning rows sorted by symbol and cut at the limit, so a symbol equal to a capped prefix comes first. The real server's order is unverified (the recorded sample is not sorted); a symbol that a capped page omits and no deeper prefix can return would be missed. The weekly job cross-checks against the equity list CSV, which reports a listed symbol with no CSV row as unresolved.
- `reference.symbol_history_resolves` draws symbols distinct within one listing and steps seven days apart; the resolver prefers the active instrument, then series EQ, when two series of one ISIN share a symbol.
- `reference.fyers_symbol_roundtrip` does not assert that every symbol gets a row: a symbol that does not round-trip (an alias such as LTIM) is reported, which is the property.
- The Muhurat and Saturday special sessions are modelled as `MarketHoursPolicy.sessions`; the NSE holiday JSON shape (`tradingDate`, `evening_session`) is `[A]`, validated on the first live download.
- `mcp.no_raw_error_text` checks substrings of 9 or more characters; payloads that are substrings of the stable codes themselves are excluded (`assume`).
- Postgres is checked by `refresh_on_postgres` only for the unique keys; the property tests use SQLite, where the keys are the same constraints.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### PBT mcp.retry_bounded

Break: the retry loop allows two more attempts.

```
tests/hat/properties/test_mcp.py F
```

Second break: backoff_delay is constant.

```
tests/hat/properties/test_mcp.py F
tests/hat/properties/test_mcp.py:73: in test_retry_bounded_backoff_grows
tests/hat/properties/test_mcp.py:74: in test_retry_bounded_backoff_grows
E assert 1.0 > 1.0
E Failing test case: test_retry_bounded_backoff_grows(
E retry=1,
```

### PBT mcp.call_logged

Break: a failed attempt writes no log record.

```
tests/hat/properties/test_mcp.py F
tests/hat/properties/test_mcp.py:78: in test_call_logged
tests/hat/properties/test_mcp.py:103: in test_call_logged
E AssertionError: assert 0 == 4
E Failing test case: test_call_logged(
E script=[McpTransportError('mcp_transport')],
```

### PBT mcp.no_raw_error_text

Break: McpTransportError keeps whatever code text a transport gives it.

```
tests/hat/properties/test_mcp.py F
tests/hat/properties/test_mcp.py:167: in test_no_raw_error_text
E app.hat.data.mcp_client.McpError: 000000000
tests/hat/properties/test_mcp.py:123: in test_no_raw_error_text
tests/hat/properties/test_mcp.py:171: in test_no_raw_error_text
E assert not True
```

### PBT ingest.batches_le_50

Break: batch size 51.

```
tests/hat/properties/test_ingest_mcp.py F
tests/hat/properties/test_ingest_mcp.py:21: in test_batches_le_50
tests/hat/properties/test_ingest_mcp.py:28: in test_batches_le_50
E assert False
E Failing explicit example: test_batches_le_50(
E n=3121,
```

### PBT ingest.concurrency_bounded

Break: the semaphore is not taken around a transport call.

```
tests/hat/properties/test_ingest_mcp.py F
tests/hat/properties/test_ingest_mcp.py:87: in test_concurrency_bounded
tests/hat/properties/test_ingest_mcp.py:102: in test_concurrency_bounded
E assert 2 <= 1
E Failing test case: test_concurrency_bounded(
E n=51,
```

### PBT ingest.date_mismatch_rejected

Break: every row is kept whatever its date.

```
tests/hat/properties/test_ingest_mcp.py F
tests/hat/properties/test_ingest_mcp.py:46: in test_date_mismatch_rejected
tests/hat/properties/test_ingest_mcp.py:79: in test_date_mismatch_rejected
E assert False
E Failing test case: test_date_mismatch_rejected(
E symbols=['A'],
```

### PBT reference.instrument_identity_stable

Break: instruments are keyed by symbol too, so a rename makes a new instrument.

```
tests/hat/properties/test_reference.py F
```

### PBT reference.delisted_never_deleted

Break: a missing instrument is deleted.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:136: in test_delisted_never_deleted
tests/hat/properties/test_reference.py:145: in test_delisted_never_deleted
E assert 0 >= 1
E Failing test case: test_delisted_never_deleted(
E steps=[[Security(isin='INE000000005',
```

### PBT reference.symbol_history_resolves

Break: a rename does not close the previous interval.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:157: in test_symbol_history_resolves
tests/hat/properties/test_reference.py:165: in test_symbol_history_resolves
tests/hat/properties/test_reference.py:96: in check_history_disjoint
E AssertionError: an open interval is followed by another
E assert None is not None
```

Second break: seeding adds the old-symbol row on every run.

```
tests/hat/properties/test_reference.py F
E sqlite3.IntegrityError: UNIQUE constraint failed: hat_instrument_symbol_history.instrument_id, hat_instrument_symbol_history.valid_from
tests/hat/properties/test_reference.py:186: in test_symbol_history_resolves_seed_idempotent
tests/hat/properties/test_reference.py:227: in test_symbol_history_resolves_seed_idempotent
E sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: hat_instrument_symbol_history.instrument_id, hat_instrument_symbol
E [SQL: INSERT INTO hat_instrument_symbol_history (instrument_id, symbol, valid_from, valid_to) VALUES (?, ?, ?, ?)]
```

### PBT reference.prefix_paging_complete

Break: a page at the cap is accepted as complete.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:254: in test_prefix_paging_complete
tests/hat/properties/test_reference.py:264: in test_prefix_paging_complete
E AssertionError: assert [('A', 'EQ'),...', 'EQ'), ...] == [('A', 'EQ'),...', 'EQ'), ...]
E Right contains 901 more items, first extra item: ('A&A&BA', 'EQ')
E Use -v to get more diff
```

Second break: the CSV fallback drops non-EQ series.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:273: in test_prefix_paging_complete_csv_fallback
tests/hat/properties/test_reference.py:316: in test_prefix_paging_complete_csv_fallback
E AssertionError: assert set() == {('INE0000000... 'BE', 'AAA')}
E Extra items in the right set:
E ('INE000000000', 'BE', 'AAA')
```

### PBT reference.fyers_symbol_roundtrip

Break: the round-trip check is removed.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:358: in test_fyers_symbol_roundtrip
tests/hat/properties/test_reference.py:384: in test_fyers_symbol_roundtrip
E AssertionError: assert 'LTM' == 'LTIM'
E - LTIM
E ? -
```

### PBT reference.current_band

Break: the earliest band row is used instead of the latest.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:398: in test_current_band
tests/hat/properties/test_reference.py:431: in test_current_band
E AssertionError: assert Decimal('2.0000000000') == Decimal('5')
E Failing test case: test_current_band(
E plan=[(1, [Decimal('2'), Decimal('2'), Decimal('2')]),
```

### PBT reference.current_industry

Break: the earliest industry row is used.

```
tests/hat/properties/test_reference.py F
tests/hat/properties/test_reference.py:453: in test_current_industry
tests/hat/properties/test_reference.py:492: in test_current_industry
E AssertionError: assert 'industry X' == 'industry Y'
E - industry Y
E ? ^
```

### PBT calendar.holiday_closed

Break: holidays are not passed to the policy.

```
tests/hat/properties/test_calendar.py F
```

Second break: the installed calendar is ignored.

```
tests/hat/properties/test_calendar.py F
tests/hat/properties/test_calendar.py:109: in test_holiday_closed_installed_calendar
tests/hat/properties/test_calendar.py:121: in test_holiday_closed_installed_calendar
E assert not True
E Failing test case: test_holiday_closed_installed_calendar(
E spec=(datetime.time(0, 0),
```

Second break: a holiday row still has a session.

```
tests/hat/properties/test_calendar.py F
tests/hat/properties/test_calendar.py:130: in test_holiday_closed_trading_days
tests/hat/properties/test_calendar.py:142: in test_holiday_closed_trading_days
E assert (datetime.date(2026, 1, 21) in {datetime.date(2026, 1, 1), datetime.date(2026, 1, 2), datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), d
E Failing test case: test_holiday_closed_trading_days(
E spec=(datetime.time(0, 0),
```

### PBT calendar.session_from_reference_data

Break: the policy falls back to the NSE constants.

```
tests/hat/properties/test_calendar.py F
tests/hat/properties/test_calendar.py:146: in test_session_from_reference_data
tests/hat/properties/test_calendar.py:165: in test_session_from_reference_data
E assert False == (((datetime.time(0, 0), datetime.time(0, 30)) is not None and datetime.time(0, 0) <= datetime.time(0, 30)))
E Failing test case: test_session_from_reference_data(
E spec=(datetime.time(0, 0), datetime.time(0, 30), {}),
```

### DB reference.refresh_on_postgres

Break: stale broker rows are never removed, so a symbol that moves collides on the unique key.

```
tests/hat/db/test_db_reference_upsert.py F
E psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "hat_broker_symbol_pkey"
E DETAIL: Key (instrument_id, broker)=(01a15bfc-c801-7475-8be0-be9b894e62d4, fyers) already exists.
tests/hat/db/test_db_reference_upsert.py:39: in test_reference__refresh_on_postgres
E sqlalchemy.exc.IntegrityError: (psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "hat_broker_symbol_pkey"
E DETAIL: Key (instrument_id, broker)=(01a15bfc-c801-7475-8be0-be9b894e62d4, fyers) already exists.
```

### CONF imports.engine_data_sources

Break: a research server is allowed.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:52: in test_imports__engine_data_sources
E AssertionError: assert frozenset({'c..., 'tapetide'}) == {'cm-market', 'nse-bhavcopy'}
E Extra items in the left set:
E 'tapetide'
E Use -v to get more diff
```

### CONF imports.mcp_transport_only

Break: another module reads the MCP endpoint variable.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:91: in test_imports__mcp_transport_only
E AssertionError: data/reference_jobs.py addresses an MCP endpoint outside the client
E assert not True
```

### CONF mcp_endpoints_allowlist

Break: HttpTransport accepts any server name.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:108: in test_mcp_endpoints_allowlist
E Failed: DID NOT RAISE McpConfigError
```
