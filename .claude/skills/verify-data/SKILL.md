---
name: verify-data
description: How to run, read and update the checks behind Hire a Trader's historical data build, the bhavcopy parsers for both NSE file formats, the Parquet store and its DuckDB views, corporate-action loading and the adjusted-price view, the index history load, and the daily features (polars indicators, percentiles, breadth, the incremental `features` job). Use before and after any change to backend/app/hat/data/store.py, bhavcopy.py, corporate_actions.py, history_build.py, features.py, backend/app/hat/data/ratios.py, the history methods of mcp_client.py, or the data build and features sections of docs/specs/hat/02-data.md.
---

# verify-data

Checks that the 2010-to-today build writes each trading day once, in the right format era, with exact prices; that Parquet files are written atomically and raw files never change; that the adjusted view equals raw prices times the product of later factors, exactly; that corporate actions load once however many sources and reruns bring them; and that the build is idempotent and resumable. Specs: `docs/specs/hat/02-data.md` §8, §9 (HAT-DATA-011 source schema, 052..055, 080..085, 090..097), `01-decisions.md` (T6, B27, I2), `00-overview.md` (HAT-OVR-052).

The features half (PR 6) checks that every feature of date d uses bars up to d only and equals its §7 definition on adjusted bars; that `rs_pct`, `mom_rank` and breadth are the specified percentiles and counts; that a split changes no feature; that an incremental run equals a full rebuild; and that the job is idempotent. Specs: `02-data.md` §7 (HAT-DATA-070..078), `01-decisions.md` (T11).

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
- `PBT history_build.job_fails_on_any_gap_or_failed_download` The job body succeeds only when every weekday is stored or an explained holiday: a failed download, a missing file that is no holiday or a file with no usable rows raises `history_incomplete`. Harm: a scheduled build reports success with holes in history.
- `PBT history_build.mcp_index_source_chains` Chained `get_index_history` chunks yield every present day in range exactly once and nothing outside. Harm: index history with holes or repeats.
- `PBT history_build.mcp_close_source_exact` The MCP close for a symbol-day is the row dated that day, exactly; other days, missing rows and unreadable closes give None. Harm: validation compares against a neighbouring day.
- `PBT history_build.mcp_corporate_actions_page` An `error` answer is a failed lookup, never an empty list; a `warning` marks the page incomplete. Harm: a failed lookup reads as no corporate actions.
- `CONF store.data_root` The store root is `app.paths.HAT_DATA_ROOT`, defined only there, and raw names cannot leave it. Harm: data written outside the data volume.
- `CONF store.layout` Every Parquet file sits in `<dataset>/date=YYYY-MM-DD/`. Harm: DuckDB partition pruning and the views miss files.
- `PBT features.no_lookahead` Features and breadth of every date d are identical after any bar dated after d is changed, dropped, or more bars are appended (any universe, gaps, ties). Harm: backtests use future data and report edge live trading cannot have.
- `PBT features.formulas` SMA 10/20/50/150/200, 52-week high and low, ADR20, ADTR20, ADV20, turnover20 and ret21/63/126/252 equal a direct Fraction evaluation of the §7 definitions within 1e-9 relative, and are null exactly when the window is not full, over histories with gaps, short and long series. Harm: a wrong ADV or ADTR changes position size and entry filters on every trade.
- `PBT features.turnover_input` A bar's turnover input is close x volume when its traded value is null and the traded value when it is not, bar by bar. Harm: the 10 crore liquidity filter admits illiquid stocks.
- `PBT features.split_invariant` A split stored with its factor, with raw prices before the ex-date divided by it, leaves every price feature, return and percentile of every date as in the unsplit series. Harm: every split looks like a crash and corrupts RS ranks.
- `PBT features.rs_pct` On every date rs_pct is null outside the universe or below 253 bars, else 100 x average rank / count in [0, 100] of 0.4 r3 + 0.2 r6 + 0.2 r9 + 0.2 r12, and a strictly greater weighted return never has a smaller rs_pct (ties share the average rank). Harm: the RS filter admits laggards or rejects leaders.
- `PBT features.mom_rank` mom_rank is the largest universe percentile of ret21, ret63 and ret126 on the date, in [0, 100]; null only when none exists or the instrument is outside the universe. Harm: the momentum filter selects the wrong stocks.
- `PBT features.breadth` Per date, breadth percent is 100 x |close above SMA50| / |SMA50 not null| over the universe, in [0, 100], with exact counts; a flat price is not above its own mean despite float noise. Harm: the regime engine misreads breadth.
- `PBT features.breadth_without_sma50` A universe in which no stock has 50 bars gives null breadth on every date, in the frame and the stored partition, never zero or an error. Harm: a zero breadth reads as a collapse and moves sleeves.
- `PBT features.batching_changes_nothing` Computing a few instruments at a time and dropping early dates (the memory bounds of the full rebuild) gives exactly the one-shot features and breadth. Harm: the full rebuild differs from the nightly run.
- `PBT features.load_lookback` For every instrument, including ones that stopped trading for longer than the slice read, the loader returns the rows of the requested dates plus exactly the lookback rows before them, equal to `bars_daily` (latest run per day), bounded or not. Harm: features of the first dates of a run are null or wrong, and incremental differs from full.
- `PBT features.incremental_equals_full` Runs through successive cut dates, with a corporate action arriving part-way and any chunk size, leave the same features and breadth for every date as one full rebuild. Harm: live features drift from backtest features.
- `PBT features.changed_inputs_rebuild_everything` After a full run, a new corporate action or a new universe, even when the next run stops at an earlier date, leaves no stale date once a later run without a limit follows: every date equals a fresh full rebuild. Harm: part of history keeps the old split adjustment or the old universe's percentiles.
- `PBT features.job_idempotent` The `features` body run twice writes nothing the second time; a crash at any write followed by re-runs ends with the same files and values as an uninterrupted run. Harm: a re-run after a lost job record corrupts or skips dates.
- `PBT features.pandas_equivalence` SMA50/150/200 equal the frozen `stage2_metrics` averages (`backend/tests/hat/fixtures/rs_oracle.json`) and the 0-99 integer rating of `compute_rs_map` equals round(0.99 x rs_pct / 100) (an exact .5 may differ by one); high_52w is never below pandas' (highest high vs highest close). Harm: two RS implementations disagree on the same data.
- `CONF features.schema` Every written features partition has exactly the §7 columns (instrument_id text, the rest float64), unique instrument ids, in `date=` directories. Harm: criteria read a missing or renamed column and score every stock zero.
- `CONF imports.hat_polars_only` No module under `app/hat` imports pandas except listed boundary adapters (only `backtest/metrics.py`, moved unchanged by K1). Harm: two indicator implementations diverge.

