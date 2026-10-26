"""Bring every stored `bars_5m` day back to the raw basis the store promises (audit F1).

The minute-history download of 2026-10-22 ran before the raw basis (audit D1) existed: it wrote
the broker's candles as answered, adjusted for every action up to that day, without the envelope
check, and under the chain's current instrument even on the days before an ISIN change;
`bars5m_import` then kept those days as stored. This command takes every stored (instrument,
day) that `check_day` refuses (`bars_basis.misfit_days`) and mends it (`mend`):

- a day filed under a chain member without a raw daily bar that day belongs to the member that
  has one (`bars5m_import.owner_of`); when that member already holds bars of the day the filed
  day is a duplicate and is dropped;
- a day of the download's period (`--first..--last`) is converted with the stored factors as of
  the day it was fetched (`--fetched-on`, `fyers_factor` of the chain) when it does not fit as
  stored (`bars_basis.mend_day`); days outside the period were written checked, so they are
  never converted;
- a day that still does not fit, or that has no owner, is dropped and ledgered in
  `bars_5m_rejects` (a missing day, never a wrong one).

A day that fits as stored is never touched, so a second run finds nothing to do. Dry run by
default; `--apply` rewrites each affected day file once, appends the ledger and records every
mended day in `bars5m_repair/mended-<time>.csv` under the data root.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ..time import IST
from .bars import Bar
from .bars5m_import import chains_of, owner_of
from .bars_basis import KEPT, NO_DAILY_BAR, DailyRange, RawBasis, mend_day, misfit_days
from .bars_store import BarStore, RejectRow
from .minute_limits import MinuteHistoryError
from .run_report import save_report
from .store import DataStore

if TYPE_CHECKING:
    from ..settings import HatSettings

__all__ = ["CHAIN_DUPLICATE", "Mended", "RepairReport", "Window", "mend", "repair"]

CHAIN_DUPLICATE = "chain_duplicate"  # the day's owner already holds bars of that day
CONVERTED_ACTION, REFILED, DROPPED = "converted", "refiled", "dropped"
REPORT_DIR = Path("bars5m_repair")  # under the HAT data root


@dataclass(frozen=True)
class Window:
    """The period an earlier download wrote unconverted, and the day it was fetched on."""

    first: date
    last: date
    fetched_on: date


@dataclass(frozen=True)
class Mended:
    """What became of one stored (instrument, day): `converted` in place, `refiled` under the
    chain member `owner` (converted when `factor` is not 1) or `dropped` with `code`."""

    instrument_id: str
    day: date
    action: str
    owner: str | None
    factor: Decimal
    code: str | None = None


def mend(
    instrument_id: str,
    day: date,
    bars: Sequence[Bar],
    chain: Sequence[str],
    daily: Mapping[tuple[str, date], DailyRange],
    held: set[str],
    factor: Decimal,
    tolerance: Decimal,
) -> tuple[Mended | None, list[Bar]]:
    """What becomes of the stored bars of `instrument_id` on `day` (a member of `chain`, whose
    raw daily bars of the day are in `daily`; `held` is every instrument with bars that day) and
    the bars to write in their place, under their owner; None when the day fits as it is."""
    owner = instrument_id if (instrument_id, day) in daily else owner_of(day, chain, daily)
    if owner is None:
        return Mended(instrument_id, day, DROPPED, None, factor, NO_DAILY_BAR), []
    if owner != instrument_id and owner in held:
        return Mended(instrument_id, day, DROPPED, owner, factor, CHAIN_DUPLICATE), []
    filed = [replace(b, symbol=owner) for b in bars]
    verdict, out = mend_day(filed, factor, daily[(owner, day)], tolerance)
    if verdict == KEPT and owner == instrument_id:
        return None, []
    if not out:
        return Mended(instrument_id, day, DROPPED, owner, factor, verdict), []
    used = Decimal(1) if verdict == KEPT else factor
    action = REFILED if owner != instrument_id else CONVERTED_ACTION
    return Mended(instrument_id, day, action, owner, used), out


@dataclass
class RepairReport:
    """What one run found and did (or would do, in a dry run)."""

    misfits: int = 0
    mended: list[Mended] = field(default_factory=list)
    rows: int = 0
    dry_run: bool = False

    def summary(self) -> str:
        actions = Counter(
            m.action if m.code is None else f"{m.action} {m.code}" for m in self.mended
        )
        done = ", ".join(f"{a} {n}" for a, n in sorted(actions.items())) or "nothing"
        days = {m.day for m in self.mended}
        names = {m.instrument_id for m in self.mended}
        span = f" ({min(days)}..{max(days)})" if days else ""
        return (
            f"bars5m repair{' (dry run: nothing written)' if self.dry_run else ''}: stored days "
            f"off the raw daily bar {self.misfits}: {done}; {len(names)} instruments, "
            f"{len(days)} trade dates{span}, bars written {self.rows}\n"
        )


def repair(
    store: BarStore,
    misfits: Sequence[tuple[str, date]],
    basis: RawBasis,
    lineage: Mapping[str, str],
    window: Window,
    *,
    dry_run: bool = True,
) -> RepairReport:
    """Mend each of `misfits` (stored (instrument, day) pairs off the raw daily bar), one day
    file at a time: its mended instruments' bars are replaced whole (`replace`), the refiled and
    converted bars written in their place and the dropped days ledgered. `lineage` maps an older
    instrument to its chain's current id, the id the chain's factors are kept under."""
    report = RepairReport(misfits=len(misfits), dry_run=dry_run)
    chains = chains_of(lineage)
    by_day: dict[date, list[str]] = {}
    for instrument_id, day in misfits:
        by_day.setdefault(day, []).append(instrument_id)
    for day, ids in sorted(by_day.items()):
        heads = {i: lineage.get(i, i) for i in ids}
        members = {i: chains.get(heads[i], (heads[i],)) for i in ids}
        daily = basis.daily(sorted({m for chain in members.values() for m in chain}), day, day)
        held = store.instruments(day)
        bars: dict[str, list[Bar]] = {}
        for row in store.read(day, ids):
            start = row.bar_start.astimezone(IST)
            bars.setdefault(row.instrument_id, []).append(
                Bar(row.instrument_id, start, row.open, row.high, row.low, row.close, row.volume)
            )
        keep: list[Bar] = []
        mended: list[Mended] = []
        for instrument_id in sorted(ids):
            within = window.first <= day <= window.last
            factor = (
                basis.factor(heads[instrument_id], day, window.fetched_on) if within else Decimal(1)
            )
            done, out = mend(
                instrument_id,
                day,
                bars.get(instrument_id, []),
                members[instrument_id],
                daily,
                held,
                factor,
                basis.tolerance,
            )
            if done is None:
                continue
            mended.append(done)
            keep.extend(out)
            if done.action == REFILED:
                held.add(str(done.owner))  # a second misfiled member is then a duplicate
        report.mended.extend(mended)
        if dry_run or not mended:
            continue
        report.rows += store.write(
            keep,
            {b.symbol: b.symbol for b in keep},
            replace={m.instrument_id: (day, day) for m in mended},
        )
        store.write_rejects(
            RejectRow(m.instrument_id, m.instrument_id, m.day, None, str(m.code))
            for m in mended
            if m.action == DROPPED
        )
    return report


def write_mended(path: Path, mended: Sequence[Mended]) -> None:  # pragma: no cover - file IO
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        out = csv.writer(handle)
        out.writerow(["instrument_id", "day", "action", "owner", "factor", "code"])
        for m in mended:
            out.writerow([m.instrument_id, m.day, m.action, m.owner or "", m.factor, m.code or ""])


# --- the command line ------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.hat.data.bars5m_repair", description=__doc__)
    p.add_argument(
        "--first", type=date.fromisoformat, required=True, help="the download's first day"
    )
    p.add_argument("--last", type=date.fromisoformat, required=True, help="its last day")
    p.add_argument(
        "--fetched-on",
        type=date.fromisoformat,
        required=True,
        help="the IST day the download fetched its candles",
    )
    p.add_argument("--apply", action="store_true", help="write (default: a dry run)")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data and DB
    """python -m app.hat.data.bars5m_repair --first D --last D --fetched-on D [--apply]

    Mends the stored `bars_5m` days under `HAT_DATA_ROOT` that do not fit the raw daily bar
    (reads the database: lineage and corporate-action factors; writes none). Holds the
    minute-history lock: one `bars_5m` history writer at a time."""
    from ..settings import operator_settings
    from .minute_history import PROGRESS_FILE as HISTORY_PROGRESS
    from .minute_history import run_lock

    args = _parse_args(argv)
    if args.first > args.last or args.fetched_on <= args.last:
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
        now = datetime.now(IST)
        write_mended(root / REPORT_DIR / f"mended-{now:%Y%m%dT%H%M%S}.csv", report.mended)
        save_report(root / REPORT_DIR, text, now)
    return 0


def _run(args: argparse.Namespace, settings: HatSettings) -> RepairReport:  # pragma: no cover
    from app.database import db_session_scope

    from .bars_basis import store_daily
    from .chains import stored_adjustment

    with db_session_scope(writes_data=True) as db:
        adjustment = stored_adjustment(db)
    data = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    tolerance = settings.history_envelope_tol
    basis = RawBasis(adjustment.factors, store_daily(data), tolerance)
    lineage = {str(old): str(new) for old, new in adjustment.lineage.items()}
    return repair(
        BarStore(settings.data_root),
        misfit_days(data, tolerance),
        basis,
        lineage,
        Window(args.first, args.last, args.fetched_on),
        dry_run=not args.apply,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
