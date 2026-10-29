#!/usr/bin/env bash
# Install (or reinstall) the daily data update as a systemd user timer for this checkout
# (docs/runbook.md, Daily data update). Usage: scripts/install_data_update_timer.sh DATA_ROOT
# KOTAK_PYTHON, when set, is the interpreter with neo-api-client and pyotp for the Kotak calls.
set -euo pipefail
data_root="$(realpath "${1:?usage: $0 HAT_DATA_ROOT}")"
repo="$(cd "$(dirname "$0")/.." && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$units"
# a path as a literal sed replacement: its \, & and | escaped
lit() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
for unit in hat-data-update.service hat-data-update.timer; do
  sed -e "s|@REPO@|$(lit "$repo")|g" -e "s|@DATA_ROOT@|$(lit "$data_root")|g" \
    -e "s|@KOTAK_PYTHON@|$(lit "${KOTAK_PYTHON:-}")|g" \
    "$repo/deploy/systemd/$unit" >"$units/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now hat-data-update.timer
loginctl show-user "$(id -un)" -p Linger | grep -q yes || echo "run: sudo loginctl enable-linger $(id -un)"
systemctl --user list-timers hat-data-update.timer
