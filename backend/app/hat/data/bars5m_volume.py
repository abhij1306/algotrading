"""Restore the raw volume of the stored `bars_5m` days the Kotak import cut by a factor (audit D4).

The Kotak export adjusts its prices for the splits and bonuses after a day but not its volume,
and the import used to convert both (`bars_basis.to_raw`), so a day before such an action was
stored with its volume multiplied by the action's factor (a tenth of the day's trades before a
1:10 split). This command restores a stored (instrument, day) from the export
(`--kotak`) when the day is a candidate (its session volume does not reconcile with the
bhavcopy's, `verify.bars5m.day_volume_fits`, or differs from the export's by more than
`EXPORT_SLACK`) and the export proves it: every stored bar is an export bar of the same prices at
the day's scale and the export's volume reconciles (`verify.bars5m.restored_volumes`), and every
stored bar's volume is the export's times one common factor other than 1 (`common_factor`: the
mark of the import's conversion, which a second vendor's differing bars do not carry). The prices
are never touched. A candidate the export cannot prove is left as it is and counted.

Dry run by default: prints the counts by year. `--apply` first writes every changed bar's old
and new volume to `bars5m_volume/before-<UTC time>.parquet` under the data root (synced), then
rewrites each affected day file once (`BarStore.write`). A second run finds nothing to restore.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING

from ..time import IST
from .bars import Bar
from .bars_store import DATASET, FILE_NAME, BarStore
from .verify.bars5m import (
    SESSION,
    SourceBar,
    day_volume_fits,
    one_per_label,
    restored_volumes,
    source_rows,
)

if TYPE_CHECKING:
    from .store import DataStore

REPORT_DIR = "bars5m_volume"
BATCH = 40  # instruments whose candidate days are read from the store together

EXPORT_SLACK = 0.005  # a day's stored session volume this far off the export's is a candidate
FACTOR_SLACK = 0.005  # a bar within this share (or one share) of the common factor (rounding)
FACTOR_SHARE = 0.9  # the export volume the bars on the common factor must hold

_DAYS_SQL = """
SELECT CAST(instrument_id AS VARCHAR), CAST(timezone('Asia/Kolkata', bar_start) AS DATE),
       sum(volume) FILTER (WHERE CAST(timezone('Asia/Kolkata', bar_start) AS TIME) < TIME '15:30')
