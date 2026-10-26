"""Import the broker's 5-minute history (one Parquet file per NSE symbol) into `bars_5m`.

A source file holds `ts, open, high, low, close, volume` rows, `ts` an aware IST label. The
history labels a bar by its end up to `END_LABELLED_UNTIL` (the opening bar is labelled 09:20
and the session's last 15:30) and by its start after it; `start_epoch` gives every row its
start. Each row then goes through the minute-candle policy as a candle of its own bucket
(`candles.parse_candles`, `screen`, `aggregate`): a row that breaks a `REJECT_RULES` rule is
ledgered with its bucket, a zero-volume row that breaks none is kept, and a row that starts
outside the live builder's session `[09:15, 15:30)` (the flat pre-open rows at 09:05 and 09:10,
a 15:30 placeholder after the close) belongs to no bar, as in `BarBuilder`.

The history is adjusted for corporate actions up to the day it was downloaded (the file's
modification day), but not for every one: most splits, bonuses and rights issues are carried,
most demergers and a few splits and bonuses are not. `carried` decides for each stored action
from the sessions before its ex-date, `bars_basis.to_raw` converts each day with the carried
factors, and every day must fit the raw daily bar of that day (`check_day`) or it is ledgered,
never written: a wrong decision costs days, never a wrong bar.

A file's symbol maps to an instrument through its ISIN (`universe_raw.csv`, cross-checked with
the scrip master); a symbol without one, or with two, is reported and not imported. A day belongs
to the member of the ISIN chain (the instrument and its `linked` older ISINs) that has the raw
daily bar of that day, so a renamed symbol keeps its instrument and a day before an ISIN change
goes to the older ISIN, as in `bars_daily_raw`.

Precedence (decisions.md): an (instrument, day) already in `bars_5m` is kept as it is; the
import adds only the days the store does not hold, so it overwrites and deletes nothing, and a
second run writes nothing new. Symbols are written `batch` at a time; `progress.json` records each
imported file with its modification time, so a stopped run resumes after the last batch written,
and a file downloaded again is imported again.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..time import IST, trade_date_of
from .bars import Bar, BarBuilder
from .bars_basis import (
    NO_DAILY_BAR,
    DailyRange,
    RawBasis,
    check_day,
    fyers_factor,
    store_daily,
    to_raw,
)
from .bars_store import BarStore, RejectRow
from .candles import aggregate, parse_candles, screen
from .minute_limits import MinuteHistoryError
from .run_report import save_report
from .store import DataStore, atomic_replace

if TYPE_CHECKING:
    from ..settings import HatSettings

__all__ = [
    "END_LABELLED_UNTIL",
    "carried",
    "ImportReport",
    "import_symbols",
    "isin_of",
    "main",
    "owner_of",
    "start_epoch",
]

# The last day the history labels a bar by its end; from the next session on, by its start.
END_LABELLED_UNTIL = date(2022, 1, 21)
_SWITCH = int(datetime(2022, 1, 22, tzinfo=IST).timestamp())
_WIDTH = int(timedelta(minutes=5).total_seconds())
CARRY_WINDOW = 15  # sessions before an ex-date that decide whether the history carries it
_LAST_BAR = (15, 25)  # the session's last 5-minute bar starts at 15:25
PROGRESS_FILE = Path("bars5m_import") / "progress.json"  # under the HAT data root
_COLUMNS = ("ts", "open", "high", "low", "close", "volume")


def start_epoch(label: int) -> int:
    """The start (epoch seconds) of the source bar labelled `label`: five minutes earlier while
    the history labels by the bar's end (through `END_LABELLED_UNTIL`), the label after."""
    return label - _WIDTH if label < _SWITCH else label


def owner_of(
    day: date, members: Sequence[str], daily: Mapping[tuple[str, date], object]
) -> str | None:
    """The chain member that has the raw daily bar of `day`; None when none or more than one
    has (fails closed: the day is ledgered `no_daily_bar`)."""
    held = [m for m in members if (m, day) in daily]
    return held[0] if len(held) == 1 else None


def isin_of(symbol: str, universe: Mapping[str, str], scrips: Mapping[str, set[str]]) -> str | None:
    """The ISIN of `symbol`: the universe's, which the scrip master must not contradict, else
    the scrip master's single one; None when neither has one or they disagree."""
    found = set(scrips.get(symbol, ()))
    if symbol in universe:
        found.add(universe[symbol])
    return found.pop() if len(found) == 1 else None


