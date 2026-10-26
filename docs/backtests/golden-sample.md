# Golden sample

The database and the stores are too large to check completely, so a sample of values is
verified once against independent sources, frozen, and looked up again on every data audit. The
sample leans towards risky spots: ex-dates, renames, delistings and index rebalances. Code:
`app/hat/data/golden.py`. Schema: `golden-sample.schema.json`.

Files: `HAT_DATA_ROOT/audits/golden/YYYY-MM-DD[.N].json` (data, never committed). A check reads
the newest file (by date, then `.N`). A golden file is never edited.

```json
{"format": "golden-sample/1", "created": "2026-10-26", "created_by": "data-review agent",
 "seed": 42, "data_audit_digest": "<the data audit the sample was drawn from>",
 "items": [{"id": "raw-INE002A01018-2024-03-04-close", "kind": "bar_raw",
            "key": {"isin": "INE002A01018", "date": "2024-03-04", "field": "close"},
            "value": 2987.5, "tolerance": {"abs": 0.01}, "risk": "ex_date",
            "verified_from": "NSE bhavcopy 2024-03-04, CLOSE_PRICE"}],
 "mismatches": [{"...item...": "", "store_value": 2990.1}]}
```

`tolerance` is `{"abs": x}`, `{"rel": x}` (of `value`) or `null` (equal). `mismatches` are values
already wrong when the sample was frozen. `risk` is free text (`ex_date`, `rename`, `delisting`,
`rebalance`, `random`, `5m_reject_neighbor`, ...) and is used for counting.

## Keys

An instrument is named by `instrument_id`, `isin` or `symbol`. A symbol is read as of the key's
date (`hat_instrument_symbol_history`), or as the current symbol if no dated symbol matches. A
key that names more than one row is reported as ambiguous.

| `kind` | Key (aliases) | `field` (default) | Store |
| --- | --- | --- | --- |
| `bar_raw` | instrument, `date` | `close` (any raw column) | `bars_daily_raw`, the latest ingest run's row |
| `bar_adjusted` | instrument, `date`, `view` (`store`, `basis`): `bars_daily`/`tr` or `bars_daily_cap`/`cap` | `close` | the adjusted view, by chain |
| `corporate_action` | instrument, `ex_date`, `action_type` (needed when the day has two) | `adjustment_factor` (any column; `exists` returns a bool) | `hat_corporate_action` |
| `membership` | instrument, `date`, `index` (default `NIFTY 500`) | `member` (bool), `effective_from`, `effective_to` | `hat_index_member` |
| `isin_lineage` | `old_*` and `new_*` instrument | `status` (`linked` returns a bool, or any column) | `hat_isin_lineage` |
| | or one instrument | `chain_id`, `chain_isin` | the linked chain |
| `index_close` | `index`, `date` | `close` | `index_daily` |
| `feature` | instrument, `date` | a feature name | `features_daily`, by chain |
| `breadth` | `date`, `dataset` (inferred from the field) | `breadth_pct` | `breadth_daily` or `market_breadth` |
| `bar_5m_daily_agg` | instrument, `date` | `close`; `open`, `high`, `low`, `volume`, `bars` | `bars_5m`, the day's bars aggregated |

## Check and rebase

`python -m app.hat.data.golden check [--file F]` looks up each item. Each item gets one of:
`pass`, `drift` (outside tolerance), `missing` (no such row) or `unresolved` (ambiguous or
malformed key). Each recorded mismatch gets `open` (still the recorded store value), `fixed`
(now the verified value) or `drift`. The command prints counts per status, kind and risk, and the
drift list. It writes the result to `audits/golden/results/`. It exits 2 on any drift, missing or
unresolved item.

The data audit's `golden` check runs the same lookup and names the file and its sha256:

- **Fails:** drift, missing, or an unreadable or invalid golden file.
- **Warns:** an unresolved key, an open mismatch, or no golden file yet.

The audit's cache key includes the golden file's sha256, so a new golden file is checked.

A deliberate data fix (for example, a corrected demerger factor) makes the items it touches
drift. Never edit the golden file. Rebase instead:
`python -m app.hat.data.golden rebase --reason "<why>" --items <id,...|all-drifted>`. It writes a
new dated file in which each named drifted item takes the current store value and records the
reason, the previous value and the source file. It refuses without a reason, and it refuses any
item that did not drift.