FROM read_parquet($files) GROUP BY ALL
"""

_DAILY_SQL = """
SELECT CAST(instrument_id AS VARCHAR), date, volume FROM bars_daily_raw_latest
WHERE date BETWEEN $lo AND $hi
"""

_BARS_SQL = """
SELECT CAST(instrument_id AS VARCHAR), bar_start, open, high, low, close, volume
FROM read_parquet($file) WHERE CAST(instrument_id AS VARCHAR) IN (SELECT unnest($ids))
"""


@dataclass(frozen=True, slots=True)
class Change:
    instrument_id: str
    bar_start: datetime  # UTC
    old: int
    new: int


@dataclass
class VolumeReport:
    off: Counter[int] = field(default_factory=Counter)  # days off the bhavcopy, by year
    scaled: Counter[int] = field(default_factory=Counter)  # fit it, but off the export's volume
    restored: Counter[int] = field(default_factory=Counter)
    # the export proves them and every stored bar already has its volume (a repaired day)
    agrees: Counter[int] = field(default_factory=Counter)
    unproven: Counter[int] = field(default_factory=Counter)  # the export cannot prove them
    no_file: Counter[int] = field(default_factory=Counter)  # the export has no bars for them
    changes: list[Change] = field(default_factory=list)
    left: list[tuple[str, date, str]] = field(default_factory=list)  # instrument, day, why

    def summary(self) -> str:
        lines = ["year off scaled restored agrees unproven no_file"]
        for year in sorted(self.off.keys() | self.scaled.keys()):
            lines.append(
                f"{year} {self.off[year]} {self.scaled[year]} {self.restored[year]} "
                f"{self.agrees[year]} {self.unproven[year]} {self.no_file[year]}"
            )
        lines.append(f"bars changed: {len(self.changes)}")
        return "\n".join(lines) + "\n"


def changes_of(
    instrument_id: str,
    day: date,
    stored: Mapping[time, tuple[datetime, Sequence[float]]],
    volumes: Mapping[time, int],
) -> list[Change]:
    """The bars of a restored day whose volume changes (`stored`: start time -> UTC start and
    the stored open, high, low, close, volume)."""
    return [
        Change(instrument_id, at, int(bar[4]), volumes[t])
        for t, (at, bar) in sorted(stored.items())
        if volumes[t] != int(bar[4])
    ]


def common_factor(pairs: Iterable[tuple[int, int]]) -> float | None:
    """The one factor `f` with stored = export x `f` on a day's bars, from the bars' (stored,
    export) volumes: `f` is the day's ratio, and the bars within one share or `FACTOR_SLACK` of it
    must hold at least `FACTOR_SHARE` of the export's volume (a trade at a bucket boundary may sit
    in the neighbouring bar in one copy); None otherwise, or when no bar traded."""
    pairs = list(pairs)
    total = sum(k for _, k in pairs)
    if not total:
        return None
    f = sum(s for s, _ in pairs) / total
    held = sum(k for s, k in pairs if abs(s - k * f) <= max(1.0, FACTOR_SLACK * s))
    return f if held >= FACTOR_SHARE * total else None


RESTORED, AGREES, UNPROVEN = "restored", "agrees", "unproven"


def verdict(stored: Mapping[time, int], volumes: Mapping[time, int] | None) -> str:
    """A candidate day's verdict from its stored bar volumes and the export's (`restored_volumes`,
    None when the export cannot prove the day): `AGREES` when every stored bar already has the
    export's volume (a repaired day: the candidate test sums the export by label, the store by
    start, so the edge bars can keep it a candidate), `RESTORED` when they differ by one common
    factor other than 1 (`common_factor`), else `UNPROVEN`."""
    if volumes is None:
        return UNPROVEN
    if stored and all(stored[t] == volumes[t] for t in stored):
        return AGREES
    factor = common_factor((stored[t], volumes[t]) for t in stored)
    return UNPROVEN if factor is None or abs(factor - 1) <= EXPORT_SLACK else RESTORED


def is_candidate(stored: int, daily: int | None, export: int) -> tuple[bool, bool]:
    """Whether a day is a candidate: (off the bhavcopy's volume, off the export's only)."""
    off = bool(daily) and not day_volume_fits(stored / daily)  # type: ignore[operator]
    scaled = not off and export > 0 and abs(stored / export - 1) > EXPORT_SLACK
    return off, scaled


def _day_volumes(
    store: DataStore,
) -> dict[str, dict[date, tuple[int, int | None]]]:  # pragma: no cover
    """Every stored day's session volume and the bhavcopy's, by instrument."""
    files = sorted((store.root / DATASET).glob(f"date=*/{FILE_NAME}"))
    out: dict[str, dict[date, tuple[int, int | None]]] = defaultdict(dict)
    with closing(store.connect()) as con:
        for at in range(0, len(files), 20):
            chunk = files[at : at + 20]
            lo, hi = (
                date.fromisoformat(f.parent.name.removeprefix("date="))
                for f in (chunk[0], chunk[-1])
            )
            daily = {
                (i, d): v for i, d, v in con.execute(_DAILY_SQL, {"lo": lo, "hi": hi}).fetchall()
            }
            rows = con.execute(_DAYS_SQL, {"files": [str(f) for f in chunk]}).fetchall()
            for i, d, v in rows:
                volume = daily.get((i, d))
                out[i][d] = (int(v or 0), None if volume is None else int(volume))
    return out


def _stored_bars(
    store: DataStore, by_day: Mapping[date, Sequence[str]]
) -> dict[tuple[str, date], dict[time, tuple[datetime, tuple[float, ...]]]]:  # pragma: no cover
    out: dict[tuple[str, date], dict[time, tuple[datetime, tuple[float, ...]]]] = defaultdict(dict)
    with closing(store.connect()) as con:
        for day, ids in sorted(by_day.items()):
            path = store.root / DATASET / f"date={day.isoformat()}" / FILE_NAME
            rows = con.execute(_BARS_SQL, {"file": str(path), "ids": list(ids)}).fetchall()
            for i, start, o, h, lo, c, v in rows:
                at = start.astimezone(IST)
                bar = (float(o), float(h), float(lo), float(c), int(v))
                out[(i, at.date())][at.time()] = (start.astimezone(UTC), bar)
    return out


def _judge(
    report: VolumeReport,
    batch: Mapping[str, Mapping[date, tuple[int | None, list[SourceBar]]]],
    stored: Mapping[tuple[str, date], dict[time, tuple[datetime, tuple[float, ...]]]],
) -> None:
    for instrument, days in sorted(batch.items()):
        for day, (daily_volume, rows) in sorted(days.items()):
            mine = stored.get((instrument, day), {})
            volumes = restored_volumes(
                day, {t: bar for t, (_, bar) in mine.items()}, rows, daily_volume
            )
            why = verdict({t: int(bar[4]) for t, (_, bar) in mine.items()}, volumes)
            if why == AGREES:
                report.agrees[day.year] += 1
                continue
            if why == UNPROVEN:
                report.unproven[day.year] += 1
                report.left.append((instrument, day, UNPROVEN))
                continue
            report.restored[day.year] += 1
            report.changes += changes_of(instrument, day, mine, volumes)  # type: ignore[arg-type]


def _export_rows(
    kotak: Path, names: Mapping[str, Sequence[str]], instrument: str, days: Iterable[date]
) -> dict[date, list[SourceBar]]:  # pragma: no cover - files
    """The export's rows of `days` under any of the instrument's symbols, one per label, by day."""
    paths = [
        kotak / folder / f"{symbol}.parquet"
        for symbol in dict.fromkeys(names.get(instrument, ()))
        for folder in ("raw_5m", "raw_5m_former")
    ]
    by_day: dict[date, list[SourceBar]] = defaultdict(list)
    for row in one_per_label(r for p in paths if p.is_file() for r in source_rows(p, days)):
        by_day[row.label.date()].append(row)
    return by_day


def _candidates(
    report: VolumeReport,
    instrument: str,
    days: Mapping[date, tuple[int, int | None]],
    export: Mapping[date, list[SourceBar]],
) -> dict[date, tuple[int | None, list[SourceBar]]]:
    """The instrument's candidate days with export rows (`is_candidate`), counted in `report`;
    a candidate without them is left."""
    out: dict[date, tuple[int | None, list[SourceBar]]] = {}
    for day, (volume, daily) in days.items():
        rows = export.get(day, [])
        total = sum(r.volume for r in rows if SESSION[0] <= r.label.time() < SESSION[1])
        off, scaled = is_candidate(volume, daily, total)
        report.off[day.year] += off
        report.scaled[day.year] += scaled
        if not (off or scaled):
            continue
        if not rows:
            report.no_file[day.year] += 1
            report.left.append((instrument, day, "no_file"))
            continue
        out[day] = (daily, rows)
    return out


def restore(
    store: DataStore, kotak: Path, names: Mapping[str, Sequence[str]]
) -> VolumeReport:  # pragma: no cover - real data
    """The changes that restore every candidate day the export proves (nothing written)."""
    report = VolumeReport()
    batch: dict[str, dict[date, tuple[int | None, list[SourceBar]]]] = {}

    def flush() -> None:
        by_day: dict[date, list[str]] = defaultdict(list)
        for instrument, days in batch.items():
            for day in days:
                by_day[day].append(instrument)
        _judge(report, batch, _stored_bars(store, by_day))
        batch.clear()

    for instrument, days in sorted(_day_volumes(store).items()):
        mine = _candidates(report, instrument, days, _export_rows(kotak, names, instrument, days))
        if mine:
            batch[instrument] = mine
        if len(batch) >= BATCH:
            flush()
    flush()
    return report


def write_backup(path: Path, changes: Iterable[Change]) -> None:  # pragma: no cover - file IO
    """Every changed bar's old and new volume, written (and synced) before the store changes."""
    import os  # noqa: PLC0415

    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    rows = list(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "instrument_id": [c.instrument_id for c in rows],
            "bar_start": pa.array([c.bar_start for c in rows], pa.timestamp("us", tz="UTC")),
            "old_volume": [c.old for c in rows],
            "new_volume": [c.new for c in rows],
        }
    )
    pq.write_table(table, path)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def apply(store: BarStore, changes: Sequence[Change]) -> int:  # pragma: no cover - real data
    """Each affected day file rewritten once with the new volumes, prices as stored."""
    by_day: dict[date, dict[tuple[str, datetime], int]] = defaultdict(dict)
    for c in changes:
        by_day[c.bar_start.astimezone(IST).date()][(c.instrument_id, c.bar_start)] = c.new
    written = 0
    for day, new in sorted(by_day.items()):
        ids = sorted({i for i, _ in new})
        bars = [
            Bar(r.instrument_id, r.bar_start, r.open, r.high, r.low, r.close, new[r.key])
            for r in store.read(day, ids)
            if r.key in new
        ]
        written += store.write(bars, {i: i for i in ids})
    return written


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - real data and DB
    """python -m app.hat.data.bars5m_volume --kotak DIR [--apply]

    Reads the database for the instruments' symbols only. Holds the minute-history lock: one
    `bars_5m` history writer at a time."""
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ..models import HatMarket  # noqa: PLC0415
    from ..settings import operator_settings  # noqa: PLC0415
    from .minute_history import PROGRESS_FILE as HISTORY_PROGRESS  # noqa: PLC0415
    from .minute_history import run_lock  # noqa: PLC0415
    from .store import DataStore  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="python -m app.hat.data.bars5m_volume", description=__doc__
    )
    parser.add_argument("--kotak", type=Path, required=True, help="the export's folder")
    parser.add_argument("--apply", action="store_true", help="write (default: a dry run)")
    parser.add_argument("--left", type=Path, help="write the days left as they are (CSV) here")
    args = parser.parse_args(argv)
    root = operator_settings().data_root
    names: dict[str, list[str]] = defaultdict(list)
    with db_session_scope() as session:
        for i, symbol in session.execute(
            text(
                "SELECT CAST(instrument_id AS TEXT), symbol FROM hat_instrument_symbol_history "
                "UNION SELECT CAST(id AS TEXT), symbol FROM hat_instrument"
            ),
            bind_arguments={"mapper": HatMarket},
        ).all():
            names[i].append(symbol)
        session.rollback()
    with run_lock(root / HISTORY_PROGRESS.with_suffix(".lock")):
        report = restore(DataStore(root), args.kotak, names)
        sys.stdout.write(report.summary())
        if args.left:
            with args.left.open("w", newline="") as handle:
                csv.writer(handle).writerows([("instrument_id", "day", "why"), *report.left])
        if args.apply and report.changes:
            path = root / REPORT_DIR / f"before-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.parquet"
            write_backup(path, report.changes)
            sys.stdout.write(f"backup: {path}\n")
            sys.stdout.write(f"bars written: {apply(BarStore(root), report.changes)}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
