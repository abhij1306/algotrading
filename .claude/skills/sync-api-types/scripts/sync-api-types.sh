#!/usr/bin/env bash
# Regenerate frontend/lib/api/schema.d.ts from the FastAPI OpenAPI schema,
# then check only the frontend files that import it.
# Set PYTHON to override the interpreter (default: repo venv, then python on PATH).
set -euo pipefail

repo="$(git rev-parse --show-toplevel)"
frontend="$repo/frontend"
out="$frontend/lib/api/schema.d.ts"
cache="$frontend/node_modules/.cache/sync-api-types"
spec="$cache/openapi.json"
mkdir -p "$cache" "$(dirname "$out")"

py="${PYTHON:-}"
if [ -z "$py" ]; then
  main="$(git -C "$repo" worktree list --porcelain | sed -n '1s/^worktree //p')"
  for base in "$repo" "$main"; do
    for candidate in "$base/venv/bin/python" "$base/venv/Scripts/python.exe"; do
      if [ -x "$candidate" ]; then py="$candidate"; break 2; fi
    done
  done
fi
py="${py:-python}"

echo "== Exporting OpenAPI schema with $py"
(cd "$repo/backend" && "$py" -c "
import json, sys
from app.main import app
with open(sys.argv[1], 'w', encoding='utf-8') as f:
    json.dump(app.openapi(), f, indent=2, sort_keys=True)
" "$spec" > "$cache/export.log" 2>&1) || { cat "$cache/export.log"; exit 1; }

echo "== Generating $out"
# Isolated, pinned run: openapi-typescript needs the TypeScript 5 JS API, and the project uses TypeScript 7.
(cd "$frontend" && pnpm dlx --package=openapi-typescript@7.13.0 --package=typescript@5.9.3   openapi-typescript "$spec" -o "$out" --alphabetize)
(cd "$frontend" && pnpm exec vp fmt lib/api/schema.d.ts > /dev/null)

echo "== Changes"
if git -C "$repo" ls-files --error-unmatch "$out" > /dev/null 2>&1; then
  git -C "$repo" diff --stat -- "$out"
else
  echo "schema.d.ts is new (untracked)"
fi

echo "== Checking files that import the schema"
mapfile -t importers < <(cd "$frontend" && grep -rlE "lib/api/schema" --include='*.ts' --include='*.tsx' \
  app components lib 2>/dev/null || true)
if [ "${#importers[@]}" -eq 0 ]; then
  echo "No files import lib/api/schema yet."
else
  (cd "$frontend" && pnpm exec vp check "${importers[@]}")
fi
