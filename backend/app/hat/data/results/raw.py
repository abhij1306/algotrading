"""The raw cache of fetched answers: every body kept once, gzipped, under the sha256 of its bytes,
and a manifest that maps each URL to its latest answer.

Layout under the cache root (`$HAT_DATA_ROOT/raw/nse-results/` for NSE,
`$HAT_DATA_ROOT/raw/secondary-results/` for the secondary source):

    manifest.jsonl          one line per fetch: url, status, sha256, bytes, fetched_at (UTC)
    blobs/<sha[:2]>/<sha>.gz

A body is written before its manifest line, and a blob is never overwritten (same bytes, same
name), so an interrupted run leaves at most an unreferenced blob and a rerun resumes from the
manifest. A URL fetched again (a listing that grew) appends a new line; the latest line wins.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "manifest.jsonl"
BLOBS = "blobs"


@dataclass(frozen=True)
class Entry:
    url: str
    status: int
    sha256: str
    size: int
    fetched_at: datetime  # aware UTC


def sha256_of(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class RawCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._entries: dict[str, Entry] | None = None

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST

    def blob_path(self, sha: str) -> Path:
        return self.root / BLOBS / sha[:2] / f"{sha}.gz"

    def entries(self) -> dict[str, Entry]:
        if self._entries is None:
            self._entries = {}
            if self.manifest.exists():
                with self.manifest.open(encoding="utf-8") as handle:
                    for line in handle:
                        entry = _entry(line)
                        if entry is not None:
                            self._entries[entry.url] = entry
        return self._entries

    def entry(self, url: str) -> Entry | None:
        return self.entries().get(url)

    def body(self, entry: Entry) -> bytes:
        return gzip.decompress(self.blob_path(entry.sha256).read_bytes())

    def put(self, url: str, status: int, body: bytes, now: datetime | None = None) -> Entry:
        sha = sha256_of(body)
        path = self.blob_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".tmp{os.getpid()}")
            temp.write_bytes(gzip.compress(body, compresslevel=6))
            temp.replace(path)
        entry = Entry(url, status, sha, len(body), now or datetime.now(UTC))
        self.root.mkdir(parents=True, exist_ok=True)
        line = {
            "url": url,
            "status": status,
            "sha256": sha,
            "bytes": len(body),
            "fetched_at": entry.fetched_at.isoformat(),
        }
        with self.manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        self.entries()[url] = entry
        return entry


def _entry(line: str) -> Entry | None:
    """One manifest line; a torn last line (an interrupted write) is skipped."""
    try:
        row = json.loads(line)
        return Entry(
            row["url"],
            int(row["status"]),
            row["sha256"],
            int(row["bytes"]),
            datetime.fromisoformat(row["fetched_at"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
