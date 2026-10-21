---
name: verify-data
description: How to run, read and update the checks behind Hire a Trader's historical data build, the bhavcopy parsers for both NSE file formats, the Parquet store and its DuckDB views, corporate-action loading and the adjusted-price view, and the index history load. Use before and after any change to backend/app/hat/data/store.py, bhavcopy.py, corporate_actions.py, history_build.py, backend/app/services/bhavcopy_text.py, the history methods of mcp_client.py, or the data build sections of docs/specs/hat/02-data.md.
---

# verify-data

Checks that the 2010-to-today build writes each trading day once, in the right format era, with exact prices; that Parquet files are written atomically and raw files never change; that the adjusted view equals raw prices times the product of later factors, exactly; that corporate actions load once however many sources and reruns bring them; and that the build is idempotent and resumable. Specs: `docs/specs/hat/02-data.md` §8, §9 (HAT-DATA-011 source schema, 052..055, 080..085, 090..097), `01-decisions.md` (T6, B27, I2), `00-overview.md` (HAT-OVR-052).

## Properties

- `DB reference.corporate_action_load_on_postgres` The corporate-action loader on Postgres stores each (instrument, ex-date, kind) once however often or in what order MCP and archive rows arrive, and keeps the first-written factor, reporting a conflict. Harm: an action applied twice halves every earlier price twice.
- `PBT corporate_actions.adjusted_view` adj price(d) is raw x the product of factors with ex-date after d (open, high, low, close, prev close), exactly, for every instrument, factor set and bar days. Harm: a split looks like a crash and indicators and backtests fire on it.
- `PBT corporate_actions.adjusted_volume` adj volume(d) is raw volume over the same product, and adj close x adj volume equals raw close x raw volume. Harm: liquidity and turnover filters misread split stocks.
- `PBT corporate_actions.factor_parse` A face-value split a to b parses to b/a, a bonus x:y to y/(x+y), in every wording template; text with no usable ratio gives no factor and is reported. Harm: a wrong factor corrupts all earlier prices of the stock.
- `PBT corporate_actions.upsert_idempotent` For every sequence of loads (MCP, archive fallback, repeats) the stored actions are the set of distinct keys, first write wins. Harm: a rerun backfill double-adjusts.
- `PBT store.atomic_write` A write interrupted at any point leaves no partial file visible and the previous contents unchanged. Harm: a half-written partition is read as the day.
- `PBT store.raw_immutable` A raw zip keeps the bytes it was first given; rewriting with other bytes is refused; adjusted values cannot be stored as raw rows. Harm: raw history silently changes.
- `PBT store.bars_daily_unique` For raw partitions written by several runs in any order, `bars_daily` has one row per (instrument, date), the latest run's. Harm: duplicate bars double volume and skew indicators.
- `PBT store.partitions_complete` The partition report lists exactly the expected trading days with no rows, and extra days separately. Harm: holes in history go unnoticed.
- `PBT ingest.parquet_roundtrip` Every bar written to Parquet reads back with identical prices, volumes, traded value and source, exactly. Harm: float rounding in stored prices.
- `PBT ingest.sources_same_schema` Bars from the bhavcopy and MCP sources go through one writer with one schema, so each source's file has the same Parquet schema. Harm: mixed schemas break the DuckDB views.
- `PBT ingest.index_snapshot_stored` Index history loaded in chunks, repeated, or one index at a time is stored as one row per index per day with the loaded values; coverage names exactly the days with no row; a repeated load is byte-identical. Harm: the regime and breadth inputs have holes or lose indices.
- `PBT bhavcopy.format_by_date` Every weekday from 2010-01-01 to today (boundaries 5 and 8 Jul 2024 included) maps to the old URL up to 5 Jul 2024 and the UDiFF URL from 8 Jul 2024; weekends have none. Harm: the backfill requests files that do not exist.
- `PBT bhavcopy.missing_file_is_holiday_candidate` For whatever NSE answers on each weekday, a missing file that the calendar lists is a holiday candidate, an unlisted one is a gap; both are reported. Harm: real gaps pass as holidays.
- `PBT bhavcopy.resume_skips_done` After a run interrupted at any request, `--resume` never requests a day already stored. Harm: a restart re-downloads years of files.
- `PBT bhavcopy.rate_limited` Consecutive requests start at least the interval apart whatever each takes. Harm: NSE blocks the host.
- `PBT bhavcopy.parsers_agree` The same rows rendered as an old CM file and as a UDiFF file parse to identical normalised rows; a file carrying another day's date yields no rows. Harm: the 2024 format switch shows as a price break.
- `PBT bhavcopy.traded_value_kept` Each parsed row keeps the file's traded value. Harm: turnover history is lost.
- `PBT bhavcopy.validation_flags_mismatch` The build fails iff a sampled close differs from the MCP close (exact), and lists the row. Harm: a wrong parse passes validation.
- `PBT history_build.offline_end_to_end` The offline build over generated files in both formats writes exactly the file rows, with no duplicate (instrument, date), reports missing days, and a repeated or resumed run changes nothing. Harm: a rerun duplicates or loses days.
- `PBT history_build.mcp_index_source_chains` Chained `get_index_history` chunks yield every present day in range exactly once and nothing outside. Harm: index history with holes or repeats.
- `PBT history_build.mcp_close_source_exact` The MCP close for a symbol-day is the row dated that day, exactly; other days, missing rows and unreadable closes give None. Harm: validation compares against a neighbouring day.
- `PBT history_build.mcp_corporate_actions_page` An `error` answer is a failed lookup, never an empty list; a `warning` marks the page incomplete. Harm: a failed lookup reads as no corporate actions.
- `CONF store.data_root` The store root is `app.paths.HAT_DATA_ROOT`, defined only there, and raw names cannot leave it. Harm: data written outside the data volume.
- `CONF store.layout` Every Parquet file sits in `<dataset>/date=YYYY-MM-DD/`. Harm: DuckDB partition pruning and the views miss files.