def read_universe(path: Path) -> dict[str, str]:
    """`universe_raw.csv`: symbol, ISIN, series, ... (no header) -> symbol to ISIN."""
    with path.open(encoding="utf-8", newline="") as handle:
        return {row[0].strip(): row[1].strip() for row in csv.reader(handle) if len(row) > 1}


SHARE_SERIES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})  # the scrip master's share groups


def read_scrips(path: Path) -> dict[str, set[str]]:
    """The scrip master's cash-market share rows -> symbol to the ISINs listed for it (a
    company's bonds share its symbol under their own ISIN)."""
    out: dict[str, set[str]] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol, isin = (row.get("pSymbolName") or "").strip(), (row.get("pISIN") or "").strip()
            if (
                symbol
                and isin
                and row.get("pExchSeg") == "nse_cm"
                and row.get("pGroup") in SHARE_SERIES
            ):
                out.setdefault(symbol, set()).add(isin)
    return out


@dataclass
class ImportReport:
    """What one run did. Day verdicts count (instrument, day) pairs; `rules` counts the
    source rows refused by `candles.REJECT_RULES`."""

    symbols: int = 0
    imported: int = 0
    skipped: int = 0  # imported before from the same file
    unmapped: list[str] = field(default_factory=list)
    written: int = 0  # days written
    rows: int = 0
    kept_stored: int = 0  # days the store already held (precedence: kept)
    rejected: Counter[str] = field(default_factory=Counter)  # days, by code
    rules: Counter[str] = field(default_factory=Counter)  # rows, by rule
    outside: int = 0  # rows outside the session (pre-open, after the close)
    no_closing_bar: int = 0  # days written without the 15:25 bar
    uncarried: list[str] = field(default_factory=list)  # SYMBOL:ex-date the history ignores
    dry_run: bool = False

    def summary(self) -> str:
        rejected = ", ".join(f"{c} {n}" for c, n in sorted(self.rejected.items())) or "none"
        rules = ", ".join(f"{c} {n}" for c, n in sorted(self.rules.items())) or "none"
        lines = [
            f"bars5m import{' (dry run: nothing written)' if self.dry_run else ''}: symbols "
            f"{self.symbols} (imported {self.imported}, skipped as done {self.skipped}, unmapped "
            f"{len(self.unmapped)}), days written {self.written} ({self.rows} bars), kept as "
            f"stored {self.kept_stored}, days rejected: {rejected}",
            f"rows refused: {rules}; rows outside the session dropped {self.outside}; days "
            f"written without the 15:25 bar {self.no_closing_bar}",
        ]
        if self.uncarried:
            lines.append("actions the history does not carry: " + " ".join(self.uncarried))
        if self.unmapped:
            lines.append("unmapped: " + " ".join(sorted(self.unmapped)))
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Source:
    """One source file to import: its symbol, the instrument chain it maps to (the current id
    first) and the IST day it was downloaded, which the adjustment it carries is as of."""

    path: Path
    symbol: str
    chain: tuple[str, ...]
    fetched_on: date
    stamp: str  # the file's modification time, what the progress records


def carried(
    steps: Mapping[date, Decimal],
    days: Sequence[date],
    fits: Callable[[date, Sequence[tuple[date, Decimal]]], bool],
    window: int = CARRY_WINDOW,
) -> list[tuple[date, Decimal]]:
    """The actions `(ex-date, factor)` of `steps` (one factor per ex-date) the history carries.
    From the latest ex-date back, an action is carried unless the last `window` of `days`
    before it (and on or after the previous ex-date) fit the raw daily bar (`fits(day, carried
    actions)`) strictly more often without it: a tie keeps the stored assumption."""
    kept: list[tuple[date, Decimal]] = []
    exes = sorted(steps, reverse=True)
    for i, ex in enumerate(exes):
        floor = exes[i + 1] if i + 1 < len(exes) else date.min
        before = [d for d in days if floor <= d < ex][-window:]
        trial = [*kept, (ex, steps[ex])]
        if sum(fits(d, kept) for d in before) <= sum(fits(d, trial) for d in before):
            kept = trial
    return kept


def _rows(path: Path) -> list[list[object]]:
    """The file's rows as history candles `[start epoch, open, high, low, close, volume]`."""
    table = pq.read_table(path, columns=list(_COLUMNS))
    seconds = pc.divide(pc.cast(pc.cast(table["ts"], pa.timestamp("ns", "UTC")), pa.int64()), 10**9)
    columns = [seconds.to_pylist(), *(table[c].to_pylist() for c in _COLUMNS[1:])]
    return [[start_epoch(row[0]), *row[1:]] for row in zip(*columns, strict=True)]


