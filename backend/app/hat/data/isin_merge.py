"""One instrument per ISIN: the migration of an existing database and data root (audit D5, P6b).

Before P6b an instrument was (market, ISIN, series), so a stock that moved between EQ and BE (or
traded a block-deal window, BL) had one id per series and one broken history per id. This
module merges every ISIN's rows into one id and repoints everything that references them.

    python -m app.hat.data.isin_merge check
    python -m app.hat.data.isin_merge up
    python -m app.hat.data.isin_merge down
    python -m app.hat.data.isin_merge rekey --root OLD_ROOT --into NEW_ROOT

`up`, in one transaction under an advisory lock:
- each ISIN with more than one row keeps one id (`identity.kept_id`: the active row, else the
  best-ranked series, else the lowest id); every member's row is saved in `hat_isin_merge`
  (old id, kept id, the row as JSON), which is the mapping the Parquet re-key reads;
- every row of a table with a foreign key to `hat_instrument` is repointed to the kept id. A
  repoint that would break a unique key (the same corporate action stored under two siblings,
  say) drops the sibling's row in the reference tables (`DROPPABLE`) and aborts the migration
  anywhere else (a signal, a position: never dropped). Each change is saved in
  `hat_isin_merge_row` (table, action, old id, the row as JSON);
- the symbol histories of the siblings become one (`identity.merge_symbol_history`);
- the kept row learns its `previous_series` and `series_since` from a sibling the listing left,
  the other rows are deleted, and the unique key becomes (market_id, isin).
Run again, it finds no ISIN with two rows and changes nothing.

`down` undoes `up` from the two audit tables (siblings restored, rows put back, the old unique
key) and drops them; run again, it finds no audit table and changes nothing. It is meant for
right after `up`: a row changed since (a broker symbol refreshed, say) is matched by its primary
key, so the restore still finds it.

`rekey` writes a new data root from an old one with the instrument ids of `bars_daily_raw`,
`bars_5m` (with its coverage files) and the reject ledger mapped old -> kept; `bars_daily_raw`
then keeps one row per (instrument, day), the best-ranked series' (`identity.series_rank`: BL and
IL trade beside EQ or BE on 4,548 ISIN-days). Every other file is hard-linked (Parquet parts and
the raw downloads, never rewritten in place) or copied. The old root stays as it was, which is the
way back. Run `python -m app.hat.data.features --full` on the new root afterwards: the features'
inputs digest changes with the corporate actions, so the next run rebuilds anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import IntegrityError

from ..errors import HatError
from ..settings import operator_settings
from .bars_store import COVERAGE_NAME
from .bars_store import DATASET as DATASET_5M
from .bars_store import REJECTS as DATASET_REJECTS
from .identity import Interval, Sibling, kept_id, merge_symbol_history, series_rank
from .store import DATASET_BARS, RAW_DIR

__all__ = [
    "DROPPABLE",
    "MergeError",
    "MergeReport",
    "RekeyReport",
    "check",
    "down",
    "rekey",
    "up",
]

_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('hat_isin_merge'))"
HISTORY = "hat_instrument_symbol_history"
# Reference tables whose sibling row may be dropped when the kept id already holds the same key.
DROPPABLE = frozenset(
    {
        "hat_broker_symbol",
        "hat_corporate_action",
        "hat_fundamental",
        "hat_industry",
        "hat_price_band",
        "hat_universe_member",
    }
)
OLD_UNIQUE = "hat_instrument_market_id_isin_series_key"
NEW_UNIQUE = "hat_instrument_market_id_isin_key"
REPOINTED, DROPPED, INSERTED = "repointed", "dropped", "inserted"

_AUDIT_DDL = (
    """CREATE TABLE IF NOT EXISTS hat_isin_merge (
        old_id uuid PRIMARY KEY,
        kept_id uuid NOT NULL,
        isin text NOT NULL,
        series text NOT NULL,
        instrument jsonb NOT NULL,
        merged_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS hat_isin_merge_row (
        id bigserial PRIMARY KEY,
        table_name text NOT NULL,
        action text NOT NULL CHECK (action IN ('repointed', 'dropped', 'inserted')),
        old_instrument_id uuid NOT NULL,
        row jsonb NOT NULL)""",
    "ALTER TABLE hat_instrument ADD COLUMN IF NOT EXISTS series_since date",
    "ALTER TABLE hat_instrument ADD COLUMN IF NOT EXISTS previous_series text",
)


class MergeError(HatError):
    """The migration cannot go on; `code` is stable and the transaction is rolled back."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} {detail}".strip())
        self.code = code


