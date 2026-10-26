"""The one-off storage split (docs/runbook.md, Storage split).

    python -m app.hat.ops.state_split [--check]

Before the split hat_live held everything. This command moves what is not stock data out of it:
each config row (`schema.LEGACY_CONFIG_TABLES`) becomes revision 1 of its config record under
`HAT_DATA_ROOT/admin/config` (same id), and each row of a hat_state table is copied to hat_state
(`DB_STATE_NAME`, created when missing, with its schema). Then it reads both sides back, compares
the counts and a digest of every table and kind, and prints the report as JSON. It reads hat_live
through its read-only connection, so it never drops or changes anything there; the owner drops the
legacy tables later, by hand.

It is safe to repeat: a record already exported with the same content and a row already copied
are left as they are. A record whose revision 1 differs from its row is refused
(`config_file_differs`, exit 2) and nothing of that kind is written. `--check` writes nothing and
reports what the run would do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, MetaData, Table, TextClause, func, inspect, select, text

from app.base import StateBase

from ..configs.records import KINDS, Record, doc_of, record_from_doc
from ..configs.store import ConfigConflictError, FileConfigStore, Write, validate_revision
from ..errors import HatError
from ..schema import ensure_state_schema
from .partitions import ensure_event_partitions, month_of

__all__ = [
    "LEGACY_TABLE_OF_KIND",
    "config_plan",
    "legacy_record_doc",
    "main",
    "rows_digest",
]

REFUSED = 2
# The hat_live table each config kind's rows were kept in, read in this order.
LEGACY_TABLE_OF_KIND: Mapping[str, str] = {
    "strategy": "hat_strategy",
    "strategy_version": "hat_strategy_version",
    "risk_policy_version": "hat_risk_policy_version",
    "broker_account": "hat_broker_account",
    "bot": "hat_bot",
    "regime_model_version": "hat_regime_model_version",
    "criterion_type": "hat_criterion_type",
}
_BATCH = 1000
_EPOCH = "1970-01-01T00:00:00+00:00"  # a placeholder instant: only the record is checked


def legacy_record_doc(cls: type[Record], row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The key and the record a legacy config row holds, as its revision file's `record` object
    (a uuid column such as `backtest_run_id` becomes its text). Raises `ConfigRecordError` when
    the row does not make a valid record of `cls`."""
    record = record_from_doc(cls, {name: _json_value(row.get(name)) for name in cls.FIELDS})
    doc = doc_of(record)
    envelope = {"kind": cls.KIND, "id": record.key, "rev": 1, "written_at": _EPOCH, "record": doc}
    validate_revision(cls.KIND, envelope)
    return record.key, doc


def _json_value(value: Any) -> Any:  # noqa: ANN401 - a column value of any type
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass
class KindPlan:
    """What the export does with one kind: the records to write (revision 1), those already
    exported with the same content, and those whose revision 1 differs."""

    writes: list[Write] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    differs: list[str] = field(default_factory=list)


def config_plan(
    kind: str, docs: Mapping[str, Mapping[str, Any]], exported: Mapping[str, Mapping[str, Any]]
) -> KindPlan:
    """Compare a kind's legacy records (`docs`, by key) with the revision 1 files already
    exported (`exported`, by key): a missing one is written, an equal one left, a different one
    refused."""
    plan = KindPlan()
    for key, doc in sorted(docs.items()):
        before = exported.get(key)
        if before is None:
            plan.writes.append(Write(kind, key, 0, dict(doc)))
        elif dict(before) == dict(doc):
            plan.same.append(key)
        else:
            plan.differs.append(key)
    return plan


