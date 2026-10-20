"""Write the backend OpenAPI schema to a JSON file without starting the server.

Importing ``app.main`` registers the routers but does not run the lifespan, so no
database, broker or websocket connection is opened. The output uses sorted keys and
LF line endings so the generated frontend types are byte-stable across machines.

Usage (from the repo root, with the backend venv active):
    python scripts/export_openapi.py [output_path]

The default output path is ``frontend/openapi.json`` (gitignored). The frontend
``pnpm gen:api`` script calls this and then regenerates ``lib/api/schema.d.ts``.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
DEFAULT_OUTPUT = REPO_ROOT / "frontend" / "openapi.json"


def build_schema() -> dict:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.main import app  # noqa: PLC0415 - must follow the sys.path change

    return app.openapi()


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    schema = build_schema()
    text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    print(f"Wrote OpenAPI schema to {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
