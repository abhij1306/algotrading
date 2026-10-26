"""Where the config records live: `HAT_DATA_ROOT/admin/config/<kind>/` (docs/architecture.md,
Storage).

    <kind>/<id>/<rev>.json      one revision of one record; written once, never changed
    <kind>/in_force.json        the pointer: {"seq", "written_at", "records": {id: rev}}
    <kind>/pointers/<seq>.json  every pointer the kind ever had (the history)
    <kind>/.lock                serialises the writers of the kind (flock)

A record changes by a new revision file; what is in force is only what the kind's pointer
names, and the pointer moves in one rename (`os.replace`) after the revisions it names are on
disk, so a reader never sees a half-written change. Every file is validated against its kind's
schema (`records.kind_schema`) when it is read and before it is written. `commit` is a compare
and swap: each write names the revision it was made from, and the whole commit is refused
(`ConfigConflictError`) when another writer moved a record first. `MemoryConfigStore` keeps the same
contract in memory (the backtest ledger, checks).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ..errors import HatError
from ..time import utc_now
from .records import KINDS, ConfigRecordError, kind_schema, record_rules

__all__ = [
    "ConfigConflictError",
    "ConfigStore",
    "FileConfigStore",
    "MemoryConfigStore",
    "Revision",
    "Write",
    "canonical",
    "config_root",
    "next_pointer",
    "validate_revision",
]

POINTER = "in_force.json"


class ConfigConflictError(HatError):
    """A record changed under a writer (`code`: config_conflict) or a revision file already
    holds other content (config_file_differs)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class Revision:
    rev: int
    record: dict[str, Any]


@dataclass(frozen=True)
class Write:
    """A new revision of one record: `base` is the revision it was made from (0 for a record
    not yet stored)."""

    kind: str
    id: str
    base: int
    record: dict[str, Any]


class ConfigStore(Protocol):
    def load(self, kind: str) -> dict[str, Revision]: ...

    def commit(self, writes: Sequence[Write]) -> None: ...


def config_root(data_root: Path) -> Path:
    return data_root / "admin" / "config"