@dataclass
class MergeReport:
    isins: int = 0
    removed: int = 0
    changes: dict[str, dict[str, int]] = field(default_factory=dict)
    unique_key: str = ""

    def count(self, table: str, action: str) -> None:
        per = self.changes.setdefault(table, {})
        per[action] = per.get(action, 0) + 1


# --- the database -------------------------------------------------------------------------------


def _constraints(conn: Connection) -> set[str]:
    sql = "SELECT conname FROM pg_constraint WHERE conrelid = 'hat_instrument'::regclass"
    return {r[0] for r in conn.execute(text(sql))}


def _audit_exists(conn: Connection) -> bool:
    return conn.execute(text("SELECT to_regclass('hat_isin_merge') IS NOT NULL")).scalar_one()


def _groups(conn: Connection) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every (market, ISIN) with more than one row: its rows as JSON."""
    rows = conn.execute(
        text(
            """SELECT to_jsonb(i) FROM hat_instrument i
            WHERE (market_id, isin) IN (
                SELECT market_id, isin FROM hat_instrument GROUP BY 1, 2 HAVING count(*) > 1)
            ORDER BY market_id, isin, id"""
        )
    ).scalars()
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["market_id"], row["isin"]), []).append(row)
    return groups


def _ordered(members: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The members, the kept one first, the rest in the order `kept_id` ranks them."""
    sib = {m["id"]: Sibling(uuid.UUID(m["id"]), m["series"], m["active"]) for m in members}
    first = str(kept_id(list(sib.values())))
    rest = sorted(
        (m for m in members if m["id"] != first),
        key=lambda m: (not m["active"], series_rank(m["series"]), uuid.UUID(m["id"]).bytes),
    )
    return [next(m for m in members if m["id"] == first), *rest]


def _referencing(conn: Connection) -> list[tuple[str, str]]:
    """(table, column) of every foreign key to `hat_instrument`, the symbol history excepted."""
    rows = conn.execute(
        text(
            """SELECT c.conrelid::regclass::text, a.attname FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
            WHERE c.contype = 'f' AND c.confrelid = 'hat_instrument'::regclass
            ORDER BY 1, 2"""
        )
    ).all()
    return [(t, col) for t, col in rows if t != HISTORY]


def _primary_key(conn: Connection, table: str) -> list[str]:
    cols = [
        r[0]
        for r in conn.execute(
            text(
                """SELECT a.attname FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
                WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary ORDER BY a.attnum"""
            ),
            {"t": table},
        )
    ]
    if not cols:
        raise MergeError("isin_merge_no_primary_key", table)
    return cols


_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def _name(value: str) -> str:
    """`value` when it is a plain lower-case SQL name (tables and columns come from the catalog,
    never from input; this keeps it so)."""
    if not _NAME.match(value):
        raise MergeError("isin_merge_bad_name", value)
    return value


def _sql(template: str, *, where: str = "", **names: str) -> TextClause:
    """`template` with the checked names filled in, then the `_match` clause `where`."""
    filled = template.format(**{k: _name(v) for k, v in names.items()})
    return text(filled + where)


def _match(table: str, pk: Sequence[str]) -> str:
    """A WHERE clause matching the row whose primary key the JSON parameter `:row` holds."""
    for name in (table, *pk):
        _name(name)
    cols = ", ".join(f'"{c}"' for c in pk)
    record = f"jsonb_populate_record(NULL::{table}, CAST(:row AS jsonb))"
    return f"({cols}) = (SELECT {cols} FROM {record})"  # noqa: S608 - names checked above


def _save(conn: Connection, table: str, action: str, old_id: str, row: Mapping[str, Any]) -> None:
    conn.execute(
        text(
            "INSERT INTO hat_isin_merge_row (table_name, action, old_instrument_id, row) "
            "VALUES (:t, :a, :o, CAST(:r AS jsonb))"
        ),
        {"t": table, "a": action, "o": old_id, "r": json.dumps(row)},
    )


