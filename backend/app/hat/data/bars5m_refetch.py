"""Refetch from Fyers the stored `bars_5m` sessions a vendor's copy left short (audit D4).

Two kinds of stored day are short at an edge. The Kotak export of 2026-10-28 ends at the 15:10 bar
on the sessions of its `bars5m_gaps` rows (`short_close`: the stored day has no 15:25 bar), and
the export's 09:15 auction-open bar was refused at import on many 2026 sessions (`no_first_bar`:
a NIFTY 500 member-day whose stored day has no 09:15 bar). A replay refuses both
(`backtest.intraday.on_basis`, `bars5m_gaps.in_gap`) and decides them on the daily estimate.

`fetch` asks Fyers for each target instrument's 1-minute history over its target days through
the minute-history download (`minute_history.load_symbol`: the same paced calls, candle screen,
aggregation and raw-basis conversion), then judges each target day (`admit`) by the rules every
5-minute day meets: it fits the raw daily bar (`check_day`), it is replayable (`on_basis`: the
09:15 bar, a clean grid, the open within 5% of the daily open), it holds the 15:25 bar, it lacks
no bucket the screen or the import refused, and its session volume reconciles with the
bhavcopy's (`verify.bars5m.day_volume_fits`). The admitted days' bars go to the run folder
(`bars5m_refetch/<name>` under the data root, `--run <name>`) with every target's outcome
(`outcomes.csv`); nothing is written to the store, and a stopped fetch continues with `--resume`.

`apply --run <name>` writes the admitted days of a fetch: it first saves every stored row of
those (instrument, day) to `before.parquet` in the run folder (synced), then replaces each day
whole (`BarStore.write`, one rewrite per day file), skipping a day whose stored bars changed since
the fetch. It holds the minute-history and the data-update locks, so no other `bars_5m` writer
runs with it. A day that is not admitted keeps its stored bars; the outcome says why.

`gaps --run <name>` prints the `bars5m_gaps` rows the applied run leaves (`--write` writes them to
`app.paths.BARS5M_GAPS`): a Kotak row whose short sessions were all admitted is dropped, the
others keep only their spans of sessions still short, with the reason.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from ..backtest.intraday import lacks_refused, on_basis
from ..time import IST, trade_date_of
from .bars import Bar
from .bars5m_gaps import APPLIED, REFETCH_DIR, Gap
from .bars_basis import DailyRange, check_day
from .bars_store import SCHEMA, BarStore, row_from_bar
from .minute_limits import HistoryLimits, MinuteHistoryError, Progress
from .verify.bars5m import day_volume_fits

if TYPE_CHECKING:
    from ..settings import HatSettings
    from .minute_history import Download

__all__ = [
    "NO_FIRST_BAR",
    "SHORT_CLOSE",
    "Target",
    "admit",
    "remaining_gaps",
    "run_folder",
    "short_spans",
    "target_kind",
]

SHORT_CLOSE = "short_close"  # a Kotak gap session stored without its 15:25 bar
NO_FIRST_BAR = "no_first_bar"  # a member-day stored without its 09:15 bar
ADMITTED = "admitted"
FIRST_BAR = time(9, 15)
LAST_BAR = time(15, 25)
KOTAK = "kotak"
OUTCOMES = "outcomes.csv"
BARS_DIR = "bars"
BEFORE = "before.parquet"
PROGRESS = "progress.json"
_RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
OUTCOME_FIELDS = (
    "instrument_id",
    "isin",
    "symbol",
    "day",
    "kind",
    "stored_bars",
    "stored_first",
    "stored_last",
    "outcome",
    "bars",
    "volume_ratio",
)


@dataclass(frozen=True)
class Target:
    """One stored day to refetch, with what the store held when it was chosen."""

    instrument_id: str
    isin: str
    symbol: str
    day: date
    kind: str
    stored_bars: int
    stored_first: time | None
    stored_last: time | None


def target_kind(
    first: time | None, last: time | None, *, in_kotak_gap: bool, member: bool
) -> str | None:
    """Why a stored day (first and last bar start, IST; None without bars) is refetched, or None:
    `short_close` for a Kotak gap session without the 15:25 bar, `no_first_bar` for a member-day
    with bars but no 09:15 bar. A gap session without bars is no truncated copy (nothing short is
    stored), so it is no target."""
    if first is None or last is None:
        return None
    if in_kotak_gap and last < LAST_BAR:
        return SHORT_CLOSE
    if member and first != FIRST_BAR:
        return NO_FIRST_BAR
    return None


def admit(
    day: date,
    bars: Sequence[Bar],
    daily: DailyRange | None,
    daily_volume: int | None,
    tolerance: Decimal,
    *,
    refused: Collection[datetime] = (),
    screened: int = 0,
) -> str | None:
    """None when a refetched day (`bars`, ordered by start, on the raw basis) may replace the
    stored one, else the first rule it breaks: `no_bars`, `screened_bucket` (the candle screen
    refused `screened` candles of the day), the `check_day` code, `not_on_basis`, `no_last_bar`,
    `lacks_refused` (a bucket the import refused, `refused`, is still missing) or
    `day_volume_differs` (against the bhavcopy's `daily_volume`)."""
    if not bars:
        return "no_bars"
    if screened:
        return "screened_bucket"
    code = check_day(bars, daily, tolerance)
    if code is not None:
        return code
    assert daily is not None  # check_day refuses a day without one
    if not on_basis(day, bars, daily.open):
        return "not_on_basis"
    if bars[-1].start.astimezone(IST).time() != LAST_BAR:
        return "no_last_bar"
    if lacks_refused(bars, refused):
        return "lacks_refused"
    if not daily_volume or not day_volume_fits(sum(b.volume for b in bars) / daily_volume):
        return "day_volume_differs"
    return None


def run_folder(root: Path, name: str) -> Path:
    """The folder of the run `name` under the data root `root` (`bars5m_refetch/<name>`); a name
    that is not a bare folder name is refused, so no run reads or writes outside it."""
    if not _RUN_NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError(f"refused: run name {name!r} is not a bare folder name")
    return root / REFETCH_DIR / name


def short_spans(short: Collection[date], sessions: Sequence[date]) -> list[tuple[date, date]]:
    """The runs of consecutive `sessions` (ordered) that are all in `short`, as (first, last)."""
    spans: list[tuple[date, date]] = []
    run: list[date] = []
    for day in sessions:
        if day in short:
            run.append(day)
        elif run:
            spans.append((run[0], run[-1]))
            run = []
    if run:
        spans.append((run[0], run[-1]))
    return spans


def remaining_gaps(
    gaps: Iterable[Gap],
    short: Mapping[str, Collection[date]],
    sessions: Mapping[str, Sequence[date]],
    reason: str,
) -> list[Gap]:
    """The gap rows left once the refetch made days whole: a Kotak row keeps only its spans of
    sessions still `short` (by ISIN; its `sessions`, ordered), each with `reason`, and is dropped
    when none is; every other row stays as it is."""
    out: list[Gap] = []
    for g in gaps:
        if g.vendor != KOTAK:
            out.append(g)
            continue
        inside = [d for d in sessions.get(g.isin, ()) if g.first <= d <= g.last]
        out.extend(
            Gap(g.vendor, a, b, g.isin, reason)
            for a, b in short_spans(set(short.get(g.isin, ())), inside)
        )
    return out


# --- the run (real Fyers, store and DB) -------------------------------------------------------


def _hhmm(value: time | None) -> str:
    return "" if value is None else value.strftime("%H:%M")


def _targets(
    root: Path, gaps: Sequence[Gap], first: date, last: date
) -> list[Target]:  # pragma: no cover - real store and DB
    """The target days of `first..last`: each stored day's first and last bar against the gap
    rows and the NIFTY 500 membership."""
    import duckdb
    from sqlalchemy import select

    from app.database import db_session_scope

    from ..models import HatIndexMember, HatInstrument

    with db_session_scope(writes_data=True) as db:
        names = {
            str(i): (isin, symbol)
            for i, isin, symbol in db.execute(
                select(HatInstrument.id, HatInstrument.isin, HatInstrument.symbol)
            )
        }
        members = [
            (str(m.instrument_id), m.effective_from, m.effective_to)
            for m in db.scalars(
                select(HatIndexMember).where(HatIndexMember.index_name == "NIFTY 500")
            )
        ]
    spans: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for g in gaps:
        if g.vendor == KOTAK:
            spans[g.isin].append((g.first, g.last))
    terms: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for instrument_id, start, end in members:
        terms[instrument_id].append((start, end))
    con = duckdb.connect()
    con.execute("SET memory_limit = '1500MB'")
    rows = con.execute(
        """
        SELECT instrument_id, CAST(date AS DATE), count(*),
            min(timezone('Asia/Kolkata', bar_start))::TIME,
            max(timezone('Asia/Kolkata', bar_start))::TIME
        FROM read_parquet($files, hive_partitioning = true)
        WHERE CAST(date AS DATE) BETWEEN $first AND $last GROUP BY 1, 2
        """,
        {"files": str(root / "bars_5m" / "*" / "*.parquet"), "first": first, "last": last},
    ).fetchall()
    out = []
    for instrument_id, day, n, first_bar, last_bar in rows:
        isin, symbol = names.get(instrument_id, ("", ""))
        kind = target_kind(
            first_bar,
            last_bar,
            in_kotak_gap=any(a <= day <= b for a, b in spans.get(isin, ())),
            member=any(
                a <= day and (b is None or day < b) for a, b in terms.get(instrument_id, ())
            ),
        )
        if kind:
            out.append(Target(instrument_id, isin, symbol, day, kind, n, first_bar, last_bar))
    return sorted(out, key=lambda t: (t.instrument_id, t.day))


def _daily_volumes(
    root: Path, ids: Sequence[str], first: date, last: date, memory: str | None
) -> dict[tuple[str, date], int]:  # pragma: no cover - real store
    from .store import RAW_LATEST, DataStore

    store = DataStore(root, memory_limit=memory)
    with closing(store.connect()) as con:
        rows = con.execute(
            f"SELECT CAST(instrument_id AS VARCHAR), date, volume FROM {RAW_LATEST} "  # noqa: S608
            "WHERE date BETWEEN ? AND ? AND list_contains(?, CAST(instrument_id AS VARCHAR))",
            [first, last, list(ids)],
        ).fetchall()
    return {(i, d): int(v) for i, d, v in rows if v is not None}


def _fyers_symbols(ids: Collection[str]) -> dict[str, str]:  # pragma: no cover - real DB
    from sqlalchemy import select

    from app.database import db_session_scope

    from ..configs.records import BROKER_FYERS
    from ..models import HatBrokerSymbol

    with db_session_scope(writes_data=True) as db:
        rows = db.execute(
            select(HatBrokerSymbol.instrument_id, HatBrokerSymbol.broker_symbol).where(
                HatBrokerSymbol.broker == BROKER_FYERS
            )
        ).all()
    return {str(i): s for i, s in rows if str(i) in ids}


def _outcome(t: Target, outcome: str, bars: Sequence[Bar], volume: int | None) -> dict[str, str]:
    total = sum(b.volume for b in bars)
    return {
        "instrument_id": t.instrument_id,
        "isin": t.isin,
        "symbol": t.symbol,
        "day": t.day.isoformat(),
        "kind": t.kind,
        "stored_bars": str(t.stored_bars),
        "stored_first": _hhmm(t.stored_first),
        "stored_last": _hhmm(t.stored_last),
        "outcome": outcome,
        "bars": str(len(bars)),
        "volume_ratio": f"{total / volume:.4f}" if volume and bars else "",
    }


def _append(path: Path, rows: Sequence[Mapping[str, str]]) -> None:  # pragma: no cover - file IO
    new = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTCOME_FIELDS)
        if new:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _judge(
    dl: Download,
    targets: Sequence[Target],
    symbol: str | None,
    volumes: Mapping[tuple[str, date], int],
    refused: Mapping[tuple[str, date], frozenset[datetime]],
) -> tuple[list[dict[str, str]], list[Bar]]:  # pragma: no cover - real Fyers
    """One instrument's targets fetched and judged: their outcome rows and the admitted bars."""
    from .minute_history import load_symbol

    instrument_id = targets[0].instrument_id
    if symbol is None:
        return [_outcome(t, "no_fyers_symbol", (), None) for t in targets], []
    first, last = targets[0].day, targets[-1].day
    loaded = load_symbol(dl, instrument_id, symbol, first, last)
    by_day: dict[date, list[Bar]] = defaultdict(list)
    for bar in sorted(loaded.bars, key=lambda b: b.start):
        by_day[trade_date_of(bar.start)].append(bar)
    screened = Counter(trade_date_of(r.at) for r in loaded.rejected if r.at is not None)
    daily = dl.basis.daily([instrument_id], first, last)
    rows, kept = [], []
    for t in targets:
        bars = by_day.get(t.day, [])
        volume = volumes.get((instrument_id, t.day))
        code = admit(
            t.day,
            bars,
            daily.get((instrument_id, t.day)),
            volume,
            dl.basis.tolerance,
            refused=refused.get((instrument_id, t.day), frozenset()),
            screened=screened[t.day],
        )
        rows.append(_outcome(t, code or ADMITTED, bars, volume))
        if code is None:
            kept.extend(bars)
    return rows, kept


