# Scripts

Active helpers only (run from repo root):

- `install_backend_deps.py` installs backend deps into `venv/` (fyers-apiv3 with `--no-deps`, Python 3.14 safe)
- `check_symbol_format.py [path]` flags ad-hoc symbol parsing outside `SymbolMaster` (defaults to `backend/app`)

Backend one-off CLIs live in `backend/scripts/` (run from repo root):
`python backend/scripts/export_symbol_history.py --out symbol_history.csv` exports the legacy
`symbol_history` table to CSV (the only legacy table the owner chose to keep, D68).

Lint and format are `ruff` (backend) and `pnpm check` (frontend).

Legacy maintenance scripts were moved to archive for debt reduction:

- `archive/debt-cleanup/2026-02-19/scripts/maintenance/`