## Run

```bash
cd backend
# every pytest, Hypothesis or data command runs under the 2 GB memory cap
CAP="systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=512M --quiet"
CI=1 $CAP python -m pytest tests/hat/properties/test_store.py tests/hat/properties/test_corporate_actions.py \
  tests/hat/properties/test_bhavcopy.py tests/hat/properties/test_history_build.py tests/hat/properties/test_ingest.py tests/hat/properties/test_features.py -q --no-cov
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
- A failing `features.formulas` names the instrument, date and feature. Check the window convention first: a window is the last N rows of that instrument's own bars (a day without a bar is no row), null unless full. A real bug is in `_per_instrument` in `features.py`; a modelling error is a change in the spec's definition (change the spec table, then the naive reference in `test_features.py`).
- A failing `features.no_lookahead` or `features.incremental_equals_full` is always real: some value of date d read a later row, or a run loaded the wrong rows. Look at `load_bars`, the `shift` and `rolling` expressions, and the action digest in `run_features`. Never loosen the comparison. Float values compare within 1e-9 relative because a rolling mean's last bits depend on where its frame starts (about 1e-14); rank columns and counts compare exactly.
- A failing `features.pandas_equivalence` is expected where the definitions differ (see Scope changes); it is real when SMAs or the weighted-return rating differ. The pandas code is legacy: where it conflicts with the spec the spec wins (owner rule D59), so record a difference instead of matching it.
- A failing `features.breadth` on a flat price means the margin against float noise (`ABOVE_MARGIN`) was changed.

## Updating

- A new source format or column: change the spec table, the parser and `tests/hat/hat_bhav_support.py` (the renderers) together; `parsers_agree` fails until both formats agree.
- A new dataset: add its schema and partition rule to `store.py` and the spec's dataset table; `CONF store.layout` covers every Parquet file the store writes.
- A new corporate-action kind: add its wording template to `test_corporate_actions.py` and the parser together.
- After re-confirming everything, update the sync commit and digest: `python backend/tests/hat/conformance/hat_conf_skills.py verify-data` prints the digest.
- A new or changed feature: change the §7 table, `store.FEATURE_NAMES`, `_per_instrument` and the naive reference in `test_features.py` together; `CONF features.schema` fails until the spec and the schema agree. Bump nothing else: the next run rebuilds with `--full`.
- Full-history rebuild: `python -m app.hat.data.features --full` (needs the database for the corporate actions). Measured on synthetic data, 3,000 instruments x 2,500 dates (7.5M rows), chunks of 63 dates, 500 instruments per batch: 180 seconds, peak 1.4 GB, under the 2 GB cap; the 4,150 dates of 2010 to today are expected to take about 5 to 6 minutes. A nightly run of one date takes about a second plus the lookback read.

## Coverage

Code under check:
- `backend/app/hat/data/ratios.py`
- `backend/app/hat/data/store.py`
- `backend/app/hat/data/bhavcopy.py`
- `backend/app/hat/data/corporate_actions.py`
- `backend/app/hat/data/history_build.py`
- `backend/app/hat/data/mcp_client.py`
- `backend/app/hat/data/features.py`

Checks:
- `backend/tests/hat/hat_bhav_support.py`
- `backend/tests/hat/properties/test_store.py`
- `backend/tests/hat/properties/test_corporate_actions.py`
- `backend/tests/hat/properties/test_bhavcopy.py`
- `backend/tests/hat/properties/test_history_build.py`
- `backend/tests/hat/properties/test_ingest.py`
- `backend/tests/hat/conformance/test_conf_store.py`
- `backend/tests/hat/conformance/test_conf_fixtures.py`
- `backend/tests/hat/properties/test_features.py`
- `backend/tests/hat/conformance/test_conf_features.py`
- `backend/tests/hat/fixtures/rs_oracle.json`
- `backend/tests/hat/conformance/test_conf_imports.py`
- `backend/tests/hat/db/test_db_corporate_actions.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `742e0f09885017e4b63ac0050f2ac4dc9f7a6cff`
Covered digest: `f1784cf2c18629d1186c0fe6a131781d6a129a93ee30a9506c5b66b03d8751e8`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- A cumulative factor outside [1e-9, 100) is refused with `store_factor_range` (the view holds factors as decimal(20,18)); `adjusted_view_idempotent` skips such worlds, and `test_cumulative_factor_out_of_range_is_refused` pins the refusal. Several compounded reverse splits could reach it: a follow-up if real data does.
- The adjusted-view exactness properties draw factors with finite decimal reciprocals and at most 18 places (factors are stored to 18 places and their reciprocals, for volume, to 12), so an arbitrary rational factor such as 2/3 is rounded in the view, for prices and volume alike; real factors (splits, bonuses) are ratios of small integers. A bar volume of 10^14 or more is refused (`store_bad_volume`).
- The old CM (`cmDDMMMYYYYbhav.csv`) and UDiFF column lists are `[A]`, written from the published layouts and shared fixtures, not from a real download; the first real run validates them.
- The MCP normaliser for nightly bars is PR 7's; `ingest.sources_same_schema` covers the shared writer that both sources use.
- `bulk_quotes` and its date check do not apply to history: validation compares against the `date` field of `get_stock_history` rows.
- Smallcap 250's first date is `[A]` (default 2010-01-01); a gap is reported, not hidden.
- The index archive CSV parser (`parse_index_archive_csv`) is covered by unit examples only, as MCP is the primary source for index history.
- History-only securities register as inactive instruments with their symbol as name; renames across history are not recorded until a weekly refresh (follow-up).
- Features: windows are over each instrument's own rows, and the generated histories include gaps (a day without a bar), short series, ties and a cloned instrument; the store-based properties (`split_invariant`, `incremental_equals_full`, `job_idempotent`) run 8 examples each with 253 to 330 bars, because each writes one Parquet file per day.
- Features are float64: the spec's "exact for Decimal inputs" is checked against an exact Fraction evaluation within 1e-9 relative, and the first-run frames are not byte-compared across different frame starts (a re-run after a crash gives the same values within 1e-9, not the same bytes; a re-run with nothing to do writes nothing).
- A close counts as above its SMA50 only when it exceeds it by more than a relative 1e-12 (`ABOVE_MARGIN`); the property compares with the exact comparison on generated prices (paise), where the two always agree.
- Differences from the legacy pandas `compute_rs_map` and `stage2_metrics`, recorded and not matched (owner rule D59): `rs_pct` is a 0-100 float (average rank / n x 100), the pandas rating an integer 0-99 (the same rank x 99, rounded); `high_52w` and `low_52w` use the day's high and low, pandas uses closes (so `high_52w >= pandas high_52`); features cover instruments with fewer than 253 bars (null where a window is not full), pandas leaves them out. `features.pandas_equivalence` checks the parts that agree.
- Instruments outside the universe get null `rs_pct` and `mom_rank`; the universe is one static set per run (point-in-time membership arrives with PR 9).
- The advance/decline ratio of HAT-DATA-076 comes from `get_market_breadth` in the nightly ingest (PR 7), not from the features job.
- A correction of bars of an already featured date is not detected: the caller runs with `full`.
- Runs are serialised with a file lock (`.features.lock` under the data root); two concurrent rebuilds are not tested, the lock is `fcntl.flock` as the index upsert uses.

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

