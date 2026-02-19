# Scripts

This directory now keeps only active operational helpers:

- `quality-checks/` for lint, symbol-format, and bundle checks
- `setup/` for one-time database/bootstrap tasks

## Canonical Data Pipeline

All Phase-1 data build/refresh work is centralized in:

```bash
python -m data_platform.pipelines.phase1_build --asof YYYY-MM-DD --mode full
```

Step-wise subcommands:

```bash
python -m data_platform.pipelines.phase1_build ingest-sources --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-equity --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-nifty50 --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build apply-corp-actions --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-snapshots --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build validate --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build publish --asof YYYY-MM-DD
```

Legacy maintenance scripts were moved to archive for debt reduction:

- `archive/debt-cleanup/2026-02-19/scripts/maintenance/`
