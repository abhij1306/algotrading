# Scripts

Active helpers only (run from repo root):

- `install_backend_deps.py` installs backend deps into `venv/` (fyers-apiv3 with `--no-deps`, Python 3.14 safe)
- `lock_ci_requirements.py` regenerates `backend/requirements-ci.lock` after a change to `backend/requirements*.txt`
- `export_openapi.py` writes the OpenAPI schema for `pnpm gen:api` in `frontend/`
- `hat_backup.py run --local DIR --remote DIR` takes the nightly HAT backup: a `pg_dump`
  (custom format) and a tar of `HAT_DATA_ROOT`, written to both targets with a `manifest.json`
  (size and sha256 of every file) and re-verified at both; exit 1 on any mismatch.
  `hat_backup.py verify SET_DIR` re-checks one set. Targets default to `HAT_BACKUP_LOCAL` and
  `HAT_BACKUP_REMOTE`; the password reaches `pg_dump` only via `PGPASSWORD`/`~/.pgpass`
  (HAT-OPS-060, runbook in `docs/runbook.md`)
- `hat_deploy_guard.py [--calendar FILE] [--force --reason TEXT]` exits 2 between 08:30 and
  15:35 IST on a trading day (every weekday unless the calendar export says otherwise) and 0
  otherwise; a forced run is logged to `<HAT_DATA_ROOT>/ops/deploy_guard.jsonl` (HAT-OPS-042).
  Run it before every deploy or planned restart

Backend one-off CLIs live in `backend/scripts/` (run from repo root):
`python backend/scripts/export_symbol_history.py --name symbol_history.csv` exports the legacy
`symbol_history` table to CSV (the only legacy table the owner chose to keep, D68). It reads an
old database; HAT itself starts from a fresh one.

Lint and format are `ruff` (backend) and `pnpm check` (frontend).