def rows_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    """A digest of rows independent of their order: the sha256 of each row's canonical JSON,
    sorted, hashed again."""
    lines = sorted(
        hashlib.sha256(json.dumps(dict(r), sort_keys=True, default=_text).encode()).hexdigest()
        for r in rows
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _text(value: Any) -> str:  # noqa: ANN401 - json's fallback for any column type
    if isinstance(value, (datetime, time)):
        return value.isoformat()
    if isinstance(value, (date, Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(f"no JSON form for {type(value).__name__}")


class SplitError(HatError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the run (needs the databases) ------------------------------------------------------------


def _legacy_rows(live: Connection, table: str) -> list[dict[str, Any]] | None:
    if not inspect(live).has_table(table):
        return None
    reflected = Table(table, MetaData(), autoload_with=live)
    return [dict(r) for r in live.execute(select(reflected)).mappings()]


def export_configs(live: Connection, store: FileConfigStore, *, apply: bool) -> dict[str, Any]:
    """Export every legacy config kind (`LEGACY_TABLE_OF_KIND`) and verify it."""
    report: dict[str, Any] = {}
    for kind, table in LEGACY_TABLE_OF_KIND.items():
        cls = KINDS[kind]
        rows = _legacy_rows(live, table)
        if rows is None:
            report[kind] = {"table": table, "missing": True}
            continue
        docs = dict(legacy_record_doc(cls, row) for row in rows)
        _, pointer = store.pointer(kind)
        exported = {k: store.revision(kind, k, 1) for k in docs if k in pointer}
        plan = config_plan(kind, docs, exported)
        if plan.differs:
            raise SplitError(f"config_file_differs: {kind}/{plan.differs[0]}")
        if apply and plan.writes:
            store.commit(plan.writes)
        _, pointer = store.pointer(kind)
        stored = [store.revision(kind, k, 1) for k in docs if k in pointer]
        report[kind] = {
            "table": table,
            "rows": len(rows),
            "written": len(plan.writes) if apply else 0,
            "to_write": 0 if apply else len(plan.writes),
            "unchanged": len(plan.same),
            "files": len(stored),
            "digest_rows": rows_digest(docs.values()),
            "digest_files": rows_digest(stored),
        }
    return report


def copy_state(live: Connection, state: Connection | None, *, apply: bool) -> dict[str, Any]:
    """Copy each hat_state table's rows from hat_live (rows already there are kept) and verify
    the counts and digests."""
    report: dict[str, Any] = {}
    for table in StateBase.metadata.sorted_tables:
        rows = _legacy_rows(live, table.name)
        if rows is None:
            report[table.name] = {"missing": True}
            continue
        columns = [c.name for c in table.columns if not rows or c.name in rows[0]]
        source = [{c: r[c] for c in columns} for r in rows]
        entry: dict[str, Any] = {"rows": len(source), "digest_live": rows_digest(source)}
        if state is not None:
            if apply and source:
                if table.name == "hat_event":
                    for month in sorted({month_of(r["recorded_at"]) for r in source}):
                        ensure_event_partitions(state, month, ahead=0)
                for start in range(0, len(source), _BATCH):
                    state.execute(
                        _insert(state, table, columns),
                        {"rows": _rows_json(source[start : start + _BATCH])},
                    )
                _bump_sequences(state, table)
            copied = [
                dict(r) for r in state.execute(select(*[table.c[c] for c in columns])).mappings()
            ]
            keys = {_key(table, r) for r in source}
            entry["rows_state"] = len(copied)
            entry["digest_state"] = rows_digest(r for r in copied if _key(table, r) in keys)
        report[table.name] = entry
    return report


def _insert(state: Connection, table: Table, columns: Sequence[str]) -> TextClause:
    """Insert the rows of the JSON array `:rows` with their own keys, an identity column's too
    (`OVERRIDING SYSTEM VALUE`), keeping any row already there."""
    quote = state.dialect.identifier_preparer.quote
    # the table and column names are the models', quoted; the rows are a bound parameter
    names, name = ", ".join(quote(c) for c in columns), quote(table.name)
    sql = (
        f"INSERT INTO {name} ({names}) OVERRIDING SYSTEM VALUE SELECT {names} "  # noqa: S608
        f"FROM jsonb_populate_recordset(NULL::{name}, CAST(:rows AS jsonb)) ON CONFLICT DO NOTHING"
    )
    return text(sql)


def _rows_json(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps([dict(r) for r in rows], default=_text)


def _key(table: Table, row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[c.name] for c in table.primary_key.columns)


def _bump_sequences(state: Connection, table: Table) -> None:
    """Move each serial column's sequence past the copied ids, so the next insert does not
    collide with them."""
    for column in table.primary_key.columns:
        if column.type.python_type is int:
            sequence = state.execute(
                select(func.pg_get_serial_sequence(table.name, column.name))
            ).scalar_one()
            if sequence is not None:
                top = state.execute(select(func.max(column))).scalar_one()
                if top is not None:
                    state.execute(select(func.setval(sequence, top)))


def verified(configs: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    """Every exported kind and copied table matches its source in count and digest."""
    kinds_ok = all(
        r.get("missing") or (r["files"] == r["rows"] and r["digest_files"] == r["digest_rows"])
        for r in configs.values()
    )
    tables_ok = all(
        r.get("missing")
        or (
            "rows_state" in r
            and r["digest_state"] == r["digest_live"]
            and r["rows_state"] >= r["rows"]
        )
        for r in state.values()
    )
    return kinds_ok and tables_ok


def run(apply: bool) -> dict[str, Any]:  # pragma: no cover - needs the databases
    from app.database import (  # noqa: PLC0415 - importing it builds the engines
        engine,
        state_database_exists,
        state_engine,
    )

    from ..configs.store import config_root  # noqa: PLC0415
    from ..settings import operator_settings  # noqa: PLC0415

    settings = operator_settings()
    store = FileConfigStore(config_root(settings.data_root))
    exists = state_database_exists(create=apply)
    with engine.connect() as live:  # read-only: default_transaction_read_only
        configs = export_configs(live, store, apply=apply)
        if exists:
            if apply:
                StateBase.metadata.create_all(state_engine)
                ensure_state_schema(state_engine)
            with state_engine.begin() as state:
                tables = copy_state(live, state, apply=apply)
        else:
            tables = copy_state(live, None, apply=False)
    return {
        "applied": apply,
        "config_root": "HAT_DATA_ROOT/admin/config",
        "state_database": state_engine.url.database,
        "configs": configs,
        "state": tables,
        "verified": apply and verified(configs, tables),
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the databases
    parser = argparse.ArgumentParser(prog="python -m app.hat.ops.state_split")
    parser.add_argument("--check", action="store_true", help="write nothing; report the plan")
    args = parser.parse_args(argv)
    try:
        report = run(apply=not args.check)
    except (SplitError, ConfigConflictError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return REFUSED
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if args.check or report["verified"] else REFUSED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