def _repoint(
    conn: Connection,
    table: str,
    column: str,
    mapping: Mapping[str, str],
    order: Mapping[str, int],
    report: MergeReport,
) -> None:
    pk = _primary_key(conn, table)
    rows = conn.execute(
        _sql(
            'SELECT to_jsonb(t) FROM {t} t WHERE "{c}" = ANY (CAST(:ids AS uuid[]))',
            t=table,
            c=column,
        ),
        {"ids": list(mapping)},
    ).scalars()
    where = _match(table, pk)
    for row in sorted(rows, key=lambda r: (order[r[column]], json.dumps(r, sort_keys=True))):
        old = row[column]
        params = {"row": json.dumps(row), "kept": mapping[old]}
        try:
            with conn.begin_nested():
                conn.execute(
                    _sql('UPDATE {t} SET "{c}" = :kept WHERE ', t=table, c=column, where=where),
                    params,
                )
        except IntegrityError as exc:
            if table not in DROPPABLE:
                raise MergeError("isin_merge_conflict", table) from exc
            conn.execute(_sql("DELETE FROM {t} WHERE ", t=table, where=where), params)
            _save(conn, table, DROPPED, old, row)
            report.count(table, DROPPED)
            continue
        _save(conn, table, REPOINTED, old, row)
        report.count(table, REPOINTED)


def _merge_history(
    conn: Connection, members: Sequence[dict[str, Any]], report: MergeReport
) -> None:
    ids = [m["id"] for m in members]
    rows = conn.execute(
        text(
            "SELECT to_jsonb(h) FROM hat_instrument_symbol_history h "
            "WHERE instrument_id = ANY (CAST(:ids AS uuid[])) ORDER BY valid_from"
        ),
        {"ids": ids},
    ).scalars()
    per: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in rows:
        per[row["instrument_id"]].append(row)
    merged = merge_symbol_history(
        [
            [
                Interval(
                    r["symbol"],
                    date.fromisoformat(r["valid_from"]),
                    None if r["valid_to"] is None else date.fromisoformat(r["valid_to"]),
                )
                for r in per[i]
            ]
            for i in ids
        ]
    )
    for instrument_id, history in per.items():
        for row in history:
            _save(conn, HISTORY, DROPPED, instrument_id, row)
            report.count(HISTORY, DROPPED)
    conn.execute(
        text(
            "DELETE FROM hat_instrument_symbol_history "
            "WHERE instrument_id = ANY (CAST(:ids AS uuid[]))"
        ),
        {"ids": ids},
    )
    kept = ids[0]
    for iv in merged:
        row = {
            "instrument_id": kept,
            "symbol": iv.symbol,
            "valid_from": iv.valid_from.isoformat(),
            "valid_to": None if iv.valid_to is None else iv.valid_to.isoformat(),
        }
        conn.execute(
            text(
                "INSERT INTO hat_instrument_symbol_history "
                "(instrument_id, symbol, valid_from, valid_to) "
                "VALUES (:instrument_id, :symbol, :valid_from, :valid_to)"
            ),
            row,
        )
        _save(conn, HISTORY, INSERTED, kept, row)
        report.count(HISTORY, INSERTED)


def _series_change(members: Sequence[dict[str, Any]]) -> tuple[str, str] | None:
    """(previous series, since) for an active kept row: the series of the sibling the listing
    left last under another series, and the day it left. None when there is none."""
    kept = members[0]
    if not kept["active"]:
        return None
    left = [
        m for m in members[1:] if m["series"] != kept["series"] and m["delisted_on"] is not None
    ]
    if not left:
        return None
    last = max(left, key=lambda m: m["delisted_on"])
    return last["series"], last["delisted_on"]


def up(conn: Connection) -> MergeReport:
    """Merge every ISIN's rows into one id (see the module docstring). Commits nothing itself:
    the caller's transaction holds it all."""
    conn.execute(text(_LOCK_SQL))
    for ddl in _AUDIT_DDL:
        conn.execute(text(ddl))
    report = MergeReport()
    ordered = [_ordered(members) for members in _groups(conn).values()]
    mapping, order = _record_members(conn, ordered)
    report.isins = len(ordered)
    if mapping:
        for table, column in _referencing(conn):
            _repoint(conn, table, column, mapping, order, report)
    for members in ordered:
        _merge_history(conn, members, report)
        change = _series_change(members)
        if change is not None:
            conn.execute(
                text(
                    "UPDATE hat_instrument SET previous_series = :p, series_since = :d "
                    "WHERE id = :k AND series_since IS NULL"
                ),
                {"p": change[0], "d": change[1], "k": members[0]["id"]},
            )
    if mapping:
        conn.execute(
            text("DELETE FROM hat_instrument WHERE id = ANY (CAST(:ids AS uuid[]))"),
            {"ids": list(mapping)},
        )
    report.removed = len(mapping)
    _swap_unique(conn)
    report.unique_key = NEW_UNIQUE
    return report


