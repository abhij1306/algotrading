#!/usr/bin/env bash
# Install (or reinstall) the weekly snapshot release as a systemd user timer for this checkout
# (docs/runbook.md, Snapshot and restore). Usage: scripts/install_snapshot_timer.sh DATA_ROOT
# DATA_REPO [PG_DUMP]; PG_DUMP is the pg_dump 18 command (default pg_dump), for example
# "docker exec hatpg pg_dump -U postgres" for the one inside the database container.
set -euo pipefail
data_root="$(realpath "${1:?usage: $0 HAT_DATA_ROOT DATA_REPO [PG_DUMP]}")"
data_repo="$(realpath "${2:?usage: $0 HAT_DATA_ROOT DATA_REPO [PG_DUMP]}")"
pg_dump="${3:-pg_dump}"
repo="$(cd "$(dirname "$0")/.." && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$units"
# each value goes into a quoted systemd Environment= value: no quote or backslash in it; %
# doubled (a systemd specifier), then & and | escaped for the sed replacement
for value in "$repo" "$data_root" "$data_repo" "$pg_dump"; do
  case "$value" in *'"'* | *'\'*) echo "refused: a quote or backslash in $value" >&2; exit 1 ;;
    *) ;;
  esac
done
lit() { printf '%s' "$1" | sed -e 's/%/%%/g' -e 's/[&|]/\\&/g'; }
for unit in hat-snapshot.service hat-snapshot.timer; do
  sed -e "s|@REPO@|$(lit "$repo")|g" -e "s|@DATA_ROOT@|$(lit "$data_root")|g" \
    -e "s|@DATA_REPO@|$(lit "$data_repo")|g" -e "s|@PG_DUMP@|$(lit "$pg_dump")|g" \
    "$repo/deploy/systemd/$unit" >"$units/$unit"
done
systemctl --user daemon-reload
systemctl --user enable --now hat-snapshot.timer
loginctl show-user "$(id -un)" -p Linger | grep -q yes || echo "run: sudo loginctl enable-linger $(id -un)"
systemctl --user list-timers hat-snapshot.timer
