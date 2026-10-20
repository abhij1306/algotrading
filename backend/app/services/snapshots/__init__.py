"""Phase-1 curated snapshot reads (see `store.py`)."""

from functools import cache

from .store import (
    SnapshotCorruptError,
    SnapshotDateNotFoundError,
    SnapshotError,
    SnapshotKind,
    SnapshotStore,
    SnapshotUnavailableError,
    atomic_write_parquet,
    to_records,
)


@cache
def get_snapshot_store() -> SnapshotStore:
    """The process-wide store over `paths.CURATED_ROOT`; a FastAPI dependency."""
    return SnapshotStore()


__all__ = [
    "SnapshotCorruptError",
    "SnapshotDateNotFoundError",
    "SnapshotError",
    "SnapshotKind",
    "SnapshotStore",
    "SnapshotUnavailableError",
    "atomic_write_parquet",
    "get_snapshot_store",
    "to_records",
]
