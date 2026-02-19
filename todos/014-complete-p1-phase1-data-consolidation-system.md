---
status: complete
priority: p1
issue_id: "014"
tags: [phase1, data-platform, nse-data, snapshots, consolidation]
dependencies: ["010"]
---

# Problem Statement
Phase-1 required a single reproducible data system for Jan 2025+ equities and NIFTY50 that avoids repeated manual cleaning and script drift.

# Findings
- Existing `nse_data` and `data_platform` had overlapping raw/processed outputs and legacy scripts.
- NIFTY50 monthly sources had duplicate/variant files and occasional non-50 constituent snapshots.
- Existing processed datasets had parallel adjusted/master outputs with inconsistent lineage.

# Proposed Solutions
## A (recommended)
Implement one orchestrator with deterministic source materialization, curated outputs, validation, checksum manifests, and DB publish indexes.

# Recommended Action
Adopt `python -m data_platform.pipelines.phase1_build --asof YYYY-MM-DD --mode full` as canonical Phase-1 build path.

# Acceptance Criteria
- [x] Canonical root `data_system/` created with `01_sources/` + `04_curated/phase1/` + `05_metadata/phase1/` structure
- [x] `phase1_build.py` supports idempotent subcommands (`ingest-sources`, `build-equity`, `build-universe`, `apply-corp-actions`, `build-snapshots`, `validate`, `publish`)
- [x] Source manifest + checksums + validation reports generated
- [x] Curated artifacts generated for all required Phase-1 datasets
- [x] Snapshot DB index tables and snapshot read APIs implemented
- [x] Legacy inventory mapped with archive-candidate status (no hard delete)

# Work Log
### 2026-02-19
- Implemented `backend/app/models/data_snapshot.py` (`dataset_runs`, `dataset_artifacts`, `snapshot_index_stock`, `snapshot_index_universe`).
- Implemented `backend/app/routers/data_snapshot.py` and wired routes in `backend/app/main.py`.
- Implemented `data_platform/pipelines/phase1_build.py` with full orchestration and subcommand support.
- Materialized source ingestion into `data_system/01_sources`.
- Built and published curated datasets:
  - `equity_ohlcv.parquet`
  - `equity_ohlcv_adj.parquet`
  - `nifty50_weights_monthly.parquet`
  - `nifty50_membership_daily.parquet`
  - `snapshot_stock_daily.parquet`
  - `snapshot_nifty50_daily.parquet`
- Generated metadata artifacts and run logs.
- Removed `IndexInclExcl.xls` fallback from Phase-1 monthly build logic (it is pre-2021 only).
- Moved duplicate legacy monthly raw index files (42 files) from active `nse_data/index_universe/raw/2025_06..2025_12` into archive-first storage.
- Removed hardcoded local downloads path from pipeline; index-weight source now defaults to repo-local source-drop (`nse_data/_source_drop/index_weights`) with `PHASE1_INDEX_WEIGHTS_SRC` override.
- Executed archive-first consolidation pass-2:
  - moved legacy `nse_data/processed`, `raw/equities`, `raw/indices`
  - moved legacy `nse_data/index_universe/{raw,processed,dataset_figshare,parsed,snapshots,validation}`
  - moved `nse_data/IndexInclExcl.xls`
  - move ledger: `archive/data-legacy/2026-02-19/consolidation-pass2-moves.tsv`
- Standardized the pipeline to be universe-agnostic with `build-universe --universe <ID>`, configurable `--start-date`, and `--universes` for repeatable runs across different indices/date windows.
- Removed broker-historical backfill from canonical flow; active source policy is now monthly weightage + corporate actions + bhavcopy only.
- Executed canonical BankNifty/NIFTY50 flow successfully (`build-universe -> build-equity -> apply-corp-actions -> build-snapshots -> validate`).
