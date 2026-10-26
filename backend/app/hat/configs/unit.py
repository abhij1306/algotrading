"""The config records a database session reads and changes (`configs_of(session)`).

A session's sessionmaker names its config store (`session.info[STORE]`: the file store under
`HAT_DATA_ROOT` in `app.database`, a memory store in the backtest ledger). The unit loads each
kind once per transaction and hands out one instance per record, so code reads and sets fields
on records as it did on rows. At the session's commit, once the database
rows are flushed and before the database commits, every new record and every record whose fields
changed is written as its next revision in one `store.commit` (a compare and swap against the
revisions the transaction read): a refused write (`ConfigConflictError`, `ConfigRecordError`,
`CoolingOffError`) raises out of the commit, and the database transaction rolls back with it.
Only a failure of the database's own COMMIT after that leaves the config change without the rows
and events of its transaction. A rollback, or the end of the transaction, drops the
unit, so the next transaction reads the records afresh.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from ..errors import HatError
from ..governance import VersionStatus
from .records import ConfigRecordError, Record, StrategyVersion, doc_of, record_from_doc
from .store import ConfigStore, Write

__all__ = [
    "RUNS_ROOT",
    "STORE",
    "ConfigUnavailableError",
    "ConfigUnit",
    "configs_of",
    "data_root_of",
]

STORE = "hat_config_store"
# the session's `HAT_DATA_ROOT` (a `Path`): where the gates read the backtest run folders
RUNS_ROOT = "hat_data_root"
_UNIT = "hat_config_unit"

R = TypeVar("R", bound=Record)


class ConfigUnavailableError(HatError):
    """The session was made without a config store."""


class ConfigUnit:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._records: dict[str, dict[str, Record]] = {}
        self._base: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}

    def _kind(self, cls: type[R]) -> dict[str, R]:
        kind = cls.KIND
        if kind not in self._records:
            loaded: dict[str, Record] = {}
            for key, revision in self.store.load(kind).items():
                loaded[key] = record_from_doc(cls, revision.record)
                self._base[(kind, key)] = (revision.rev, revision.record)
            self._records[kind] = loaded
        return self._records[kind]  # type: ignore[return-value]

    def all(self, cls: type[R], where: Callable[[R], bool] | None = None) -> list[R]:
        """The kind's records (those added in this transaction included), by key."""
        found = sorted(self._kind(cls).items())
        return [r for _, r in found if where is None or where(r)]

    def get(self, cls: type[R], key: uuid.UUID | str | None) -> R | None:
        if key is None:
            return None
        return self._kind(cls).get(str(key))

    def add(self, record: Record) -> Record:
        """Store `record` as a new record at the commit; a key already taken is refused."""
        records = self._kind(type(record))
        if record.key in records and records[record.key] is not record:
            raise ConfigRecordError("config_duplicate", f"{record.KIND}/{record.key}")
        records[record.key] = record
        return record

    def pending(self) -> list[Write]:
        """The writes the commit makes: new records, and records whose fields changed."""
        writes = []
        for kind, records in sorted(self._records.items()):
            for key, record in sorted(records.items()):
                doc = doc_of(record)
                base = self._base.get((kind, key))
                if base is None:
                    writes.append(Write(kind, key, 0, doc))
                elif base[1] != doc:
                    writes.append(Write(kind, key, base[0], doc))
        _one_released(self._records.get(StrategyVersion.KIND, {}).values())
        return writes

    def flush(self) -> None:
        writes = self.pending()
        if not writes:
            return
        self.store.commit(writes)
        for write in writes:
            self._base[(write.kind, write.id)] = (write.base + 1, write.record)


def _one_released(versions: Iterable[Record]) -> None:
    """One released version per strategy (HAT-PORT-020)."""
    released: set[uuid.UUID] = set()
    for version in versions:
        assert isinstance(version, StrategyVersion)
        if version.status == VersionStatus.RELEASED:
            if version.strategy_id in released:
                raise ConfigRecordError("one_released_version", str(version.strategy_id))
            released.add(version.strategy_id)


def configs_of(session: Session) -> ConfigUnit:
    """The config records of `session`'s transaction."""
    unit = session.info.get(_UNIT)
    if unit is None:
        store = session.info.get(STORE)
        if store is None:
            raise ConfigUnavailableError("session_without_config_store")
        unit = session.info[_UNIT] = ConfigUnit(store)
    return unit


def data_root_of(session: Session) -> Path | None:
    """The data root the session's sessionmaker names (None: no run folder is read)."""
    root = session.info.get(RUNS_ROOT)
    return root if isinstance(root, Path) else None


@event.listens_for(Session, "before_commit")
def _write_configs(session: Session) -> None:
    unit = session.info.get(_UNIT)
    if unit is not None and unit.pending():
        session.flush()  # the database refuses its rows first (a constraint, a trigger)
        unit.flush()


@event.listens_for(Session, "after_transaction_end")
def _drop_unit(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(_UNIT, None)
