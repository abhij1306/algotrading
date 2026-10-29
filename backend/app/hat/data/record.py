"""The data record: manifests of the stores and CSV exports of the reference tables, written into a
local clone of the private data repo and pushed when anything changed (docs/runbook.md, Data
record). The daily update runs it as its last step; by hand:

    python -m app.hat.data.record [--no-push]

A manifest (`manifests/<store>.csv`) lists each file of a store under the data root: its path,
rows and date range (Parquet metadata; a `date=` partition gives the date) and its size and
sha256. Committed after every update, it makes any rewrite of past data a git diff; the commit
message counts the added, rewritten and removed files per store. The reference exports
(`reference/<table>.csv`) are read through the read-only engine, in a stable order, so an
unchanged table gives an unchanged file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
import subprocess  # noqa: S404 - git with fixed arguments, never a shell
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

STORES = (
    "raw",
    "bars_daily_raw",
    "bars_5m",
    "features_daily",
    "index_daily",
    "breadth_daily",
    "etf",
)
MANIFEST_FIELDS = ("path", "rows", "min_date", "max_date", "bytes", "sha256")
MANIFESTS = Path("manifests")
REFERENCE = Path("reference")
HOOKS_PATH = ".githooks"  # the data repo's pre-push secret guard
PARTITION = re.compile(r"(?:^|/)date=(\d{4}-\d{2}-\d{2})(?:/|$)")
CHUNK = 1 << 20

# Each reference export: file name -> query (ordered on a key, so the CSV is stable).
EXPORTS: dict[str, str] = {
    "index_membership": (
        "select t.index_name, i.isin, t.symbol, t.effective_from, t.effective_to, t.source_notice "
        "from hat_index_member t join hat_instrument i on i.id = t.instrument_id "
        "order by t.index_name, t.effective_from, i.isin, t.symbol"
    ),
    "index_membership_loads": (
        "select index_name, source, digest, covered_from, covered_to, rows, loaded_at "
        "from hat_index_membership order by index_name, covered_from"
    ),
    "corporate_actions": (
        "select i.isin, i.symbol, t.ex_date, t.action_type, t.adjustment_factor, t.cash_amount, "
        "t.demerger, t.announced_on, t.known_on, t.known_basis, t.source, t.raw_text "
        "from hat_corporate_action t join hat_instrument i on i.id = t.instrument_id "
        "order by t.ex_date, i.isin, t.action_type, t.source, t.raw_text"
    ),
    "corporate_action_versions": (
        "select i.isin, i.symbol, t.ex_date, t.action_type, t.seq, t.status, t.factor, t.cash, "
        "t.band_low, t.band_high, t.basis, t.source, t.source_published_on, t.available_on, "
        "t.first_observed_at, t.text "
        "from hat_corporate_action_version t join hat_instrument i on i.id = t.instrument_id "
        "order by t.ex_date, i.isin, t.action_type, t.seq"
    ),
    "demerger_notices": (
        "select i.isin, i.symbol, t.ex_date, t.announced_on, t.source, t.recorded_at, t.raw_text "
        "from hat_demerger_notice t join hat_instrument i on i.id = t.instrument_id "
        "order by t.ex_date, i.isin, t.source"
    ),
    "results_filings": (
        "select t.symbol, t.isin, t.period_start, t.period_end, t.basis, t.format, t.source, "
        "t.version, t.listing, t.listing_seq, t.public_at, t.public_at_source, "
        "t.public_at_estimated, t.xbrl_at, t.xbrl_at_estimated, t.q4_derived, "
        "t.period_mismatch, t.quality_flags, t.source_url, t.sha256, t.recorded_at "
        "from hat_financial_result t "
        "order by t.isin, t.period_end, t.basis, t.source, t.version, t.listing, t.listing_seq"
    ),
    "fundamental_filings": (
        "select t.symbol, t.isin, t.period_end, t.basis, t.family, t.source, t.format, t.version, "
        "t.public_at, t.public_at_source, t.public_at_estimated, t.point_in_time, t.facts, "
        "t.source_url, t.sha256, t.fetched_at "
        "from hat_fundamental_filing t "
        "order by t.isin, t.period_end, t.basis, t.family, t.source, t.version, t.sha256"
    ),
    "trading_calendar": (
        "select date, is_trading, open_at, close_at, note from hat_trading_calendar order by date"
    ),
    "instruments": (
        "select isin, series, symbol, name, face_value, listed_on, delisted_on, active, "
        "series_since, previous_series from hat_instrument order by isin, series, symbol"
    ),
    "symbol_history": (
        "select i.isin, i.series, t.symbol, t.valid_from, t.valid_to "
        "from hat_instrument_symbol_history t join hat_instrument i on i.id = t.instrument_id "
        "order by i.isin, i.series, t.valid_from, t.symbol"
    ),
    "isin_lineage": (
        "select o.isin as old_isin, o.symbol as old_symbol, n.isin as new_isin, "
        "n.symbol as new_symbol, t.effective_date, t.status, t.reason, t.known_on, t.decided_at, "
        "t.evidence::text as evidence from hat_isin_lineage t "
        "join hat_instrument o on o.id = t.old_instrument_id "
        "join hat_instrument n on n.id = t.new_instrument_id "
        "order by t.effective_date, o.isin, n.isin"
    ),
    "isin_merges": (
        "select t.isin, t.series, k.isin as kept_isin, k.symbol as kept_symbol, t.merged_at "
        "from hat_isin_merge t join hat_instrument k on k.id = t.kept_id "
        "order by t.isin, t.series, t.merged_at"
    ),
    "lineage_overrides": (
        "select o.isin as old_isin, o.symbol as old_symbol, n.isin as new_isin, "
        "n.symbol as new_symbol, t.verdict, t.note, t.decided_at from hat_lineage_override t "
        "join hat_instrument o on o.id = t.old_instrument_id "
        "join hat_instrument n on n.id = t.new_instrument_id "
        "order by o.isin, n.isin, t.decided_at"
    ),
}


class RecordError(Exception):
    pass


# --- manifests (pure) -------------------------------------------------------------------------


def partition_date(path: str) -> date | None:
    """The `date=YYYY-MM-DD` partition of a store path, if it has one."""
    match = PARTITION.search(path)
    return date.fromisoformat(match.group(1)) if match else None


def manifest_row(  # noqa: PLR0913
    path: str,
    rows: int | None,
    min_date: date | None,
    max_date: date | None,
    size: int,
    sha256: str,
) -> dict[str, str]:
    """One manifest line; a partition path's date stands in for a file without date columns."""
    day = partition_date(path)
    low, high = min_date or day, max_date or day
    return {
        "path": path,
        "rows": "" if rows is None else str(rows),
        "min_date": low.isoformat() if low else "",
        "max_date": high.isoformat() if high else "",
        "bytes": str(size),
        "sha256": sha256,
    }


