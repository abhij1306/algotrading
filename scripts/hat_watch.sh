#!/usr/bin/env bash
# The health watch (docs/runbook.md, Health watch); arguments go to the module.
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo/backend" && exec "${HAT_PYTHON:-$repo/venv/bin/python}" -m app.hat.ops.watch "$@"