def _record_members(
    conn: Connection, ordered: Sequence[Sequence[dict[str, Any]]]
) -> tuple[dict[str, str], dict[str, int]]:
    """Save every group member in `hat_isin_merge`; (old id -> kept id of the merged ones, each
    member's rank in its group)."""
    mapping: dict[str, str] = {}
    order: dict[str, int] = {}
    for members in ordered:
        kept = members[0]["id"]
        for rank, m in enumerate(members):
            order[m["id"]] = rank
            conn.execute(
                text(
                    "INSERT INTO hat_isin_merge (old_id, kept_id, isin, series, instrument) "
                    "VALUES (:o, :k, :i, :s, CAST(:r AS jsonb))"
                ),
                {"o": m["id"], "k": kept, "i": m["isin"], "s": m["series"], "r": json.dumps(m)},
            )
            if m["id"] != kept:
                mapping[m["id"]] = kept
    return mapping, order


def _swap_unique(conn: Connection) -> None:
    """The unique key (market_id, isin) in place of (market_id, isin, series)."""
    names = _constraints(conn)
    if OLD_UNIQUE in names:
        conn.execute(text("ALTER TABLE hat_instrument DROP CONSTRAINT " + OLD_UNIQUE))
    if NEW_UNIQUE not in names:
        conn.execute(
            text(
                "ALTER TABLE hat_instrument ADD CONSTRAINT "
                + NEW_UNIQUE
                + " UNIQUE (market_id, isin)"
            )
        )


def down(conn: Connection) -> MergeReport:
    """Undo `up` from its audit tables, then drop them. Commits nothing itself."""
    conn.execute(text(_LOCK_SQL))
    report = MergeReport()
    if not _audit_exists(conn):
        report.unique_key = ", ".join(sorted(_constraints(conn) & {OLD_UNIQUE, NEW_UNIQUE}))
        return report
    names = _constraints(conn)
    if NEW_UNIQUE in names:
        conn.execute(text("ALTER TABLE hat_instrument DROP CONSTRAINT " + NEW_UNIQUE))
    changes = conn.execute(
        text(
            "SELECT table_name, action, CAST(old_instrument_id AS text), row "
            "FROM hat_isin_merge_row ORDER BY id"
        )
    ).all()
    keys: dict[str, list[str]] = {}

    def where(table: str) -> str:
        if table not in keys:
            keys[table] = _primary_key(conn, table)
        return _match(table, keys[table])

    for table, action, _old, row in changes:
        if action == INSERTED:
            conn.execute(
                _sql("DELETE FROM {t} WHERE ", t=table, where=where(table)),
                {"row": json.dumps(row)},
            )
            report.count(table, action)
    kept_of = _restore_instruments(conn, report)
    _restore_rows(conn, changes, kept_of, where, report)
    conn.execute(
        text(
            "ALTER TABLE hat_instrument ADD CONSTRAINT " + OLD_UNIQUE + " "
            "UNIQUE (market_id, isin, series)"
        )
    )
    conn.execute(text("DROP TABLE hat_isin_merge_row"))
    conn.execute(text("DROP TABLE hat_isin_merge"))
    report.isins = len(set(kept_of.values()))
    report.unique_key = OLD_UNIQUE
    return report


def _restore_instruments(conn: Connection, report: MergeReport) -> dict[str, str]:
    """Put the merged siblings back and the kept rows' series columns as they were; old id ->
    kept id of every group member."""
    members = conn.execute(
        text("SELECT CAST(old_id AS text), CAST(kept_id AS text), instrument FROM hat_isin_merge")
    ).all()
    kept_of: dict[str, str] = {}
    for old, kept, instrument in members:
        kept_of[old] = kept
        if old == kept:
            conn.execute(
                text(
                    "UPDATE hat_instrument SET previous_series = :p, series_since = :d "
                    "WHERE id = :k"
                ),
                {
                    "p": instrument.get("previous_series"),
                    "d": instrument.get("series_since"),
                    "k": kept,
                },
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO hat_instrument SELECT * FROM "
                    "jsonb_populate_record(NULL::hat_instrument, CAST(:row AS jsonb))"
                ),
                {"row": json.dumps(instrument)},
            )
            report.removed += 1
    return kept_of