## Run

```bash
cd backend
# every pytest, Hypothesis or data command runs under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_store.py tests/hat/properties/test_corporate_actions.py \
  tests/hat/properties/test_bhavcopy.py tests/hat/properties/test_history_build.py tests/hat/properties/test_ingest.py -q --no-cov
$CAP python -m pytest tests/hat/conformance -q --no-cov
# the database check needs Postgres (a throwaway database whose name ends in _test):
DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test SMARTTRADER_PG_TESTS=1 \
  $CAP python -m pytest -m postgres tests/hat/db/test_db_corporate_actions.py -q --no-cov
```

No check touches the network: the downloader, the MCP client and the validation source are injected fakes, and the recorded MCP answers are in `tests/hat/fixtures/mcp_*_history.json` and `mcp_corporate_actions.json`. The real 2010-to-today backfill is an ops step (`02-data.md` §9), not a check.

## Bug or model error

- A failing property prints the falsifying example and a `reproduce_failure` blob. Reproduce it with the real writer, parser or view; never narrow a strategy to avoid the example.
- A failing `corporate_actions.adjusted_view` names the instrument, date and column. First check the factor convention: HAT factors multiply prices before the ex-date (split a to b is b/a, bonus x:y is y/(x+y)); the phase-1 pipeline uses the reciprocal, so a swapped convention shows as every price moved the wrong way. A real bug is in the view in `store.py`; a spec change means the property changes with it.
- A failing `bhavcopy.parsers_agree` means one of the two file layouts is read differently. The old CM and UDiFF column lists are `[A]` until the first real download validates them: compare against a real file before changing a parser to satisfy a fake.
- A failing `store.*` check names the file or partition. Never fix it by loosening the check: atomic writes, immutable raw files and one latest run per (instrument, date) are what backtests rely on.
- A `DB` failure on the loader says what the database accepted or refused; the table is `hat_corporate_action` and its key is in `models/`.

## Updating

- A new source format or column: change the spec table, the parser and `tests/hat/hat_bhav_support.py` (the renderers) together; `parsers_agree` fails until both formats agree.
- A new dataset: add its schema and partition rule to `store.py` and the spec's dataset table; `CONF store.layout` covers every Parquet file the store writes.
- A new corporate-action kind: add its wording template to `test_corporate_actions.py` and the parser together.
- After re-confirming everything, update the sync commit and digest: `python backend/tests/hat/conformance/hat_conf_skills.py verify-data` prints the digest.

## Coverage

Code under check:
- `backend/app/services/bhavcopy_text.py`
- `backend/app/hat/data/store.py`
- `backend/app/hat/data/bhavcopy.py`
- `backend/app/hat/data/corporate_actions.py`
- `backend/app/hat/data/history_build.py`
- `backend/app/hat/data/mcp_client.py`