### PBT history_build.job_fails_on_any_gap_or_failed_download

Break: the job ignores days with no file that are no holiday and failed downloads.

```
tests/hat/properties/test_history_build.py F
tests/hat/properties/test_history_build.py:175: in test_job_fails_on_any_gap_or_failed_download
tests/hat/properties/test_history_build.py:206: in test_job_fails_on_any_gap_or_failed_download
E   Failed: DID NOT RAISE JobError
E   Failing test case: test_job_fails_on_any_gap_or_failed_download(
E       # The test sometimes passed when commented parts were varied together.
E       world={datetime.date(2024, 6, 24): None,
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

### PBT features.no_lookahead

Break: every rolling mean is read one row ahead (bars up to d+1).

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:214: in test_no_lookahead
tests/hat/properties/test_features.py:247: in test_no_lookahead
E   AssertionError: features of dates <= 2020-01-14 changed by change
E   assert '323c180ddf29f880' == '803b4b8ce3be480d'
E     - 803b4b8ce3be480d
E     + 323c180ddf29f880
```

### PBT features.formulas

Break: the moving-average window is one row short.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:323: in test_formulas
tests/hat/properties/test_features.py:333: in test_formulas
E   AssertionError: ('i00', datetime.date(2020, 1, 14), 'sma10', 4763.652222222223, None)
E   assert False
E    +  where False = close_to(4763.652222222223, None)
E   Failing test case: test_formulas(
```

### PBT features.turnover_input

Break: close x volume is preferred over the stored traded value.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:343: in test_turnover_input
tests/hat/properties/test_features.py:358: in test_turnover_input
E   assert False
E    +  where False = close_to(15514473282.02, Fraction(9180342389, 2000))
E    +    where Fraction(9180342389, 2000) = _mean([Fraction(660166469, 100), Fraction(272736993, 100), Fraction(57996629, 20), Fraction(12808287, 4), Fraction(330630123,
E   Failing test case: test_turnover_input(
```

### PBT features.split_invariant

Break: the loader reads raw bars instead of the adjusted view.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:724: in test_split_invariant
tests/hat/properties/test_features.py:767: in test_split_invariant
E   AssertionError: features of 2020-01-15 jump at the split
E   assert False
E    +  where False = same_features(shape: (3, 18)\n┌───────────────┬──────────┬───────┬───────┬───┬────────┬────────┬────────┬──────────┐\n│ instrument_id ...  ┆ null   
E   Failing test case: test_split_invariant(
```

### PBT features.rs_pct

Break: the 3 and 6 month weights are swapped.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:396: in test_rs_pct
tests/hat/properties/test_features.py:420: in test_rs_pct
E   AssertionError: ('i01', datetime.date(2020, 9, 14), 66.66666666666667, 33.333333333333336)
E   assert False
E    +  where False = <built-in function isclose>(66.66666666666667, 33.333333333333336, rel_tol=1e-12, abs_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
```

Second break: ties take the minimum rank, not the average.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:396: in test_rs_pct
tests/hat/properties/test_features.py:420: in test_rs_pct
E   AssertionError: ('i00', datetime.date(2020, 9, 14), 50.0, 75.0)
E   assert False
E    +  where False = <built-in function isclose>(50.0, 75.0, rel_tol=1e-12, abs_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
```

### PBT features.mom_rank

Break: the smallest percentile instead of the largest.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:428: in test_mom_rank
tests/hat/properties/test_features.py:456: in test_mom_rank
E   AssertionError: ('i00', datetime.date(2020, 3, 9))
E   assert False
E    +  where False = <built-in function isclose>(50.0, 100.0, rel_tol=1e-12, abs_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
```

### PBT features.breadth

Break: the denominator is the whole universe, not the members with an SMA50.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:463: in test_breadth
tests/hat/properties/test_features.py:495: in test_breadth
E   assert False
E    +  where False = <built-in function isclose>(50.0, ((100 * 1) / 1), rel_tol=1e-12)
E    +    where <built-in function isclose> = math.isclose
E   Failing test case: test_breadth(
```

### PBT features.breadth_without_sma50

Break: breadth is 0 when no member has an SMA50.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:499: in test_breadth_without_sma50
tests/hat/properties/test_features.py:505: in test_breadth_without_sma50
E   AssertionError: assert 0 == 1
E    +  where 0 = null_count()
E    +    where null_count = shape: (1,)\nSeries: 'breadth_pct' [f64]\n[\n	0.0\n].null_count
E    +  and   1 = shape: (1, 5)\n┌────────────┬───────────────┬────────────┬─────────────┬─────────────┐\n│ date       ┆ universe_size ┆ w...     ┆ 0          ┆ 0        
```

### PBT features.batching_changes_nothing

Break: the first requested date is dropped.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:252: in test_batching_changes_nothing
tests/hat/properties/test_features.py:261: in test_batching_changes_nothing
E   AssertionError: assert '9ad8226f9cbcddcb' == '698256d6970c1d8f'
E     - 698256d6970c1d8f
E     + 9ad8226f9cbcddcb
E   Failing test case: test_batching_changes_nothing(
```

### PBT features.load_lookback

Break: no deeper read for instruments with a short slice.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:597: in test_load_lookback
tests/hat/properties/test_features.py:670: in test_load_lookback
E   AssertionError: assert {('i0', datet...(2020, 1, 8))} == {('i0', datet..., 1, 8)), ...}
E     Extra items in the right set:
E     ('i1', datetime.date(2020, 1, 6))
E     ('i3', datetime.date(2020, 1, 6))
```

### PBT features.incremental_equals_full

Break: the lookback is 251 rows.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:688: in test_incremental_equals_full
tests/hat/properties/test_features.py:714: in test_incremental_equals_full
E   AssertionError: features of 2020-09-15 differ
E   assert False
E    +  where False = same_features(shape: (1, 18)\n┌───────────────┬──────────┬───────────┬───────────┬───┬──────────┬────────┬────────┬──────────┐\n│ inst... ┆ null   ┆
E   Failing test case: test_incremental_equals_full(
```

Second break: a change in the corporate actions does not force a full rebuild.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:688: in test_incremental_equals_full
tests/hat/properties/test_features.py:714: in test_incremental_equals_full
E   AssertionError: features of 2020-02-04 differ
E   assert False
E    +  where False = same_features(shape: (1, 18)\n┌───────────────┬──────────┬───────┬───────┬───┬────────┬────────┬────────┬──────────┐\n│ instrument_id ...  ┆ null   
E   Failing test case: test_incremental_equals_full(
```

### PBT features.changed_inputs_rebuild_everything

Break: the new inputs digest is recorded even when the rebuild stopped at an earlier date.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:725: in test_changed_inputs_rebuild_everything
tests/hat/properties/test_features.py:756: in test_changed_inputs_rebuild_everything
E   AssertionError: features of 2020-01-15 are stale
E   assert False
E    +  where False = same_features(shape: (1, 18)\n┌───────────────┬──────────┬───────┬──────
```

Second break: the universe is left out of the inputs digest.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:725: in test_changed_inputs_rebuild_everything
tests/hat/properties/test_features.py:758: in test_changed_inputs_rebuild_everything
E   AssertionError: assert 'b6a12e93b449ff89' == '443b2dcc24581983'
E     
E     - 443b2dcc24581983
```

### PBT features.job_idempotent

Break: a partition file is named by a random id, so re-runs leave different files.

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:783: in test_job_idempotent
tests/hat/properties/test_features.py:821: in test_job_idempotent
E   AssertionError: assert dict_keys(['b...32d.parquet']) == dict_keys(['b...d41.parquet'])
E     Full diff:
E     - dict_keys(['breadth_daily/date=2020-01-06/part-breadth.parquet', 'breadth_daily/date=2020-01-07/part-breadth.parquet', 'breadth_daily/date=2020-01-08/part-breadth
E     + dict_keys(['breadth_daily/date=2020-01-06/part-breadth.parquet', 'breadth_daily/date=2020-01-07/part-breadth.parquet', 'breadth_daily/date=2020-01-08/part-breadth
```

Second break: a date counts as done when only its features file exists (a crash between the features and breadth writes is never repaired).

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:783: in test_job_idempotent
tests/hat/properties/test_features.py:821: in test_job_idempotent
E   AssertionError: assert dict_keys(['b...res.parquet']) == dict_keys(['b...res.parquet'])
E     Full diff:
E     - dict_keys(['breadth_daily/date=2020-01-06/part-breadth.parquet', 'breadth_daily/date=2020-01-07/part-breadth.parquet', 'breadth_daily/date=2020-01-08/part-breadth
E     ?                                                                                                                       --------------------------------------------
```

### PBT features.pandas_equivalence

Break: weights swapped (the pandas rating no longer matches).

```
tests/hat/properties/test_features.py F
tests/hat/properties/test_features.py:834: in test_pandas_equivalence
tests/hat/properties/test_features.py:869: in test_pandas_equivalence
E   AssertionError: ('i0', 50, 99)
E   assert (50 == 99 or (True and 49 == 1))
E    +  where 49 = abs((50 - 99))
E   Failing test case: test_pandas_equivalence(
```

### CONF features.schema

Break: the `mom_rank` column is dropped from the schema.

```
tests/hat/conformance/test_conf_features.py F
tests/hat/conformance/test_conf_features.py:69: in test_features__schema
E   AssertionError: assert ['instrument_...'sma200', ...] == ['instrument_...'sma200', ...]
E     Right contains one more item: 'mom_rank'
E     Full diff:
E       [
```

### CONF imports.hat_polars_only

Break: `features.py` imports pandas.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:146: in test_imports__hat_polars_only
E   AssertionError: assert {'data/features.py': 'pandas'} == {}
E     Left contains 1 more item:
E     {'data/features.py': 'pandas'}
E     Full diff:
```
