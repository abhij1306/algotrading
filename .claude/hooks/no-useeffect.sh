#!/usr/bin/env bash
# PostToolUse (Edit|Write): block new useEffect/useLayoutEffect calls in frontend code.
# Compares against the last commit, so existing effects don't trigger it.
set -uo pipefail
source "$(dirname "$0")/lib.sh"

file="$(hook_file_path)"
[[ -n "$file" && -f "$file" ]] || exit 0
root="$(repo_root)"
rel="$(rel_to "$file" "$root")"
[[ "$rel" == frontend/* && "$rel" =~ \.(ts|tsx)$ && "$rel" != frontend/node_modules/* ]] || exit 0

pattern='\buse(Layout)?Effect\s*\('
now="$(grep -cE "$pattern" "$file" || true)"
before="$(git -C "$root" show "HEAD:$rel" 2>/dev/null | grep -cE "$pattern" || true)"
now="${now:-0}"; before="${before:-0}"

if [[ "$now" -gt "$before" ]]; then
  cat >&2 <<EOF
$rel now has $now useEffect/useLayoutEffect calls; the last commit had $before. This project does not use effects. Replace the new effect:
- derived value: compute it during render, or with useMemo if it is expensive
- reacting to a user action: do the work in the event handler
- fetching or polling: use a TanStack Query hook (useQuery with refetchInterval)
- external data that is not an HTTP resource: subscribe with useSyncExternalStore
- redirects: use <Navigate> or a route redirect in frontend/router.tsx
EOF
  exit 2
fi
exit 0