def _save_bars(path: Path, bars: Sequence[Bar], instrument_id: str) -> None:  # pragma: no cover
    rows = [row_from_bar(b, instrument_id) for b in bars]
    table = pa.Table.from_pylist([r.__dict__ for r in rows], schema=SCHEMA)
    pq.write_table(table, path)


def _fetch(args: argparse.Namespace, settings: HatSettings) -> int:  # pragma: no cover - real Fyers
    from app.paths import BARS5M_GAPS

    from .bars5m_gaps import read_gaps
    from .minute_history import (
        Download,
        HistoryReport,
        history_pacer,
        raw_basis,
        session_history,
    )

    root = settings.data_root
    out = run_folder(root, args.run)
    (out / BARS_DIR).mkdir(parents=True, exist_ok=True)
    progress = Progress.load(out / PROGRESS)
    if not args.resume:
        progress.done.clear()
        progress.failed.clear()
        (out / OUTCOMES).unlink(missing_ok=True)
    targets = _targets(root, read_gaps(BARS5M_GAPS), args.first, args.last)
    by_instrument: dict[str, list[Target]] = defaultdict(list)
    for t in targets:
        by_instrument[t.instrument_id].append(t)
    symbols = _fyers_symbols(by_instrument)
    volumes = _daily_volumes(
        root, sorted(by_instrument), args.first, args.last, settings.duckdb_memory
    )
    keys = {(t.instrument_id, t.day) for t in targets}
    refused: dict[tuple[str, date], set[datetime]] = defaultdict(set)
    for r in BarStore(root).read_rejects():
        if r.bar_start is not None and (r.instrument_id, r.day) in keys:
            refused[(r.instrument_id, r.day)].add(r.bar_start)
    history = session_history()
    if history is None:
        sys.stderr.write("history_no_session: log in to Fyers from the app first\n")
        return 2
    limits = HistoryLimits.from_settings(settings, per_minute=args.per_minute)
    dl = Download(
        history,
        BarStore(root),
        history_pacer(limits, progress),
        limits,
        progress,
        HistoryReport(args.first, args.last, symbols=len(by_instrument)),
        raw_basis(settings),
    )
    stopped = None
    for instrument_id, group in sorted(by_instrument.items()):
        if instrument_id in progress.done:
            continue
        try:
            rows, kept = _judge(
                dl,
                group,
                symbols.get(instrument_id),
                volumes,
                {k: frozenset(v) for k, v in refused.items()},
            )
        except MinuteHistoryError as exc:
            if exc.code in ("history_daily_limit", "history_rate_limited", "history_plan_limit"):
                stopped = exc.code
                break
            rows, kept = [_outcome(t, exc.code, (), None) for t in group], []
        if kept:
            _save_bars(out / BARS_DIR / f"{instrument_id}.parquet", kept, instrument_id)
        _append(out / OUTCOMES, rows)
        progress.done[instrument_id] = (group[0].day, group[-1].day)
        progress.save()
    _print_outcomes(out)
    if stopped:
        sys.stdout.write(f"stopped: {stopped} (run again with --resume)\n")
        return 1
    return 0


