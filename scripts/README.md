# Scripts

Active helpers only (run from repo root):

- `install_backend_deps.py` installs backend deps into `venv/` (fyers-apiv3 with `--no-deps`, Python 3.14 safe)
- `check_symbol_format.py [path]` flags ad-hoc symbol parsing outside `SymbolMaster` (defaults to `backend/app`)

Backend data/maintenance CLIs live in `backend/scripts/` (run from repo root, e.g.
`python backend/scripts/load_bhavcopy.py`):
`load_bhavcopy`, `refresh_fyers_equity_daily`,
`refresh_fyers_index_daily`, `refresh_fyers_intraday_archive`.

Lint and format are `ruff` (backend) and `pnpm check` (frontend).

Legacy maintenance scripts were moved to archive for debt reduction:

- `archive/debt-cleanup/2026-02-19/scripts/maintenance/`
