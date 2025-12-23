#!/bin/bash
# Daily Maintenance Runner
# Executes all daily maintenance tasks in sequence
# Add to cron: 0 2 * * * /path/to/run_all.sh

set -e  # Exit on error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/../../.venv/Scripts/python.exe"

echo "========================================"
echo "Daily Maintenance - $(date)"
echo "========================================"

# 1. Health Check
echo ""
echo "Step 1: Health Check"
"$VENV_PYTHON" "$SCRIPT_DIR/data_health_check.py"

# 2. Update Bhavcopy (EOD Data)
echo ""
echo "Step 2: Update Bhavcopy"
"$VENV_PYTHON" "$SCRIPT_DIR/update_bhavcopy.py"

# 3. Recalculate Indicators
echo ""
echo "Step 3: Recalculate Indicators"
"$VENV_PYTHON" "$SCRIPT_DIR/recalculate_indicators.py"

echo ""
echo "========================================"
echo "✅ All maintenance tasks complete"
echo "========================================"