def _import_one(
    source: Source,
    basis: RawBasis,
    daily: Mapping[tuple[str, date], DailyRange],
    stored: set[tuple[str, date]],
    report: ImportReport,
) -> tuple[list[Bar], list[RejectRow]]:
    """The raw bars of the days of `source` to write, and its ledger rows."""
    chain, rules = source.chain, BarBuilder()
    parsed = parse_candles(_rows(source.path))
    report.outside += sum(1 for c in parsed.candles if rules.bar_start(c.at) is None)
    by_day: dict[date, list[Bar]] = {}
    for bar in aggregate(chain[0], screen(parsed, rules), rules):
        by_day.setdefault(trade_date_of(bar.start), []).append(bar)
    rejects = []
    for r in parsed.rejected:
        day = trade_date_of(r.at) if r.at is not None else None
        owner = owner_of(day, chain, daily) if day is not None else None
        bucket = rules.bar_start(r.at) if r.at is not None else None
        rejects.append(RejectRow(owner or chain[0], source.symbol, day, bucket, r.rule))
        report.rules[r.rule] += 1
    steps: dict[date, Decimal] = {}
    for ex, f in basis.actions(chain[0]):
        if ex <= source.fetched_on:
            steps[ex] = steps.get(ex, Decimal(1)) * f

    def fits(day: date, actions: Sequence[tuple[date, Decimal]]) -> bool:
        owner, factor = owner_of(day, chain, daily), fyers_factor(actions, day, source.fetched_on)
        raw = [to_raw(b, factor) for b in by_day[day]]
        return owner is not None and check_day(raw, daily[(owner, day)], basis.tolerance) is None

    actions = carried(steps, sorted(by_day), fits)
    report.uncarried.extend(
        f"{source.symbol}:{ex}" for ex in sorted(set(steps) - {e for e, _ in actions})
    )
    keep: list[Bar] = []
    for day, group in sorted(by_day.items()):
        owner = owner_of(day, chain, daily)
        if owner is not None and (owner, day) in stored:
            report.kept_stored += 1
            continue
        factor = fyers_factor(actions, day, source.fetched_on)
        raw = [replace(to_raw(b, factor), symbol=owner or chain[0]) for b in group]
        code = (
            NO_DAILY_BAR if owner is None else check_day(raw, daily[(owner, day)], basis.tolerance)
        )
        if code is not None:
            rejects.append(RejectRow(owner or chain[0], source.symbol, day, None, code))
            report.rejected[code] += 1
            continue
        keep.extend(raw)
        report.written += 1
        if (raw[-1].start.hour, raw[-1].start.minute) != _LAST_BAR:
            report.no_closing_bar += 1
    return keep, rejects


def _stored_days(store: BarStore) -> set[tuple[str, date]]:
    """Every (instrument, day) `bars_5m` holds."""
    return {(i, day) for day in store.days() for i in store.instruments(day)}


def import_symbols(
    sources: Sequence[Source],
    store: BarStore,
    basis: RawBasis,
    progress: dict[str, str],
    save: Callable[[dict[str, str]], None],
    *,
    batch: int,
    first: date,
    last: date,
    dry_run: bool = False,
) -> ImportReport:
    """Import `sources` into `store`, `batch` symbols per write: each batch's raw daily bars are
    read once, its new days written in one `BarStore.write` (no `replace`: nothing stored is
    dropped), its ledger rows appended, and only then its files recorded in `progress` (`save`).
    A file recorded with the same stamp is skipped. `dry_run` writes and records nothing."""
    report = ImportReport(symbols=len(sources), dry_run=dry_run)
    todo = [s for s in sources if progress.get(s.symbol) != s.stamp]
    report.skipped = len(sources) - len(todo)
    stored = _stored_days(store)
    for at in range(0, len(todo), batch):
        part = todo[at : at + batch]
        daily = basis.daily(sorted({i for s in part for i in s.chain}), first, last)
        keep: list[Bar] = []
        rejects: list[RejectRow] = []
        for source in part:
            bars, refused = _import_one(source, basis, daily, stored, report)
            keep.extend(bars)
            rejects.extend(refused)
            report.imported += 1
        if dry_run:
            continue
        report.rows += store.write(keep, {b.symbol: b.symbol for b in keep})
        store.write_rejects(rejects)
        stored |= {(b.symbol, trade_date_of(b.start)) for b in keep}
        progress.update({s.symbol: s.stamp for s in part})
        save(progress)
    return report


