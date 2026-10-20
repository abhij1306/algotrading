---
name: sync-api-types
description: Regenerate the frontend's TypeScript API types from the FastAPI OpenAPI schema and report frontend code that no longer matches the backend contract.
disable-model-invocation: true
---

# Sync API types

The frontend's backend contract lives in `frontend/lib/api/schema.d.ts`. It is generated, so never edit it by hand.

## Steps

1. Run the script from the repo root. It exports the schema by importing `app.main` (no running server needed), generates the types with openapi-typescript, shows the diff, and runs `vp check` on files that import the schema:

   ```bash
   bash .claude/skills/sync-api-types/scripts/sync-api-types.sh
   ```

   If the backend import fails, set `PYTHON` to the venv interpreter and run it again.

2. Read the schema diff and summarise contract changes for the user: endpoints added or removed, fields renamed, and types changed.

3. For each `vp check` error, name the file and the endpoint it calls, then say what changed in the contract. Fix call sites only if the user asks.

4. If an endpoint returns a loose type (`Record<string, never>`, `unknown` or a missing response body), say so. The backend route needs a `response_model` before the frontend can rely on it.

## Using the types

Import from the generated module and index by path and method instead of writing response interfaces in pages:

```ts
import type { paths } from "@/lib/api/schema";

type ScreenerResponse =
  paths["/api/screener/"]["get"]["responses"]["200"]["content"]["application/json"];
```
