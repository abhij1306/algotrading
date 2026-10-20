"""Write the backend OpenAPI schema to a JSON file without starting the server.

Importing ``app.main`` registers the routers but does not run the lifespan, so no
database, broker or websocket connection is opened. The output uses sorted keys and
LF line endings so the generated frontend types are byte-stable across machines.

Usage (with the backend venv active; the working directory does not matter):
    python scripts/export_openapi.py

It always writes ``frontend/openapi.json`` (gitignored). The frontend ``pnpm gen:api``
script calls this and then regenerates ``lib/api/schema.d.ts``.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
OUTPUT = REPO_ROOT / "frontend" / "openapi.json"


def build_schema() -> dict:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.main import app  # noqa: PLC0415 - must follow the sys.path change

    return app.openapi()


def main() -> int:
    schema = build_schema()
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    OUTPUT.write_bytes(text.encode("utf-8"))
    print(f"Wrote OpenAPI schema to {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
