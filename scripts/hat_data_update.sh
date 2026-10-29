#!/usr/bin/env bash
# The daily data update (docs/runbook.md, Daily data update); arguments go to the module.
cd "$(dirname "$0")/../backend" && exec "${HAT_PYTHON:-../venv/bin/python}" -m app.hat.data.update "$@"