def chains_of(lineage: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Each chain's current id -> the chain (that id first, then its linked older ids)."""
    members: dict[str, list[str]] = {}
    for old, new in sorted(lineage.items()):
        members.setdefault(new, []).append(old)
    return {new: (new, *olds) for new, olds in members.items()}


def plan_sources(
    files: Iterable[Path],
    isins: Mapping[str, str | None],
    ids: Mapping[str, str],
    lineage: Mapping[str, str],
) -> tuple[list[Source], list[str]]:
    """The files to import (by symbol) and the symbols that map to no instrument."""
    chains = chains_of(lineage)
    sources, unmapped = [], []
    for path in sorted(files):
        symbol = path.stem
        isin = isins.get(symbol)
        current = ids.get(isin) if isin else None
        if current is None:
            unmapped.append(symbol)
            continue
        head = lineage.get(current, current)
        stat = path.stat()
        sources.append(
            Source(
                path,
                symbol,
                chains.get(head, (head,)),
                datetime.fromtimestamp(stat.st_mtime, IST).date(),
                str(stat.st_mtime_ns),
            )
        )
    return sources, unmapped


# --- the command line ------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.bars5m_import", description=__doc__)
    p.add_argument("--source", type=Path, required=True, help="the directory of <SYMBOL>.parquet")
    p.add_argument("--universe", type=Path, help="default: <source>/../universe_raw.csv")
    p.add_argument("--scrips", type=Path, help="default: <source>/../nse_cm_scrips.csv")
    p.add_argument("--batch", type=int, default=4, help="symbols per write (default 4)")
    p.add_argument("--apply", action="store_true", help="write (default: a dry run)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data and DB
    """python -m app.hat.data.bars5m_import --source DIR [--batch N] [--apply]

    Imports the broker's 5-minute history files in DIR into `bars_5m` under `HAT_DATA_ROOT`
    (reads the database: instruments, lineage and corporate-action factors; writes none). Holds
    the minute-history lock: one `bars_5m` history writer at a time."""
    from ..settings import operator_settings
    from .minute_history import PROGRESS_FILE as HISTORY_PROGRESS
    from .minute_history import run_lock

    args = _parse_args(argv)
    if args.batch < 1 or not args.source.is_dir():
        sys.stderr.write("bars5m_bad_arguments\n")
        return 2
    settings = operator_settings()
    root = settings.data_root
    try:
        with run_lock(root / HISTORY_PROGRESS.with_suffix(".lock")):
            report = _run(args, settings)
    except MinuteHistoryError as exc:
        sys.stderr.write(f"{exc.code}\n")
        return 2
    text = report.summary()
    sys.stdout.write(text)
    if args.apply:
        save_report(root / PROGRESS_FILE.parent, text, datetime.now(IST))
    return 0


def _run(args: argparse.Namespace, settings: HatSettings) -> ImportReport:  # pragma: no cover
    from sqlalchemy import select

    from app.database import db_session_scope

    from ..models import HatInstrument
    from .chains import stored_adjustment

    with db_session_scope(writes_data=True) as db:
        adjustment = stored_adjustment(db)
        pairs = db.execute(select(HatInstrument.isin, HatInstrument.id)).all()
    counts = Counter(isin for isin, _ in pairs)
    ids = {isin: str(i) for isin, i in pairs if counts[isin] == 1}
    data = args.source.parent
    universe = read_universe(args.universe or data / "universe_raw.csv")
    scrips = read_scrips(args.scrips or data / "nse_cm_scrips.csv")
    files = sorted(args.source.glob("*.parquet"))
    isins = {f.stem: isin_of(f.stem, universe, scrips) for f in files}
    sources, unmapped = plan_sources(files, isins, ids, dict(adjustment.lineage))
    root = settings.data_root
    basis = RawBasis(
        adjustment.factors,
        store_daily(DataStore(root, memory_limit=settings.duckdb_memory)),
        settings.history_envelope_tol,
    )
    path = root / PROGRESS_FILE
    progress = _load_progress(path)
    today = datetime.now(IST).date()
    report = import_symbols(
        sources,
        BarStore(root),
        basis,
        progress,
        lambda done: _save_progress(path, done),
        batch=args.batch,
        first=today - timedelta(days=6 * 366),
        last=today,
        dry_run=not args.apply,
    )
    report.symbols += len(unmapped)
    report.unmapped = unmapped
    return report


def _load_progress(path: Path) -> dict[str, str]:  # pragma: no cover - file IO
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw["done"].items()}
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise MinuteHistoryError("bars5m_bad_progress") from exc


def _save_progress(path: Path, done: Mapping[str, str]) -> None:  # pragma: no cover - file IO
    body = json.dumps({"done": dict(sorted(done.items()))}).encode()
    atomic_replace(path, lambda tmp: tmp.write_bytes(body))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
