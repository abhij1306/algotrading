#!/usr/bin/env bash
# The weekly snapshot release (docs/runbook.md, Snapshot and restore): both database dumps and the
# raw/ and bars_5m/ files new or changed since the earlier snapshots, as an incremental release of
# the data repo. HAT_DATA_ROOT, HAT_DATA_REPO (the clone) and HAT_PG_DUMP come from the unit.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
clone="${HAT_DATA_REPO:-/data/workspace/algotradingdata}"
root="${HAT_DATA_ROOT:?HAT_DATA_ROOT is not set}"
github="$(cd "$clone" && gh repo view --json nameWithOwner -q .nameWithOwner)"
exec "${HAT_PYTHON:-$repo/venv/bin/python}" "$repo/scripts/hat_snapshot.py" release \
  --tag "snapshot-$(date -u +%F)" --repo "$github" --record "$clone" --incremental \
  --db "${DB_NAME:-hat_live}" --db "${DB_STATE_NAME:-hat_state}" \
  --part "raw=$root/raw" --part "bars_5m=$root/bars_5m"