def _restore_rows(
    conn: Connection,
    changes: Sequence[Any],
    kept_of: Mapping[str, str],
    where: Callable[[str], str],
    report: MergeReport,
) -> None:
    """Insert the dropped rows again and point the repointed ones back at their old id."""
    columns = dict(_referencing(conn))
    for table, action, old, row in changes:
        if action == DROPPED:
            conn.execute(
                _sql(
                    "INSERT INTO {t} SELECT * FROM "
                    "jsonb_populate_record(NULL::{t}, CAST(:row AS jsonb))",
                    t=table,
                ),
                {"row": json.dumps(row)},
            )
        elif action == REPOINTED:
            column = columns[table]
            now = {**row, column: kept_of[old]}
            conn.execute(
                _sql('UPDATE {t} SET "{c}" = :old WHERE ', t=table, c=column, where=where(table)),
                {"row": json.dumps(now), "old": old},
            )
        else:
            continue
        report.count(table, action)


def check(conn: Connection) -> dict[str, Any]:
    """What a merge would find: instruments, ISINs, ISINs with more than one id, the unique keys
    in force and whether the audit tables exist."""
    instruments, isins = conn.execute(
        text("SELECT count(*), count(DISTINCT (market_id, isin)) FROM hat_instrument")
    ).one()
    split = conn.execute(
        text(
            "SELECT count(*) FROM (SELECT 1 FROM hat_instrument "
            "GROUP BY market_id, isin HAVING count(*) > 1) t"
        )
    ).scalar_one()
    return {
        "instruments": instruments,
        "isins": isins,
        "isins_with_two_ids": split,
        "unique_keys": sorted(_constraints(conn) & {OLD_UNIQUE, NEW_UNIQUE}),
        "audit_tables": _audit_exists(conn),
    }


def mapping_from_db(conn: Connection) -> dict[str, str]:
    """old id -> kept id of every merged sibling, from `hat_isin_merge`."""
    if not _audit_exists(conn):
        raise MergeError("isin_merge_not_run")
    sql = (
        "SELECT CAST(old_id AS text), CAST(kept_id AS text) FROM hat_isin_merge "
        "WHERE old_id <> kept_id"
    )
    return {str(old): str(kept) for old, kept in conn.execute(text(sql)).all()}


# --- the data root ------------------------------------------------------------------------------


@dataclass
class RekeyReport:
    files: dict[str, int] = field(default_factory=dict)
    rows_mapped: dict[str, int] = field(default_factory=dict)
    rows_dropped: int = 0  # bars_daily_raw rows of a worse-ranked series on the same day
    linked: int = 0
    copied: int = 0


def keep_rows(ids: Sequence[str], series: Sequence[str]) -> list[int]:
    """The indices of the rows to keep, in their order: one per instrument id, the best-ranked
    series' (the first on a tie)."""
    best: dict[str, int] = {}
    for index, (instrument_id, s) in enumerate(zip(ids, series, strict=True)):
        held = best.get(instrument_id)
        if held is None or series_rank(s) < series_rank(series[held]):
            best[instrument_id] = index
    return sorted(best.values())


def _mapped(table: pa.Table, mapping: Mapping[str, str]) -> tuple[pa.Table, int]:
    ids = table.column("instrument_id").to_pylist()
    new = [mapping.get(i, i) if i is not None else None for i in ids]
    changed = sum(1 for a, b in zip(ids, new, strict=True) if a != b)
    index = table.schema.get_field_index("instrument_id")
    field_ = table.schema.field(index)
    return table.set_column(index, field_, pa.array(new, field_.type)), changed


