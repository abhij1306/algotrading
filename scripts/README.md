# Scripts

Active helpers only (run from repo root):

- `install_backend_deps.py` installs backend deps into `venv/` (fyers-apiv3 with `--no-deps`, Python 3.14 safe)
- `lock_ci_requirements.py` regenerates `backend/requirements-ci.lock` after a change to `backend/requirements*.txt`
- `export_openapi.py` writes the OpenAPI schema for `pnpm gen:api` in `frontend/`
- `ci_property_shards.py <shard> <shards>` prints the Hypothesis property files one CI shard runs

Backend one-off CLIs live in `backend/scripts/` (run from repo root):
`python backend/scripts/export_symbol_history.py --name symbol_history.csv` exports the legacy
`symbol_history` table to CSV (the only legacy table the owner chose to keep, D68). It reads an
old database; HAT itself starts from a fresh one.

Lint and format are `ruff` (backend) and `pnpm check` (frontend).
