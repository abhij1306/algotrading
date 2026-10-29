#!/usr/bin/env bash
# The daily data update (docs/runbook.md, Daily data update); arguments go to the module.
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo/backend" && exec "${HAT_PYTHON:-$repo/venv/bin/python}" -m app.hat.data.update "$@"
