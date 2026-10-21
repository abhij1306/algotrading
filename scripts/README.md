# Scripts

Active helpers only (run from repo root):

- `install_backend_deps.py` installs backend deps into `venv/` (fyers-apiv3 with `--no-deps`, Python 3.14 safe)
- `check_symbol_format.py [path]` flags ad-hoc symbol parsing outside `SymbolMaster` (defaults to `backend/app`)

Dev startup is `start.bat` (Windows) or `python start_dev.py` at the repo root.

Backend data/maintenance CLIs live in `backend/scripts/` (run from repo root, e.g.
`python backend/scripts/load_bhavcopy.py`):
`load_bhavcopy`, `refresh_fyers_equity_daily`,
`refresh_fyers_index_daily`, `refresh_fyers_intraday_archive`.

Lint and format are `ruff` (backend) and `pnpm check` (frontend).

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