def _read_outcomes(out: Path) -> list[dict[str, str]]:  # pragma: no cover - file IO
    path = out / OUTCOMES
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _print_outcomes(out: Path) -> None:  # pragma: no cover - file IO
    counts = Counter((r["kind"], r["day"][:7], r["outcome"]) for r in _read_outcomes(out))
    for (kind, month, outcome), n in sorted(counts.items()):
        sys.stdout.write(f"{kind} {month} {outcome} {n}\n")


def _stored_state(rows: Sequence[object]) -> tuple[str, str, str]:
    """A stored day's (bars, first, last) as `outcomes.csv` records them."""
    starts = sorted(r.bar_start.astimezone(IST).time() for r in rows)  # type: ignore[attr-defined]
    if not starts:
        return ("0", "", "")
    return (str(len(starts)), _hhmm(starts[0]), _hhmm(starts[-1]))


def _apply(args: argparse.Namespace, settings: HatSettings) -> int:  # pragma: no cover - real store
    from .minute_history import PROGRESS_FILE, run_lock

    root = settings.data_root
    run = run_folder(root, args.run)
    admitted = [r for r in _read_outcomes(run) if r["outcome"] == ADMITTED]
    by_day: dict[date, dict[str, dict[str, str]]] = defaultdict(dict)
    for r in admitted:
        by_day[date.fromisoformat(r["day"])][r["instrument_id"]] = r
    fresh: dict[tuple[str, date], list[Bar]] = defaultdict(list)
    for iid in sorted({r["instrument_id"] for r in admitted}):
        for row in pq.read_table(run / BARS_DIR / f"{iid}.parquet", schema=SCHEMA).to_pylist():
            start = row["bar_start"].astimezone(IST)
            bar = Bar(iid, start, row["open"], row["high"], row["low"], row["close"], row["volume"])
            fresh[(iid, start.date())].append(bar)
    store = BarStore(root)
    with ExitStack() as stack:
        stack.enter_context(run_lock(root / PROGRESS_FILE.with_suffix(".lock")))
        stack.enter_context(run_lock(root / "update" / "update.lock"))
        plan: dict[date, list[str]] = {}
        before: list[dict[str, object]] = []
        changed = Counter()
        for day, rows in sorted(by_day.items()):
            stored = defaultdict(list)
            for r in store.read(day, rows):
                stored[r.instrument_id].append(r)
            ok = []
            for iid, r in rows.items():
                if _stored_state(stored[iid]) != (
                    r["stored_bars"],
                    r["stored_first"],
                    r["stored_last"],
                ):
                    changed[r["kind"]] += 1
                    continue
                ok.append(iid)
                before.extend(s.__dict__ for s in stored[iid])
            plan[day] = ok
        days = Counter(
            (by_day[d][i]["kind"], d.year, d.month) for d, ids in plan.items() for i in ids
        )
        for (kind, year, month), n in sorted(days.items()):
            sys.stdout.write(f"{kind} {year}-{month:02d} days {n}\n")
        new_bars = sum(len(fresh[(i, d)]) for d, ids in plan.items() for i in ids)
        sys.stdout.write(
            f"days {sum(days.values())}, stored bars replaced {len(before)}, "
            f"bars written {new_bars}, skipped as changed since the fetch {dict(changed)}\n"
        )
        if not args.apply:
            sys.stdout.write("dry run: nothing written (--apply writes)\n")
            return 0
        backup = run / BEFORE
        with backup.open("wb") as handle:
            pq.write_table(pa.Table.from_pylist(before, schema=SCHEMA), handle)
            handle.flush()
            os.fsync(handle.fileno())
        applied = []
        for day, ids in sorted(plan.items()):
            if not ids:
                continue
            bars = [b for i in ids for b in fresh[(i, day)]]
            store.write(
                bars, dict(zip(ids, ids, strict=True)), replace=dict.fromkeys(ids, (day, day))
            )
            applied.extend({"instrument_id": i, "day": day.isoformat()} for i in ids)
        with (run / APPLIED).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=("instrument_id", "day"))
            writer.writeheader()
            writer.writerows(applied)
        sys.stdout.write(f"written: {len(applied)} days; backup {backup}\n")
    return 0


