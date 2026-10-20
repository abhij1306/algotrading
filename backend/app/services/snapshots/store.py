"""
The one reader of the phase-1 curated snapshot parquet files and their metadata.

`SnapshotStore(curated_root, metadata_root)` locates each snapshot file, lists the dates
it covers, and loads the rows for one date (the latest when no date is given) into a
DataFrame whose columns have been checked against the data contract. A file is parsed
once per version: the cache is keyed by path, mtime and size, so a rewritten file is
picked up on the next read with no explicit call, and `invalidate()` drops entries
on demand. Reads are thread-safe; concurrent readers of one file share a single parse.

Failures are typed so that callers can map them to status codes without echoing raw
exception text: `SnapshotUnavailableError` (file or pyarrow missing),
`SnapshotDateNotFoundError` (the file has no rows for that date) and
`SnapshotCorruptError` (the file cannot be read or breaks its schema).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ModuleNotFoundError:  # pragma: no cover - exercised only without pyarrow
    pa = None
    pq = None

from ...paths import CURATED_ROOT, METADATA_ROOT

logger = logging.getLogger(__name__)

DATE_COLUMN = "date"
RUN_LOG_FILE = "run_log.jsonl"


class SnapshotKind(Enum):
    """A curated snapshot file: its name and the columns its contract requires."""

    STOCK_DAILY = (
        "snapshot_stock_daily.parquet",
        (
            "date",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "adj_open",
            "adj_high",
            "adj_low",
            "adj_close",
            "adj_volume",
            "corporate_action_flag",
        ),
    )
    NIFTY50_DAILY = (
        "snapshot_nifty50_daily.parquet",
        (
            "date",
            "universe_id",
            "symbol",
            "in_universe",
            "weight",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
            "corporate_action_flag",
        ),
    )

    def __init__(self, filename: str, required_columns: tuple[str, ...]) -> None:
        self.filename = filename
        self.required_columns = required_columns


class SnapshotError(Exception):
    """Base class for snapshot read failures. Messages are safe to show to a client."""


class SnapshotUnavailableError(SnapshotError):
    """The snapshot file does not exist, or pyarrow is not installed."""

    def __init__(self, kind: SnapshotKind, path: Path, *, missing_pyarrow: bool = False) -> None:
        self.kind = kind
        self.path = path
        if missing_pyarrow:
            message = (
                "pyarrow is not installed. Install backend requirements to enable snapshot APIs."
            )
        else:
            message = f"Phase-1 artifact not available: {path}"
        super().__init__(message)


class SnapshotDateNotFoundError(SnapshotError):
    """The snapshot file is readable but has no rows for the requested date."""

    def __init__(self, kind: SnapshotKind, on: date | None) -> None:
        self.kind = kind
        self.on = on
        when = on.isoformat() if on else "any date"
        super().__init__(f"{kind.filename} has no rows for {when}")


class SnapshotCorruptError(SnapshotError):
    """The snapshot file cannot be parsed or does not match its schema.

    The message names the file only; the underlying cause is logged, not exposed.
    """

    def __init__(self, kind: SnapshotKind) -> None:
        self.kind = kind
        super().__init__(f"Phase-1 artifact is unreadable: {kind.filename}")


@dataclass(frozen=True)
class _Loaded:
    version: tuple[int, int]
    frame: pd.DataFrame
    dates: tuple[date, ...]


class SnapshotStore:
    """Reads curated snapshot parquet files and phase-1 metadata under one data root."""

    def __init__(
        self, curated_root: Path = CURATED_ROOT, metadata_root: Path = METADATA_ROOT
    ) -> None:
        self.curated_root = curated_root
        self.metadata_root = metadata_root
        self._lock = threading.Lock()
        self._path_locks: dict[Path, threading.Lock] = {}
        self._cache: dict[Path, _Loaded] = {}

    # --- snapshot parquet --------------------------------------------------------------

    def path(self, kind: SnapshotKind) -> Path:
        return self.curated_root / kind.filename

    def available_dates(self, kind: SnapshotKind) -> list[date]:
        """Every date with at least one row, oldest first."""
        return list(self._get(kind).dates)

    def latest_date(self, kind: SnapshotKind) -> date:
        dates = self._get(kind).dates
        if not dates:
            raise SnapshotDateNotFoundError(kind, None)
        return dates[-1]

    def load(self, kind: SnapshotKind, on: date | None = None) -> pd.DataFrame:
        """The rows for `on` (the latest date when None), with `date` as YYYY-MM-DD text."""
        loaded = self._get(kind)
        target = on if on is not None else (loaded.dates[-1] if loaded.dates else None)
        if target is None:
            raise SnapshotDateNotFoundError(kind, on)
        rows = loaded.frame[loaded.frame[DATE_COLUMN] == target.isoformat()]
        if rows.empty:
            raise SnapshotDateNotFoundError(kind, target)
        return rows.reset_index(drop=True)

    def invalidate(self, kind: SnapshotKind | None = None) -> None:
        """Drop the cached parse of one snapshot file, or of all of them."""
        with self._lock:
            if kind is None:
                self._cache.clear()
            else:
                self._cache.pop(self.path(kind), None)

    def _get(self, kind: SnapshotKind) -> _Loaded:
        path = self.path(kind)
        if pq is None:
            raise SnapshotUnavailableError(kind, path, missing_pyarrow=True)
        version = _file_version(kind, path)
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached.version == version:
                return cached
            path_lock = self._path_locks.setdefault(path, threading.Lock())

        with path_lock:
            # Another thread may have parsed this version while we waited.
            version = _file_version(kind, path)
            with self._lock:
                cached = self._cache.get(path)
            if cached is not None and cached.version == version:
                return cached
            loaded = _parse(kind, path, version)
            with self._lock:
                self._cache[path] = loaded
            return loaded

    # --- metadata ----------------------------------------------------------------------

    def curated_status(self, filenames: Iterable[str]) -> dict[str, dict[str, int | bool]]:
        return {name: _artifact_status(self.curated_root / name) for name in filenames}

    def metadata_status(self, filenames: Iterable[str]) -> dict[str, dict[str, int | bool]]:
        return {name: _artifact_status(self.metadata_root / name) for name in filenames}

    def latest_run_log(self) -> dict[str, Any] | None:
        """The last well-formed entry of `run_log.jsonl`, or None."""
        run_log = self.metadata_root / RUN_LOG_FILE
        if not run_log.exists():
            return None

        lines = [
            line.strip()
            for line in run_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for idx in range(len(lines) - 1, -1, -1):
            try:
                return json.loads(lines[idx])
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed run_log.jsonl line at index %s: %s", idx, exc)
        return None


def to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """JSON-ready rows with plain Python values (nulls as None, not NaN or numpy scalars)."""
    return pa.Table.from_pandas(frame, preserve_index=False).to_pylist()


def atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write `frame` to `path` so that readers see either the old file or the new one.

    The parquet is written to a temporary file in the same directory and then moved over
    `path` with `os.replace`, which is atomic on one filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        frame.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _file_version(kind: SnapshotKind, path: Path) -> tuple[int, int]:
    try:
        stat_result = path.stat()
    except FileNotFoundError as exc:
        raise SnapshotUnavailableError(kind, path) from exc
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _parse(kind: SnapshotKind, path: Path, version: tuple[int, int]) -> _Loaded:
    try:
        frame = pq.read_table(path).to_pandas(types_mapper=pd.ArrowDtype)
    except FileNotFoundError as exc:
        raise SnapshotUnavailableError(kind, path) from exc
    except Exception as exc:
        logger.warning("Failed to read snapshot %s: %s", path, exc)
        raise SnapshotCorruptError(kind) from exc

    missing = [column for column in kind.required_columns if column not in frame.columns]
    if missing:
        logger.warning("Snapshot %s is missing columns %s", path, missing)
        raise SnapshotCorruptError(kind)

    try:
        parsed = pd.to_datetime(frame[DATE_COLUMN].astype(str), format="%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        logger.warning("Snapshot %s has a malformed date column: %s", path, exc)
        raise SnapshotCorruptError(kind) from exc
    if parsed.isna().any():
        logger.warning("Snapshot %s has rows without a date", path)
        raise SnapshotCorruptError(kind)

    frame[DATE_COLUMN] = parsed.dt.strftime("%Y-%m-%d").astype(pd.ArrowDtype(pa.string()))
    dates = tuple(sorted({stamp.date() for stamp in parsed}))
    return _Loaded(version=version, frame=frame, dates=dates)


def _artifact_status(path: Path) -> dict[str, int | bool]:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return {"exists": False, "size_bytes": 0}
    return {"exists": True, "size_bytes": stat_result.st_size}
