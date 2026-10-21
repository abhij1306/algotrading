"""Print the Hypothesis property files one CI shard runs (the `verification-properties` matrix).

Usage: python scripts/ci_property_shards.py <shard> <shards>   (shard is 1-based)

Files are the sorted `backend/tests/hat/properties/test_*.py`, dealt round-robin, so the split is
deterministic and every file lands in exactly one shard (CONF `verification.ci_job` checks this
against the matrix size in ci.yml). Paths are relative to `backend/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROPERTIES = Path(__file__).resolve().parents[1] / "backend" / "tests" / "hat" / "properties"


def shard_files(shard: int, shards: int) -> list[str]:
    if not 1 <= shard <= shards:
        raise ValueError(f"shard {shard} outside 1..{shards}")
    files = sorted(PROPERTIES.glob("test_*.py"))
    return [f"tests/hat/properties/{p.name}" for p in files[shard - 1 :: shards]]


def main(argv: list[str]) -> int:
    shard, shards = int(argv[1]), int(argv[2])
    print("\n".join(shard_files(shard, shards)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