def _write_parquet(table: pa.Table, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    pq.write_table(table, temp)
    os.replace(temp, target)


def _rekey_parquet(
    source: Path, target: Path, dataset: str, mapping: Mapping[str, str], report: RekeyReport
) -> None:
    table = pq.read_table(source, partitioning=None)
    table, changed = _mapped(table, mapping)
    if dataset == DATASET_BARS and changed:
        keep = keep_rows(
            table.column("instrument_id").to_pylist(), table.column("series").to_pylist()
        )
        report.rows_dropped += table.num_rows - len(keep)
        table = table.take(pa.array(keep, pa.int64()))
    elif dataset == DATASET_5M and changed:
        keys = list(
            zip(
                table.column("instrument_id").to_pylist(),
                table.column("bar_start").to_pylist(),
                strict=True,
            )
        )
        if len(set(keys)) != len(keys):
            raise MergeError("isin_merge_5m_collision", str(source))
    report.rows_mapped[dataset] = report.rows_mapped.get(dataset, 0) + changed
    report.files[dataset] = report.files.get(dataset, 0) + 1
    _write_parquet(table, target)


def coverage_ids(from_open: Iterable[object], mapping: Mapping[str, str]) -> list[str]:
    """The coverage file's instrument ids mapped old -> kept, each checked to be a uuid."""
    ids = (str(uuid.UUID(str(i))) for i in from_open)
    return sorted({mapping.get(i, i) for i in ids})


def _rekey_coverage(source: Path, target: Path, mapping: Mapping[str, str]) -> None:
    raw = json.loads(source.read_text(encoding="utf-8"))
    body = {
        "from_open": coverage_ids(raw.get("from_open", ()), mapping),
        "gap_from": raw.get("gap_from"),
        "seen_until": raw.get("seen_until"),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(body), encoding="utf-8")
    os.replace(temp, target)


def _link_or_copy(source: Path, target: Path, immutable: bool, report: RekeyReport) -> None:
    """Hard-link a file no writer changes in place (a Parquet part, a raw download), else copy
    it (a log or a progress file could be appended to through the link)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    if immutable:
        try:
            os.link(source, target)
            report.linked += 1
            return
        except OSError:
            pass
    shutil.copy2(source, target)
    report.copied += 1


REKEYED = (DATASET_BARS, DATASET_5M, DATASET_REJECTS)


def rekey(root: Path, into: Path, mapping: Mapping[str, str]) -> RekeyReport:
    """Write `into`, a new or empty directory, from `root` with the instrument ids mapped (see
    the module docstring)."""
    root, into = root.resolve(), into.resolve()
    if into == root or root in into.parents or into in root.parents:
        raise MergeError("isin_merge_same_root")
    if into.exists() and any(into.iterdir()):
        # a part left by an earlier or partial run would be read with the new ones
        raise MergeError("isin_merge_into_not_empty")
    report = RekeyReport()
    for source in sorted(p for p in root.rglob("*") if p.is_file()):
        if not source.name.endswith((".tmp", ".lock")):
            _rekey_file(source, source.relative_to(root), into, mapping, report)
    return report


def _rekey_file(
    source: Path, rel: Path, into: Path, mapping: Mapping[str, str], report: RekeyReport
) -> None:
    """One file of the old root into the new one: re-keyed, linked or copied."""
    target = into / rel
    dataset = rel.parts[0] if len(rel.parts) > 1 else ""
    if dataset in REKEYED and source.suffix == ".parquet":
        _rekey_parquet(source, target, dataset, mapping, report)
    elif dataset == DATASET_5M and source.name == COVERAGE_NAME:
        _rekey_coverage(source, target, mapping)
    else:
        immutable = dataset == RAW_DIR or source.suffix == ".parquet"
        _link_or_copy(source, target, immutable, report)


# --- the command line ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.isin_merge", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="count what a merge would find; writes nothing")
    sub.add_parser("up", help="merge every ISIN's rows into one id")
    sub.add_parser("down", help="undo `up` from its audit tables")
    r = sub.add_parser("rekey", help="write a new data root with the merged ids")
    r.add_argument("--root", type=Path, required=True, help="the data root to read")
    r.add_argument("--into", type=Path, required=True, help="the new data root to write")
    return p.parse_args(argv)


def _print(values: Iterable[tuple[str, Any]]) -> None:
    for key, value in values:
        sys.stdout.write(f"{key}: {value}\n")


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    args = _parse_args(argv)
    operator_settings()  # loads `.env` (the DB_* variables) before the engine is built
    from app.database import engine

    try:
        if args.command == "check":
            with engine.connect() as conn:
                _print(check(conn).items())
        elif args.command in ("up", "down"):
            with engine.begin() as conn:
                report = up(conn) if args.command == "up" else down(conn)
                _print(vars(report).items())
                _print(check(conn).items())
        else:
            with engine.connect() as conn:
                mapping = mapping_from_db(conn)
            _print(vars(rekey(args.root, args.into, mapping)).items())
    except MergeError as exc:
        sys.stderr.write(f"{exc.code}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