Checks:
- `backend/tests/hat/hat_bhav_support.py`
- `backend/tests/hat/properties/test_store.py`
- `backend/tests/hat/properties/test_corporate_actions.py`
- `backend/tests/hat/properties/test_bhavcopy.py`
- `backend/tests/hat/properties/test_history_build.py`
- `backend/tests/hat/properties/test_ingest.py`
- `backend/tests/hat/conformance/test_conf_store.py`
- `backend/tests/hat/conformance/test_conf_fixtures.py`
- `backend/tests/hat/db/test_db_corporate_actions.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `001a04965295bcc64ede391e0b1ffafce9a99eb6`
Covered digest: `e8e668d574089b642918772ba7937f1b71748ffcb90d69ca84826a01c8e253bb`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- A cumulative factor outside [1e-9, 100) is refused with `store_factor_range` (the view holds factors as decimal(20,18)); `adjusted_view_idempotent` skips such worlds, and `test_cumulative_factor_out_of_range_is_refused` pins the refusal. Several compounded reverse splits could reach it: a follow-up if real data does.
- The adjusted-view exactness properties draw factors with finite decimal reciprocals: factors are stored to 18 places and their reciprocals (for volume) to 12, so an arbitrary rational factor is exact for prices but not for volume. Real factors (splits, bonuses) are ratios of small integers.
- The old CM (`cmDDMMMYYYYbhav.csv`) and UDiFF column lists are `[A]`, written from the published layouts and shared fixtures, not from a real download; the first real run validates them.
- The MCP normaliser for nightly bars is PR 7's; `ingest.sources_same_schema` covers the shared writer that both sources use.
- `bulk_quotes` and its date check do not apply to history: validation compares against the `date` field of `get_stock_history` rows.
- Smallcap 250's first date is `[A]` (default 2010-01-01); a gap is reported, not hidden.
- The index archive CSV parser (`parse_index_archive_csv`) is covered by unit examples only, as MCP is the primary source for index history.
- History-only securities register as inactive instruments with their symbol as name; renames across history are not recorded until a weekly refresh (follow-up).

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### DB reference.corporate_action_load_on_postgres

Break: the loader ignores stored actions and inserts again.

```
tests/hat/db/test_db_corporate_actions.py F
E   psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint "hat_corporate_action_instrument_id_ex_date_action_type_key"
E   DETAIL:  Key (instrument_id, ex_date, action_type)=(01a15cb6-9291-7436-b05e-3f47afeaa594, 2018-06-14, split) already exists.
tests/hat/db/test_db_corporate_actions.py:39: in test_corporate_action_load_on_postgres
E   sqlalchemy.exc.IntegrityError: (psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "hat_corporate_action_instrument_id_ex_date_action_type_key"
E   DETAIL:  Key (instrument_id, ex_date, action_type)=(01a15cb6-9291-7436-b05e-3f47afeaa594, 2018-06-14, split) already exists.
E   
```

### PBT corporate_actions.adjusted_view

Break: the ex-date bar is adjusted as well (the factor applies to ex_date <= d, not ex_date > d).

```
tests/hat/properties/test_corporate_actions.py F
```

Second break: prev_close uses the same factor as the prices.

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:100: in test_adjusted_view
tests/hat/properties/test_corporate_actions.py:123: in test_adjusted_view
E   AssertionError: assert [Decimal('0.0...00000000000')] == [Decimal('0.0...imal('0.025')]
E     
E     At index 4 diff: Decimal('0.0500000000000000000000') != Decimal('0.025')
E     
```

Third break: building the view rewrites the raw files (same rows, different bytes).

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:100: in test_adjusted_view
tests/hat/properties/test_corporate_actions.py:115: in test_adjusted_view
E   AssertionError: assert {'bars_daily_...898d33c78485'} == {'bars_daily_...e775e481d45f'}
E     
E     Differing items:
E     {'bars_daily_raw/date=2024-01-18/part-01a15c36-f5b0-7237-bb57-9d8e6596416d.parquet': '725b1deb78b53cd82c37394563b1b842034b2b2a5c97c517bd3b898d33c78485'} != {'bars_daily_raw/date=2024-01-18/part-01a15c36-f5b0-7237-bb57-9d8e6596416d.parquet': '74ae6794429d48c108ce932dd65f16ab6dd3d21bf966cf5d1735e775e481d45f'}
```

### PBT corporate_actions.adjusted_volume

Break: volume is multiplied by the factor instead of divided.

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:152: in test_adjusted_volume
tests/hat/properties/test_corporate_actions.py:170: in test_adjusted_volume
E   AssertionError: assert Decimal('0.500000000000') == (Decimal('1') / Decimal('0.5'))
E    +  where Decimal('1') = Decimal(1)
E   Failing test case: test_adjusted_volume(
E       world=({UUID('00000000-0000-0000-0000-000000000001'): {datetime.date(2024, 1, 18): (Decimal('0.05'),
```

### PBT corporate_actions.factor_parse

Break: a bonus of x for y gives x / (x + y).

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:193: in test_factor_parse
tests/hat/properties/test_corporate_actions.py:213: in test_factor_parse
E   AssertionError: assert (Fraction(2, 3) == Fraction(1, 3))
E    +  where Fraction(2, 3) = ParsedAction(action_type='bonus', factor=Fraction(2, 3)).factor
E    +  and   Fraction(1, 3) = Fraction(1, (2 + 1))
E   Failing test case: test_factor_parse(
```

Second break: a reverse split (factor above 1) is accepted.

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:193: in test_factor_parse
tests/hat/properties/test_corporate_actions.py:214: in test_factor_parse
E   AssertionError: assert ParsedAction(action_type='split', factor=Fraction(20, 1)) is None
E    +  where ParsedAction(action_type='split', factor=Fraction(20, 1)) = parse_action_text('Face value split from Rs 0.1 to Rs 2')
E   Failing test case: test_factor_parse(
E       # The test always failed when commented parts were varied together.
```

Third break: unparseable text gets a guessed factor of 1/2.

```
tests/hat/properties/test_corporate_actions.py F
tests/hat/properties/test_corporate_actions.py:219: in test_factor_parse_unparseable_is_reported
tests/hat/properties/test_corporate_actions.py:222: in test_factor_parse_unparseable_is_reported
E   AssertionError: assert ParsedAction(action_type='other', factor=Fraction(1, 2)) is None
E    +  where ParsedAction(action_type='other', factor=Fraction(1, 2)) = parse_action_text('')
E   Failing test case: test_factor_parse_unparseable_is_reported(
E       text='',  # or any other generated value
```

### PBT corporate_actions.upsert_idempotent

Break: a later load overwrites the stored factor.

```
tests/hat/properties/test_corporate_actions.py F
```

### PBT store.atomic_write

Break: the file is written in place instead of to a temporary name.

```
tests/hat/properties/test_store.py F
tests/hat/properties/test_store.py:43: in test_atomic_write
tests/hat/properties/test_store.py:79: in test_atomic_write
E   AssertionError: assert {} == {'bars_daily_...\x00\x00PAR1'}
E     
E     Right contains 1 more item:
E     {'bars_daily_raw/date=2024-03-01/part-00000000-0000-0000-0000-000000000002.parquet': b'PAR1'
```

### PBT store.raw_immutable

Break: a different zip under an existing name overwrites it.

```
tests/hat/properties/test_store.py F
tests/hat/properties/test_store.py:89: in test_raw_immutable
tests/hat/properties/test_store.py:103: in test_raw_immutable
E   Failed: DID NOT RAISE StoreError
E   Failing test case: test_raw_immutable(
E       ops=[('a.zip', b'\x00'), ('a.zip', b'\x00\x00')],
E   )
```

### PBT store.bars_daily_unique

Break: the latest-run rule keeps one row per run, so a key written by two runs appears twice.

```
tests/hat/properties/test_store.py F
tests/hat/properties/test_store.py:140: in test_bars_daily_unique
tests/hat/properties/test_store.py:163: in test_bars_daily_unique
E   AssertionError: assert 2 == 1
E    +  where 2 = len([('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1)), ('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1))])
E    +  and   1 = len({('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1))})
E    +    where {('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1))} = set([('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1)), ('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1))])
```

Second break: the oldest run wins.

```
tests/hat/properties/test_store.py F
tests/hat/properties/test_store.py:140: in test_bars_daily_unique
tests/hat/properties/test_store.py:168: in test_bars_daily_unique
E   AssertionError: assert {('00000000-0...62e1f0dfb3c')} == {('00000000-0...4e005b5fb6e')}
E     
E     Differing items:
E     {('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1)): (Decimal('0.0500000000000000000000'), '01a15c38-0b7c-7344-ba52-d62e1f0dfb3c')} != {('00000000-0000-0000-0000-00000000000c', datetime.date(2024, 3, 1)): (Decimal('0.05'), '01a15c38-0b7c-7345-ad1b-44e005b5fb6e')}
```

### PBT store.partitions_complete

Break: missing days are not reported.

```
tests/hat/properties/test_store.py F
tests/hat/properties/test_store.py:176: in test_partitions_complete
tests/hat/properties/test_store.py:187: in test_partitions_complete
E   assert [] == [datetime.date(2024, 3, 1)]
E     
E     Right contains one more item: datetime.date(2024, 3, 1)
E     
```

### PBT ingest.parquet_roundtrip

Break: closes pass through a rounded float.

```
tests/hat/properties/test_ingest.py F
tests/hat/properties/test_ingest.py:145: in test_parquet_roundtrip
tests/hat/properties/test_ingest.py:161: in test_parquet_roundtrip
E   AssertionError: assert [('00000000-0....0000'), ...)] == [('00000000-0....0000'), ...)]
E     
E     At index 0 diff: ('00000000-0000-0000-0000-000000000001', 'INE000000000', 'SYM0', 'EQ', Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0000'), 0, None, 'bhavcopy', '01a15c38-3578-71a3-973a-b66823de55b0') != ('00000000-0000-0000-0000-000000000001', 'INE000000000', 'SYM0', 'EQ', Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0000'), Decimal('0.0001'), Decimal('0.0000'), 0, None, 'bhavcopy', '01a15c38-3578-71a3-973a-b66823de55b0')
E     
```

### PBT ingest.sources_same_schema

Break: a batch with no traded values (MCP rows) is written with an inferred schema.

```
tests/hat/properties/test_ingest.py F
```

### PBT ingest.index_snapshot_stored

Break: a day's file is replaced by the latest write, dropping the other indices.

```
tests/hat/properties/test_ingest.py F
tests/hat/properties/test_ingest.py:201: in test_index_snapshot_stored
tests/hat/properties/test_ingest.py:232: in test_index_snapshot_stored
E   AssertionError: assert [('NIFTY SMAL...l('39.0000'))] == [('INDIA VIX'...l('39.0000'))]
E     
E     At index 0 diff: ('NIFTY SMALLCAP 250', datetime.date(2024, 2, 9), Decimal('39.0000')) != ('INDIA VIX', datetime.date(2024, 2, 9), Decimal('39.0000'))
E     Right contains one more item: ('NIFTY SMALLCAP 250', datetime.date(2024, 2, 9), Decimal('39.0000'))
```

### PBT bhavcopy.format_by_date

Break: 5 Jul 2024 (the last old-format day) is treated as UDiFF.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:39: in test_format_by_date
tests/hat/properties/test_bhavcopy.py:44: in test_format_by_date
E   app.hat.data.bhavcopy.BhavcopyError: bhavcopy_format_gap
E   Failing explicit example: test_format_by_date(
E       day=datetime.date(2024, 7, 5),
E   )
```

### PBT bhavcopy.missing_file_is_holiday_candidate

Break: every candidate counts as a holiday, listed or not.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:89: in test_missing_file_is_holiday_candidate
tests/hat/properties/test_bhavcopy.py:106: in test_missing_file_is_holiday_candidate
E   assert {datetime.date(2012, 1, 2)} == set()
E     
E     Extra items in the left set:
E     datetime.date(2012, 1, 2)
```

### PBT bhavcopy.resume_skips_done

Break: downloaded days are requested again.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:116: in test_resume_skips_done
tests/hat/properties/test_bhavcopy.py:152: in test_resume_skips_done
E   assert False
E    +  where False = <built-in method isdisjoint of set object at 0x7f1499f4f5a0>([datetime.date(2012, 1, 2)])
E    +    where <built-in method isdisjoint of set object at 0x7f1499f4f5a0> = {datetime.date(2012, 1, 2)}.isdisjoint
E    +      where {datetime.date(2012, 1, 2)} = set([datetime.date(2012, 1, 2)])
```

### PBT bhavcopy.rate_limited

Break: the downloader never waits between requests.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:158: in test_rate_limited
tests/hat/properties/test_bhavcopy.py:184: in test_rate_limited
E   assert False
E    +  where False = all(<generator object test_rate_limited.<locals>.<genexpr> at 0x7f0a133df760>)
E   Failing test case: test_rate_limited(
E       n=2,
```

### PBT bhavcopy.parsers_agree

Break: the UDiFF parser reads prev_close from the close column.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:192: in test_parsers_agree
tests/hat/properties/test_bhavcopy.py:202: in test_parsers_agree
E   AssertionError: assert [BhavRow(trad...d_value=None)] == [BhavRow(trad...d_value=None)]
E     
E     At index 0 diff: BhavRow(trade_date=datetime.date(2000, 1, 1), isin='INE000000000', series='EQ', symbol='A', open=Decimal('0.05'), high=Decimal('0.05'), low=Decimal('0.05'), close=Decimal('0.05'), prev_close=Decimal('0.06'), volume=0, traded_value=None) != BhavRow(trade_date=datetime.date(2000, 1, 1), isin='INE000000000', series='EQ', symbol='A', open=Decimal('0.05'), high=Decimal('0.05'), low=Decimal('0.05'), close=Decimal('0.05'), prev_close=Decimal('0.05'), volume=0, traded_value=None)
E     
```

### PBT bhavcopy.traded_value_kept

Break: the parser drops the traded value.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:217: in test_traded_value_kept
tests/hat/properties/test_bhavcopy.py:223: in test_traded_value_kept
E   AssertionError: assert [None] == [Decimal('0.00')]
E     
E     At index 0 diff: None != Decimal('0.00')
E     
```

### PBT bhavcopy.validation_flags_mismatch

Break: closes within five paise count as equal.

```
tests/hat/properties/test_bhavcopy.py F
tests/hat/properties/test_bhavcopy.py:233: in test_validation_flags_mismatch
tests/hat/properties/test_bhavcopy.py:264: in test_validation_flags_mismatch
E   AssertionError: assert False == True
E    +  where False = ValidationReport(row_counts={datetime.date(2024, 1, 1): 1}, samples=[SampleCheck(symbol='AAA', day=datetime.date(2024, 1, 1), ours=Decimal('1.0000'), theirs=Decimal('1.01'))]).failed
E   Failing test case: test_validation_flags_mismatch(
E       # The test always failed when commented parts were varied together.
```

### PBT history_build.offline_end_to_end

Break: every run starts a new run id, so a resumed run writes a second file per day.

```
tests/hat/properties/test_history_build.py F
tests/hat/properties/test_history_build.py:89: in test_build_offline_end_to_end
tests/hat/properties/test_history_build.py:152: in test_build_offline_end_to_end
E   AssertionError: assert ([] == []
E     
E     Full diff:
E       [] and {'build/state...b868a2e38afa'} == {'build/state...51a0527c1759'}
```

### PBT history_build.mcp_index_source_chains

Break: points outside the requested range are kept.

```
tests/hat/properties/test_history_build.py F
tests/hat/properties/test_history_build.py:215: in test_mcp_index_source_chains
tests/hat/properties/test_history_build.py:227: in test_mcp_index_source_chains
E   assert [datetime.date(2021, 1, 1)] == []
E     
E     Left contains one more item: datetime.date(2021, 1, 1)
E     
```

### PBT history_build.mcp_close_source_exact

Break: the latest row on or before the day stands in for the day.

```
tests/hat/properties/test_history_build.py F
tests/hat/properties/test_history_build.py:232: in test_mcp_close_source_exact
tests/hat/properties/test_history_build.py:247: in test_mcp_close_source_exact
E   AssertionError: assert Decimal('1.0') == None
E   Failing test case: test_mcp_close_source_exact(
E       close=Decimal('1.00'),  # or any other generated value
E       other='other_day',
```

### PBT history_build.mcp_corporate_actions_page

Break: a warned (partial) page is reported complete.

```
tests/hat/properties/test_history_build.py F
tests/hat/properties/test_history_build.py:251: in test_mcp_corporate_actions_page
tests/hat/properties/test_history_build.py:265: in test_mcp_corporate_actions_page
E   assert ([] == []
E     
E     Full diff:
E       [] and False is True)
```

### CONF store.data_root

Break: a raw name may climb out of the data root.

```
tests/hat/conformance/test_conf_store.py F
tests/hat/conformance/test_conf_store.py:49: in test_store__data_root
E   Failed: DID NOT RAISE StoreError
```

### CONF store.layout

Break: partitions are named day= instead of date=.

```
tests/hat/conformance/test_conf_store.py F
tests/hat/conformance/test_conf_store.py:80: in test_store__layout
E   AssertionError: assert 'day=2024-05-06' == 'date=2024-05-06'
E     
E     - date=2024-05-06
E     ?   ^^
E     + day=2024-05-06
```

