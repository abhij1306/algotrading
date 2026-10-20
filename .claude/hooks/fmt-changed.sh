#!/usr/bin/env bash
# PostToolUse (Edit|Write): format and lint only the file that was just changed.
# Exit 2 sends remaining errors back to Claude; warnings are ignored.
set -uo pipefail
source "$(dirname "$0")/lib.sh"

file="$(hook_file_path)"
[ -n "$file" ] && [ -f "$file" ] || exit 0
root="$(repo_root)"

backend_rel="$(rel_to "$file" "$root/backend")"
frontend_rel="$(rel_to "$file" "$root/frontend")"

if [ -n "$backend_rel" ] && [[ "$file" == *.py ]]; then
  py=""
  for candidate in "$root/venv/Scripts/python.exe" "$root/venv/bin/python" \
                   "$(git -C "$root" worktree list --porcelain | sed -n '1s/^worktree //p')/venv/Scripts/python.exe"; do
    if [ -x "$candidate" ]; then py="$candidate"; break; fi
  done
  [ -n "$py" ] && "$py" -m ruff --version > /dev/null 2>&1 || exit 0
  cd "$root/backend" || exit 0
  "$py" -m ruff format --quiet "$backend_rel" > /dev/null 2>&1
  "$py" -m ruff check --fix --quiet "$backend_rel" > /dev/null 2>&1
  # Block only on problems this edit added; older ones in the file are left for cleanup PRs.
  now="$("$py" -m ruff check --exit-zero --output-format concise --quiet "$backend_rel" 2>/dev/null)"
  before="$(git -C "$root" show "HEAD:backend/$backend_rel" 2>/dev/null \
    | "$py" -m ruff check --exit-zero --output-format concise --quiet --stdin-filename "$backend_rel" - 2>/dev/null)"
  n_now="$(printf '%s' "$now" | grep -c . || true)"
  n_before="$(printf '%s' "$before" | grep -c . || true)"
  if [ "${n_now:-0}" -gt "${n_before:-0}" ]; then
    printf 'ruff: %s now has %s problems; the last commit had %s. Fix the new ones:\n%s\n' \
      "$backend_rel" "$n_now" "$n_before" "$now" >&2
    exit 2
  fi
  exit 0
fi

if [ -n "$frontend_rel" ] && [[ "$frontend_rel" =~ \.(ts|tsx|js|jsx|mjs|css|json|html|md)$ ]] \
   && [[ "$frontend_rel" != node_modules/* ]] && [[ "$frontend_rel" != out/* ]]; then
  cd "$root/frontend" || exit 0
  [ -d node_modules ] || exit 0
  if ! out="$(pnpm exec vp check --fix "$frontend_rel" 2>&1)"; then
    printf 'vp check found problems in frontend/%s:\n%s\n' "$frontend_rel" \
      "$(printf '%s\n' "$out" | grep -vE '^\s*$|Lockfile|supply-chain' | tail -40)" >&2
    exit 2
  fi
fi
exit 0
