# Data root layout

What lives under `HAT_DATA_ROOT` (default `data_system/hat/`) and beside it, and whether it can be
rebuilt. "Snapshot" means it is in the data repo's releases (runbook, Snapshot and restore);
"manifest" means the daily update lists it in `manifests/` (runbook, Data record).

## Under `HAT_DATA_ROOT`

| Path | Holds | Written by | Rebuild |
| --- | --- | --- | --- |
| `raw/` | NSE source files: bhavcopy zips, index closes | `history_cli`, nightly ingest | No: snapshot, manifest |
| `raw_tri/` | NIFTY 500 TRI JSON from niftyindices | `index_history` | From the source site |
| `bars_daily_raw/date=*/` | Daily bars, unadjusted, per session | `history_cli`, ingest | From `raw/`; manifest |
| `bars_5m/date=*/` | 5-minute bars (Fyers history, Kotak top-up) | `minute_history`, `bars5m_import` | No: snapshot, manifest |
| `bars_5m_rejects/`, `bars_5m_explained/` | Refused 5-minute rows and their explanations | `bars_store`, `bars_basis` | With the import |
| `bars5m_import/`, `bars5m_repair/`, `minute_history/` | Import and repair reports, download progress | those commands | Reports only |
| `bars5m_volume/`, `index_reload/` | The old values each `--apply` replaced (`before-<UTC>.*`) | `bars5m_volume`, `index_history reload` | No: the only copy of the old values |
| `features_daily/date=*/` | Adjusted features per session, `_state.json` | `features` | From bars and the DB; manifest |
| `index_daily/`, `breadth_daily/`, `market_breadth/` | Index levels and market breadth per session | ingest, `features` | From `raw/`; manifest |
| `etf/` | ETF overlay data (`daily`, `indices`, `liquid_tr`, `bars_5m`, `manifest.json`) | `overlay_etf.py` | From the overlay's raw files; manifest |
| `ca_bc/` | NSE book-closure files | `ca.adjust` | From NSE |
| `admin/` | Config store (`admin/config/`), owner rulings and review TSVs | admin commands, configs | No: snapshot |
| `audits/` | Golden, data audit and data verification (`verify-*/`) reports | `golden`, audits, `data.verify` | Rerun |
| `verify_reference/` | Public reference files the data verification downloaded (NSE archives and probes, Wayback lists, niftyindices) and D2's Fyers daily history, each written once | `data.verify --fetch`, `data.verify.fyers` | From the sources; manifest |
| `backtests/` | Backtest runs, `trials.json`, data audits | backtest jobs | Rerun |
| `results/`, `fundamentals/` | Results and fundamentals run logs and backups | results, fundamentals | Logs only |
| `build/`, `history_build/` | Build state and history build reports | `history_cli` | Reports only |
| `update/`, `logs/` | Daily update status and lock, step logs | `app.hat.data.update` | Logs only |
| `work/` | The download tools' working folders (`nifty500`, `kotak_5m`, `overlay`) | daily update steps | Hand-kept `overrides.json` and `symbol_alias.json` in `work/nifty500`: snapshot |
| `tmp/` | DuckDB spill folders (`pid-*`), one per process | `ParquetStore` | Scratch: a folder whose process has ended can go |

Nothing else belongs here: browser profiles, scratch copies and research caches go elsewhere.

## Beside it (the workspace)

| Path | Holds |
| --- | --- |
| the code checkout and `wt/` | The repo and the builders' live worktrees (never cleaned by hand) |
| the data repo clone (`HAT_DATA_REPO`) | Manifests, reference CSVs, snapshot records, research and audits |
| `kotak/` | Kotak raw 5-minute downloads and their tool (snapshot; `venv/` is rebuildable) |
| `ca-data/` | Downloaded NSE corporate action, board meeting and results CSVs (snapshot; CSVs copied to the data repo's `reference/ca-data/`) |
| `research/`, `strategy-notes/`, `briefs/`, `backtests/`, `plans/` | Research record; the text is copied to the data repo's `research/`, caches only in the snapshot |
