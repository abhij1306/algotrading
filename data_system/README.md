# Data System (Phase-1 Active Scope)

Canonical active dataset root for Phase-1 (2025 onward only).

## Active Structure

- `01_sources/`
- `01_sources/nse_bhavcopy/` - 2025+ bhavcopy source files
- `01_sources/nse_corporate_actions/` - corporate actions source CSV
- `01_sources/nse_index_weights_pdf/` - NIFTY50 monthly PDF source files (2025+)
- `03_universe/constituents/` - index constituent master files
- `03_universe/monthly_universe_raw/` - monthly universe snapshots used by pipeline
- `04_curated/phase1/` - curated parquet artifacts
- `05_metadata/reference/` - reference metadata
- `05_metadata/phase1/` - manifest/checksum/validation/run logs

## Archived Scope

Historical/pre-2025 source files and legacy processing assets are archived under:
- `archive/debt-cleanup/2026-02-19/`

They are intentionally not part of active Phase-1 runtime.