def manifest_text(rows: Iterable[Mapping[str, str]]) -> str:
    """The manifest CSV, sorted by path."""
    out = io.StringIO()
    writer = csv.DictWriter(out, MANIFEST_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda r: r["path"]))
    return out.getvalue()


def parse_manifest(text: str) -> dict[str, str]:
    """A manifest CSV's sha256 by path (empty for no text)."""
    return {row["path"]: row["sha256"] for row in csv.DictReader(io.StringIO(text))}


@dataclass(frozen=True)
class Changes:
    added: int
    rewritten: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def any(self) -> bool:
        return bool(self.added or self.rewritten or self.removed)


def compare(old: Mapping[str, str], new: Mapping[str, str]) -> Changes:
    """What a new manifest changes against the committed one (sha256 by path): files added,
    files whose bytes changed (a rewrite of stored data) and files gone."""
    rewritten = tuple(sorted(p for p, sha in new.items() if p in old and old[p] != sha))
    return Changes(len(set(new) - set(old)), rewritten, tuple(sorted(set(old) - set(new))))


def summary(changes: Mapping[str, Changes]) -> str:
    """One line of the stores that changed: `bars_5m +1 ~0 -0`; `no change` when none did."""
    parts = [
        f"{store} +{c.added} ~{len(c.rewritten)} -{len(c.removed)}"
        for store, c in sorted(changes.items())
        if c.any
    ]
    return ", ".join(parts) if parts else "no change"


def commit_message(stamp: str, changes: Mapping[str, Changes]) -> str:
    """The record commit's message: the summary line, then every rewritten and removed file."""
    lines = [f"record {stamp}: {summary(changes)}"]
    for _, c in sorted(changes.items()):
        lines += [f"rewritten {path}" for path in c.rewritten]
        lines += [f"removed {path}" for path in c.removed]
    return "\n\n".join([lines[0], "\n".join(lines[1:])]) if len(lines) > 1 else lines[0]


