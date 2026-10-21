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
root_rel="$(rel_to "$file" "$root")"

# The repo venv: this worktree's, else the main checkout's (a worktree shares its venv).
find_python() {
  local main base candidate
  main="$(git -C "$root" worktree list --porcelain | sed -n '1s/^worktree //p')"
  for base in "$root" "$main"; do
    for candidate in "$base/venv/bin/python" "$base/venv/Scripts/python.exe"; do
      if [ -x "$candidate" ]; then printf '%s' "$candidate"; return 0; fi
    done
  done
  return 1
}

# Python under backend/ uses backend/pyproject.toml; Python elsewhere (scripts/, verification/)
# uses the root ruff.toml, which extends it.
if [[ "$file" == *.py && ( -n "$backend_rel" || ( -n "$root_rel" && -z "$frontend_rel" ) ) ]]; then
  py="$(find_python)" || exit 0
  "$py" -m ruff --version > /dev/null 2>&1 || exit 0
  if [ -n "$backend_rel" ]; then
    workdir="$root/backend"; target="$backend_rel"; repo_path="backend/$backend_rel"
  else
    workdir="$root"; target="$root_rel"; repo_path="$root_rel"
  fi
  cd "$workdir" || exit 0
  "$py" -m ruff format --quiet "$target" > /dev/null 2>&1
  "$py" -m ruff check --fix --quiet "$target" > /dev/null 2>&1
  # Block only on problems this edit added; older ones in the file are left for cleanup PRs.
  now="$("$py" -m ruff check --exit-zero --output-format concise --quiet "$target" 2>/dev/null)"
  before="$(git -C "$root" show "HEAD:$repo_path" 2>/dev/null \
    | "$py" -m ruff check --exit-zero --output-format concise --quiet --stdin-filename "$target" - 2>/dev/null)"
  n_now="$(printf '%s' "$now" | grep -c . || true)"
  n_before="$(printf '%s' "$before" | grep -c . || true)"
  if [ "${n_now:-0}" -gt "${n_before:-0}" ]; then
    printf 'ruff: %s now has %s problems; the last commit had %s. Fix the new ones:\n%s\n' \
      "$target" "$n_now" "$n_before" "$now" >&2
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
