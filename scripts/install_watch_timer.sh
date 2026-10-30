#!/usr/bin/env bash
# Install (or reinstall) the health watch as a systemd user timer for this checkout
# (docs/runbook.md, Health watch). Usage: scripts/install_watch_timer.sh DATA_ROOT
set -euo pipefail
data_root="$(realpath "${1:?usage: $0 HAT_DATA_ROOT}")"
repo="$(cd "$(dirname "$0")/.." && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$units"
# a path as a literal sed replacement (its \, & and | escaped) and a literal in a unit (its %
# doubled, its " and \ escaped for the quoted values)
lit() { printf '%s' "$1" | sed -e 's/[\\"]/\\&/g' -e 's/%/%%/g' -e 's/[\\&|]/\\&/g'; }
for unit in hat-watch.service hat-watch-failed.service hat-watch.timer; do
  sed -e "s|@REPO@|$(lit "$repo")|g" -e "s|@DATA_ROOT@|$(lit "$data_root")|g" \
    "$repo/deploy/systemd/$unit" >"$units/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now hat-watch.timer
loginctl show-user "$(id -un)" -p Linger | grep -q yes || echo "run: sudo loginctl enable-linger $(id -un)"
systemctl --user list-timers hat-watch.timer