# --- IO -------------------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parquet_facts(path: Path) -> tuple[int, date | None, date | None]:
    """Rows, and the range of the first date or timestamp column from the row-group stats."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    meta = pq.ParquetFile(path).metadata
    schema = meta.schema.to_arrow_schema()
    column = next(
        (
            i
            for i, f in enumerate(schema)
            if pa.types.is_date(f.type) or pa.types.is_timestamp(f.type)
        ),
        None,
    )
    lows, highs = [], []
    for group in range(meta.num_row_groups) if column is not None else ():
        stats = meta.row_group(group).column(column).statistics
        if stats is not None and stats.has_min_max:
            lows.append(stats.min)
            highs.append(stats.max)
    return meta.num_rows, _as_date(min(lows, default=None)), _as_date(max(highs, default=None))


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return (value.astimezone(UTC) if value.tzinfo else value).date()
    return value if isinstance(value, date) else None


def store_rows(root: Path, store: str) -> list[dict[str, str]]:
    """The manifest lines of one store under the data root."""
    rows = []
    base = root / store
    for path in sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        facts = _parquet_facts(path) if path.suffix == ".parquet" else (None, None, None)
        rows.append(manifest_row(rel, *facts, path.stat().st_size, _sha256(path)))
    return rows


def write_manifests(root: Path, clone: Path) -> dict[str, Changes]:
    changes = {}
    for store in STORES:
        if not (root / store).is_dir():
            continue
        target = clone / MANIFESTS / f"{store}.csv"
        old = parse_manifest(target.read_text()) if target.exists() else {}
        text = manifest_text(store_rows(root, store))
        changes[store] = compare(old, parse_manifest(text))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return changes


def write_reference(clone: Path) -> int:
    """Each reference table as CSV through the read-only engine (UTC timestamps)."""
    from app.database import engine

    folder = clone / REFERENCE
    folder.mkdir(parents=True, exist_ok=True)
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("set transaction read only")
        cursor.execute("set local timezone = 'UTC'")
        for name, query in EXPORTS.items():
            with (folder / f"{name}.csv").open("w", encoding="utf-8", newline="") as out:
                cursor.copy_expert(f"copy ({query}) to stdout with csv header", out)
        raw.rollback()
    finally:
        raw.close()
    return len(EXPORTS)


def git(clone: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), *args],  # noqa: S607 - the operator's git on PATH
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RecordError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
    return result.stdout


def commit(clone: Path, message: str, *, push: bool) -> bool:
    """Commit the manifests and reference exports when they changed, then push (the clone's
    secret guard runs on the push); whether there was a change."""
    if git(clone, "config", "core.hooksPath").strip() != HOOKS_PATH:
        raise RecordError(
            f"the data repo's secret guard is off: git config core.hooksPath {HOOKS_PATH}"
        )
    git(clone, "add", "--", str(MANIFESTS), str(REFERENCE))
    if not git(clone, "status", "--porcelain", "--", str(MANIFESTS), str(REFERENCE)).strip():
        return False
    git(clone, "commit", "-q", "-m", message, "--", str(MANIFESTS), str(REFERENCE))
    if push:
        git(clone, "pull", "-q", "--rebase", "--autostash", "origin", "main")
        git(clone, "push", "-q", "origin", "HEAD:main")
    return True


def refresh(root: Path, clone: Path, *, push: bool = True) -> str:
    """Rewrite the manifests and reference exports in `clone`, commit and push; a one-line
    outcome. RecordError when the clone is missing or git fails."""
    if not (clone / ".git").exists():
        raise RecordError(f"no data repo clone at {clone} (HAT_DATA_REPO)")
    changes = write_manifests(root, clone)
    tables = write_reference(clone)
    stamp = datetime.now(UTC).date().isoformat()
    committed = commit(clone, commit_message(stamp, changes), push=push)
    rewritten = [p for c in changes.values() for p in c.rewritten]
    note = f"; rewritten: {', '.join(rewritten[:5])}{' ...' if len(rewritten) > 5 else ''}"
    outcome = "committed" if committed else "unchanged"
    return f"{summary(changes)}; {tables} tables; {outcome}{note if rewritten else ''}"


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data and git
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.record", description=__doc__)
    parser.add_argument("--no-push", action="store_true", help="commit but do not push")
    args = parser.parse_args(argv)
    from ..settings import operator_settings

    settings = operator_settings()
    try:
        sys.stdout.write(
            refresh(settings.data_root, settings.data_repo, push=not args.no_push) + "\n"
        )
    except RecordError as exc:
        sys.stderr.write(f"record failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