def canonical(doc: Mapping[str, Any]) -> bytes:
    """The one byte form of a JSON document (sorted keys, two-space indent, trailing newline)."""
    return (json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


@cache
def _validator(kind: str) -> Draft202012Validator:
    return Draft202012Validator(kind_schema(KINDS[kind]))


def validate_revision(kind: str, doc: Mapping[str, Any]) -> None:
    """Raise `ConfigRecordError("config_schema")` unless `doc` is a valid revision file of
    `kind`."""
    if kind not in KINDS:
        raise ConfigRecordError("config_kind", kind)
    errors = sorted(_validator(kind).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        where = "/".join(str(p) for p in errors[0].path)
        raise ConfigRecordError("config_schema", f"{kind} {where}: {errors[0].message}")


def revision_doc(write: Write, rev: int) -> dict[str, Any]:
    return {
        "kind": write.kind,
        "id": write.id,
        "rev": rev,
        "written_at": utc_now().isoformat(),
        "record": write.record,
    }


def next_pointer(
    current: Mapping[str, int], seq: int, writes: Iterable[Write]
) -> tuple[int, dict[str, int]]:
    """The kind's next pointer after `writes` (all of that kind): each written record moves to
    the revision after its base. Raises `ConfigConflictError` when a write's base is not the
    record's revision in `current` (another writer moved it first, or a new record exists)."""
    records = dict(current)
    for write in writes:
        if records.get(write.id, 0) != write.base:
            raise ConfigConflictError("config_conflict", f"{write.kind}/{write.id}")
        records[write.id] = write.base + 1
    return seq + 1, records


def _check(writes: Sequence[Write], loaded: Mapping[str, Mapping[str, Revision]]) -> None:
    """Every write keeps its kind's rules against the revision it replaces."""
    for write in writes:
        before = loaded[write.kind].get(write.id)
        record_rules(KINDS[write.kind], None if before is None else before.record, write.record)


class MemoryConfigStore:
    """The store in memory, with the file store's checks and compare and swap."""

    def __init__(self) -> None:
        self._kinds: dict[str, dict[str, Revision]] = {}
        self._lock = threading.Lock()

    def load(self, kind: str) -> dict[str, Revision]:
        return dict(self._kinds.get(kind, {}))

    def commit(self, writes: Sequence[Write]) -> None:
        with self._lock:
            loaded = {w.kind: self.load(w.kind) for w in writes}
            _check(writes, loaded)
            for kind in loaded:
                current = {i: r.rev for i, r in loaded[kind].items()}
                next_pointer(current, 0, [w for w in writes if w.kind == kind])
            for write in writes:
                doc = revision_doc(write, write.base + 1)
                validate_revision(write.kind, doc)
                self._kinds.setdefault(write.kind, {})[write.id] = Revision(
                    write.base + 1, write.record
                )


class FileConfigStore:
    """The store under `root` (`config_root(HAT_DATA_ROOT)`). Revision files are read once and
    kept (they never change); the pointer is read on every `load`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._revisions: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    # --- reading ---------------------------------------------------------------------------

    def pointer(self, kind: str) -> tuple[int, dict[str, int]]:
        """(seq, {id: rev}) of the kind's pointer; (0, {}) before its first write."""
        path = self.root / kind / POINTER
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0, {}
        if (
            not isinstance(doc, dict)
            or not isinstance(doc.get("seq"), int)
            or not isinstance(doc.get("records"), dict)
            or not all(isinstance(v, int) and v >= 1 for v in doc["records"].values())
        ):
            raise ConfigRecordError("config_pointer", kind)
        return doc["seq"], {str(k): v for k, v in doc["records"].items()}

    def revision(self, kind: str, record_id: str, rev: int) -> dict[str, Any]:
        """The validated `record` object of one revision file."""
        key = (kind, record_id, rev)
        with self._cache_lock:
            found = self._revisions.get(key)
        if found is not None:
            return found
        path = self.root / kind / record_id / f"{rev}.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        validate_revision(kind, doc)
        if doc["id"] != record_id or doc["rev"] != rev:
            raise ConfigRecordError("config_schema", f"{path} names {doc['id']}/{doc['rev']}")
        with self._cache_lock:
            self._revisions[key] = doc["record"]
        return doc["record"]

    def load(self, kind: str) -> dict[str, Revision]:
        _, records = self.pointer(kind)
        return {i: Revision(r, self.revision(kind, i, r)) for i, r in records.items()}

    def history(self, kind: str) -> list[dict[str, Any]]:
        """Every pointer the kind had, oldest first."""
        folder = self.root / kind / "pointers"
        if not folder.is_dir():
            return []
        paths = sorted(folder.glob("*.json"), key=lambda p: int(p.stem))
        return [json.loads(p.read_text(encoding="utf-8")) for p in paths]

    # --- writing ---------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, kinds: Iterable[str]) -> Iterator[None]:
        """Hold the kinds' locks, taken in name order so two writers never deadlock."""
        handles = []
        try:
            for kind in sorted(set(kinds)):
                folder = self.root / kind
                folder.mkdir(parents=True, exist_ok=True)
                handle = (folder / ".lock").open("a")
                fcntl.flock(handle, fcntl.LOCK_EX)
                handles.append(handle)
            yield
        finally:
            for handle in reversed(handles):
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def commit(self, writes: Sequence[Write]) -> None:
        """Write every revision, then move each kind's pointer: all or (on a refusal) nothing
        in force. A crash between two kinds' pointers leaves the first kind's change in force
        and the second's revision files unreferenced; nothing reads a file no pointer names."""
        if not writes:
            return
        kinds = sorted({w.kind for w in writes})
        with self._locked(kinds):
            pointers = {k: self.pointer(k) for k in kinds}
            loaded = {k: self.load(k) for k in kinds}
            _check(writes, loaded)
            moved = {
                k: next_pointer(pointers[k][1], pointers[k][0], [w for w in writes if w.kind == k])
                for k in kinds
            }
            for write in writes:
                doc = revision_doc(write, write.base + 1)
                validate_revision(write.kind, doc)
                path = self.root / write.kind / write.id / f"{write.base + 1}.json"
                write_once(path, canonical(doc))
            for kind, (seq, records) in moved.items():
                pointer = {"seq": seq, "written_at": utc_now().isoformat(), "records": records}
                write_once(self.root / kind / "pointers" / f"{seq}.json", canonical(pointer))
                replace_atomically(self.root / kind / POINTER, canonical(pointer))


def _temp_of(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def _write_synced(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_once(path: Path, data: bytes) -> bool:
    """Publish `path` with `data` in one step, never over another file: True when written,
    False when the identical bytes are already there; other bytes there raise
    `ConfigConflictError("config_file_differs")`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_of(path)
    try:
        _write_synced(tmp, data)
        try:
            os.link(tmp, path)  # fails when the name exists: a published file is never replaced
        except FileExistsError:
            if path.read_bytes() == data:
                return False
            raise ConfigConflictError("config_file_differs", str(path)) from None
        return True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def replace_atomically(path: Path, data: bytes) -> None:
    """Replace `path` with `data` in one rename (temp file + fsync + `os.replace`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_of(path)
    try:
        _write_synced(tmp, data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