def _gaps(args: argparse.Namespace, settings: HatSettings) -> int:  # pragma: no cover - real data
    from app.paths import BARS5M_GAPS

    from .bars5m_gaps import read_gaps
    from .store import RAW_LATEST, DataStore

    gaps = read_gaps(BARS5M_GAPS)
    with BARS5M_GAPS.open(encoding="utf-8", newline="") as handle:
        names = {r["isin"]: r["symbol"] for r in csv.DictReader(handle)}
    run = run_folder(settings.data_root, args.run)
    outcomes = [r for r in _read_outcomes(run) if r["kind"] == SHORT_CLOSE]
    applied_path = run / APPLIED
    if applied_path.is_file():
        with applied_path.open(encoding="utf-8", newline="") as handle:
            done = {(r["instrument_id"], r["day"]) for r in csv.DictReader(handle)}
    else:  # before the write: as if every admitted day were written
        sys.stderr.write("no applied.csv: the rows as if every admitted day were written\n")
        done = {(r["instrument_id"], r["day"]) for r in outcomes if r["outcome"] == ADMITTED}
    short: dict[str, set[date]] = defaultdict(set)
    why: Counter[str] = Counter()
    for r in outcomes:
        if (r["instrument_id"], r["day"]) not in done:
            short[r["isin"]].add(date.fromisoformat(r["day"]))
            why[r["outcome"]] += 1
    ids = sorted({r["instrument_id"] for r in outcomes})
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    first = min((g.first for g in gaps if g.vendor == KOTAK), default=date.today())
    last = max((g.last for g in gaps if g.vendor == KOTAK), default=date.today())
    isin_of = {r["instrument_id"]: r["isin"] for r in outcomes}
    with closing(store.connect()) as con:
        rows = con.execute(
            f"SELECT CAST(instrument_id AS VARCHAR), date FROM {RAW_LATEST} "  # noqa: S608
            "WHERE date BETWEEN ? AND ? AND list_contains(?, CAST(instrument_id AS VARCHAR)) "
            "ORDER BY 2",
            [first, last, ids],
        ).fetchall()
    rows = [(isin_of[i], d) for i, d in rows]
    sessions: dict[str, list[date]] = defaultdict(list)
    for isin, day in rows:
        sessions[isin].append(day)
    reason = (
        f"The Kotak export of 2026-10-28 ends at the 15:10 bar on these sessions and the Fyers "
        f"refetch of {run.name} could not make them whole"
    )
    left = remaining_gaps(gaps, short, sessions, reason)
    left = [g if g.vendor != KOTAK else _with_causes(g, outcomes, done) for g in left]
    target = BARS5M_GAPS if args.write else Path("/dev/stdout")
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("vendor", "first", "last", "isin", "symbol", "reason"))
        for g in left:
            writer.writerow((g.vendor, g.first, g.last, g.isin, names.get(g.isin, ""), g.reason))
    kept = sum(1 for g in left if g.vendor == KOTAK)
    sys.stderr.write(
        f"kotak rows {sum(1 for g in gaps if g.vendor == KOTAK)} -> {kept}; "
        f"short sessions left {sum(why.values())}: {dict(why)}\n"
    )
    return 0


