#!/usr/bin/env bash
# Start one HAT builder: a git worktree for the PR, its own herdr tab, and a Claude agent in it
# (main model, auto mode), then send it the brief. Run from inside herdr (HERDR_ENV=1).
#
#   scripts/agents/start-builder.sh <agent-name> <branch> <brief-file>
#   scripts/agents/start-builder.sh pr12 hat/pr12-backtest $HAT_WS/briefs/pr12.txt
#
# An existing worktree (an open PR's) is reused. At most two builders run at once; this script
# refuses a third.
set -euo pipefail

name=$1 branch=$2 brief=$3
HAT_WS=${HAT_WS:-/data/workspace}
main=$HAT_WS/algotrading
wt=$HAT_WS/wt/$branch
MAX_BUILDERS=2

[ "${HERDR_ENV:-}" = 1 ] || { echo "not inside herdr: start herdr, run claude there, then retry"; exit 1; }
[ -f "$brief" ] || { echo "brief not found: $brief"; exit 1; }

running=$(herdr agent list | python3 -c '
import json, sys
me = sys.argv[1]
print(sum(1 for a in json.load(sys.stdin)["result"]["agents"] if a.get("name") and a["pane_id"] != me))
' "${HERDR_PANE_ID:-}")
[ "$running" -lt "$MAX_BUILDERS" ] || { echo "$running builders already running (max $MAX_BUILDERS)"; exit 1; }

if [ ! -d "$wt" ]; then
  git -C "$main" fetch -q origin
  if git -C "$main" ls-remote --exit-code --heads origin "$branch" >/dev/null; then
    git -C "$main" worktree add -q "$wt" -B "$branch" "origin/$branch"
  else
    git -C "$main" worktree add -q "$wt" -b "$branch" origin/main
  fi
fi

pane=$(herdr tab create --workspace "${HERDR_WORKSPACE_ID:-w1}" --cwd "$wt" --label "$name" --no-focus |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["root_pane"]["pane_id"])')

# The first start in a new folder shows Claude's folder-trust dialog; the worktree is this repo.
if ! herdr agent start "$name" --kind claude --pane "$pane" --timeout 60000 -- --permission-mode auto >/dev/null 2>&1; then
  if herdr agent read "$name" --source visible --lines 30 | grep -q 'trust this folder'; then
    herdr agent send-keys "$name" down >/dev/null
    herdr agent send-keys "$name" enter >/dev/null
  fi
  herdr agent wait "$name" --until idle --timeout 60000 >/dev/null
fi

herdr agent prompt "$name" "$(cat "$brief")" >/dev/null
herdr agent wait "$name" --until working --timeout 30000 >/dev/null
echo "builder $name started: tab pane $pane, worktree $wt"