def _with_causes(
    gap: Gap, outcomes: Sequence[Mapping[str, str]], done: Collection[tuple[str, str]]
) -> Gap:
    """`gap` with the outcomes of its sessions still short appended to its reason."""
    codes = Counter(
        r["outcome"]
        for r in outcomes
        if r["isin"] == gap.isin
        and gap.first <= date.fromisoformat(r["day"]) <= gap.last
        and (r["instrument_id"], r["day"]) not in done
    )
    detail = ", ".join(f"{c} {n}" for c, n in sorted(codes.items()))
    return Gap(gap.vendor, gap.first, gap.last, gap.isin, f"{gap.reason} ({detail})")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.bars5m_refetch", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="fetch and judge the target days (writes only --out)")
    f.add_argument("--first", type=date.fromisoformat, required=True)
    f.add_argument("--last", type=date.fromisoformat, required=True)
    f.add_argument("--run", required=True, help=f"the run's name (folder {REFETCH_DIR}/<name>)")
    f.add_argument("--resume", action="store_true", help="skip the instruments already judged")
    f.add_argument(
        "--per-minute", type=int, help="override HAT_HISTORY_PER_MINUTE (a shared token)"
    )
    a = sub.add_parser("apply", help="write a fetch's admitted days (dry run without --apply)")
    a.add_argument("--run", required=True, help=f"the run's name (folder {REFETCH_DIR}/<name>)")
    a.add_argument("--apply", action="store_true")
    g = sub.add_parser("gaps", help="the bars5m_gaps rows an applied run leaves")
    g.add_argument("--run", required=True, help=f"the run's name (folder {REFETCH_DIR}/<name>)")
    g.add_argument("--write", action="store_true", help="write app.paths.BARS5M_GAPS (else stdout)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data
    """python -m app.hat.data.bars5m_refetch {fetch,apply,gaps} ... (see the module docstring)"""
    from ..settings import operator_settings

    args = _parse_args(argv)
    settings = operator_settings()
    try:
        if args.command == "fetch":
            if args.first > args.last or args.last >= date.today():
                raise MinuteHistoryError("history_bad_period")
            return _fetch(args, settings)
        if args.command == "apply":
            return _apply(args, settings)
        return _gaps(args, settings)
    except MinuteHistoryError as exc:
        sys.stderr.write(f"{exc.code}\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
