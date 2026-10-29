"""D1: every stored raw daily bar against its row in NSE's bhavcopy file, every session.

The file is read here by a reader of its own (zip, CSV, the format's column names), not by
`bhavcopy.parse_bhavcopy`, so a parser bug cannot hide in both sides. Each stored row is matched
to the file's row of its ISIN and series; its symbol, OHLC, previous close, volume and traded
value must equal the file's exactly. A file row dated other than the day the file stands for (a
stale or misnamed file, a shifted row), and a file with no equity rows, are defects: such a row is
never matched. Each other equity row of the file that is not stored is counted by why: its ISIN
is stored that day under a better-ranked series (one row per ISIN, `identity.best_per_isin`), the
row fails the row check (`rowcheck.row_ok`), its ISIN has no instrument, or none of these (a
defect). Every failed finding carries the days it was seen on.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ...money import exact
from ..audit import FAIL, MAX_LISTED, WARN, check_result, finding
from ..bhavcopy import FIRST_ISIN_DAY, BhavcopyError, archive_name, format_for
from ..identity import series_rank
from ..reference import EQUITY_SERIES
from ..rowcheck import Candidate, row_ok
from ..store import DATASET_BARS
from . import dated
from .context import Context

FIELDS = (
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "traded_value",
)
# Each format's columns for the fields above, then series, ISIN and trade date.
COLUMNS = {
    "old": (
        "SYMBOL", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTTRDVAL",
        "SERIES", "ISIN", "TIMESTAMP",
    ),
    "udiff": (
        "TckrSymb", "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric", "TtlTradgVol",
        "TtlTrfVal", "SctySrs", "ISIN", "TradDt",
    ),
}  # fmt: skip
# Why a file row is not stored: explained exclusions, and the defect.
OTHER_SERIES = "other_series"
ROW_CHECK = "row_check"
NO_INSTRUMENT = "no_instrument"
OFF_DATE = "source_row_off_date"
EMPTY = "source_without_equity_rows"
DUPLICATE = "duplicate_in_file"
NOT_STORED = "source_row_not_stored"


@dataclass(frozen=True)
class SourceRow:
    """One file row as text-parsed values: a number that cannot be read is None."""

    day: date | None
    isin: str
    series: str
    symbol: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    prev_close: Decimal | None
    volume: Decimal | None
    traded_value: Decimal | None


def _number(raw: str | None) -> Decimal | None:
    return exact(raw or "", grouped=True)  # blank and `-` are no number


def _file_date(raw: str) -> date | None:
    text = raw.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d-%m-%Y"):  # 13-Jul-20 on 13 Jul 2020
        try:
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007 - a calendar date
        except ValueError:
            continue
    return None


def read_source(fmt: str, data: bytes) -> list[SourceRow]:
    """Every row of one bhavcopy zip (the one CSV in it), of every series."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError("verify_bhavcopy_archive")
        text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
    cols = COLUMNS[fmt]
    out = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        if not row.get(cols[9]):
            continue
        out.append(
            SourceRow(
                _file_date(row.get(cols[10], "")),
                row[cols[9]].upper(),
                row.get(cols[8], "").upper(),
                row.get(cols[0], "").upper(),
                *(_number(row.get(c)) for c in cols[1:8]),
            )
        )
    return out


def _value(row: SourceRow | Mapping[str, Any], name: str) -> object:
    value = getattr(row, name) if isinstance(row, SourceRow) else row.get(name)
    if isinstance(value, int):
        return Decimal(value)
    return value


def _checkable(row: SourceRow, day: date) -> bool:
    volume = row.volume
    return (
        volume is not None
        and volume == volume.to_integral_value()
        and row_ok(
            Candidate(row.day, row.open, row.high, row.low, row.close, row.prev_close, int(volume)),
            day,
        )
    )


@dataclass
class DayResult:
    """One day's comparison: the counts, and examples of every finding."""

    stored: int = 0
    matched: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def add(self, code: str, item: Mapping[str, Any]) -> None:
        self.counts[code] += 1
        if len(self.examples[code]) < MAX_LISTED:
            self.examples[code].append(dict(item))


def compare_day(
    day: date,
    stored: Sequence[Mapping[str, Any]],
    source: Sequence[SourceRow],
    known_isins: Collection[str],
) -> DayResult:
    """A day's stored rows (dicts with `isin`, `series` and the `FIELDS`) against its file's
    rows. Findings: `value_mismatch` (one per field), `stored_not_in_source`,
    `stored_duplicate`, the defects `OFF_DATE` (a file row not dated `day`, never matched) and
    `EMPTY` (no equity row), and per unstored equity file row its reason (`OTHER_SERIES`,
    `ROW_CHECK`, `NO_INSTRUMENT`, `DUPLICATE` or the defect `NOT_STORED`)."""
    out = DayResult(stored=len(stored))
    by_key: dict[tuple[str, str], SourceRow] = {}
    equity = [row for row in source if row.series in EQUITY_SERIES]
    if not equity:
        out.add(EMPTY, {"day": day.isoformat(), "rows": len(source)})
    for row in equity:
        if row.day != day:
            out.add(
                OFF_DATE,
                {
                    "day": day.isoformat(),
                    "isin": row.isin,
                    "series": row.series,
                    "file_date": None if row.day is None else row.day.isoformat(),
                },
            )
            continue
        key = (row.isin, row.series)
        if key in by_key:
            out.add(DUPLICATE, {"isin": row.isin, "series": row.series})
            continue
        by_key[key] = row
    stored_series: dict[str, str] = {}
    used: set[tuple[str, str]] = set()
    for bar in stored:
        isin, series = str(bar["isin"]), str(bar["series"])
        if isin in stored_series:
            out.add("stored_duplicate", {"day": day.isoformat(), "isin": isin})
            continue
        stored_series[isin] = series
        match = by_key.get((isin, series))
        if match is None:
            out.add(
                "stored_not_in_source", {"day": day.isoformat(), "isin": isin, "series": series}
            )
            continue
        used.add((isin, series))
        # Decimals compare numerically: 12.5 == 12.5000
        wrong = [f for f in FIELDS if _value(bar, f) != _value(match, f)]
        for name in wrong:
            out.add(
                "value_mismatch",
                {
                    "day": day.isoformat(),
                    "isin": isin,
                    "symbol": match.symbol,
                    "field": name,
                    "stored": str(_value(bar, name)),
                    "source": str(_value(match, name)),
                },
            )
        if not wrong:
            out.matched += 1
    for key, row in by_key.items():
        if key in used:
            continue
        item = {
            "day": day.isoformat(),
            "isin": row.isin,
            "series": row.series,
            "symbol": row.symbol,
        }
        held = stored_series.get(row.isin)
        if held is not None and series_rank(held) < series_rank(row.series):
            out.add(OTHER_SERIES, item)
        elif not _checkable(row, day):
            out.add(ROW_CHECK, item)
        elif row.isin not in known_isins:
            out.add(NO_INSTRUMENT, item)
        else:
            out.add(NOT_STORED, item)
    return out


DEFECTS = (
    "value_mismatch",
    "stored_not_in_source",
    "stored_duplicate",
    OFF_DATE,
    EMPTY,
    NOT_STORED,
)
EXCLUDED = (OTHER_SERIES, ROW_CHECK, NO_INSTRUMENT, DUPLICATE)


def summarise(
    results: Iterable[tuple[date, DayResult]],
    missing_files: Sequence[date],
    unstored_days: Sequence[date],
) -> dict[str, Any]:
    """The check from the days' results: every defect fails; a file row with no instrument
    warns (a security HAT does not know); the other exclusions are counted."""
    total: Counter[str] = Counter()
    examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    mismatch_fields: Counter[str] = Counter()
    years: dict[int, Counter[str]] = defaultdict(Counter)
    seen: dict[str, list[date]] = defaultdict(list)
    days = 0
    for day, result in results:
        days += 1
        year = years[day.year]
        year["days"] += 1
        year["stored"] += result.stored
        year["matched"] += result.matched
        for code, n in result.counts.items():
            total[code] += n
            if code in DEFECTS:
                year[code] += n
                seen[code].append(day)
            room = MAX_LISTED - len(examples[code])
            examples[code].extend(result.examples[code][:room])
        for item in result.examples.get("value_mismatch", ()):
            mismatch_fields[item["field"]] += 1
    findings = []
    if missing_files:
        items = [d.isoformat() for d in missing_files]
        findings.append(dated(finding("raw_file_missing", FAIL, items), missing_files))
    if unstored_days:
        items = [d.isoformat() for d in unstored_days]
        findings.append(dated(finding("raw_day_not_stored", FAIL, items), unstored_days))
    for code in DEFECTS:
        if total[code]:
            findings.append(dated(_counted(code, FAIL, total[code], examples[code]), seen[code]))
    if total[NO_INSTRUMENT]:
        findings.append(
            _counted(NO_INSTRUMENT, WARN, total[NO_INSTRUMENT], examples[NO_INSTRUMENT])
        )
    summary = {
        "days": days,
        "stored_rows": sum(y["stored"] for y in years.values()),
        "matched_rows": sum(y["matched"] for y in years.values()),
        "excluded": {code: total[code] for code in EXCLUDED},
        "mismatch_fields_in_examples": dict(mismatch_fields),
        "by_year": {str(y): dict(c) for y, c in sorted(years.items())},
    }
    return check_result("D1", summary, findings)


def _counted(code: str, severity: str, count: int, items: Sequence[Any]) -> dict[str, Any]:
    return {**finding(code, severity, items), "count": count}


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    from sqlalchemy import select  # noqa: PLC0415

    from ...models import HatInstrument  # noqa: PLC0415

    known = set(ctx.session.scalars(select(HatInstrument.isin)).all())
    start = max(ctx.start, FIRST_ISIN_DAY)
    stored_days = [d for d in ctx.store.days(DATASET_BARS) if start <= d <= ctx.end]
    raw_days = find_raw_days(ctx, start)
    missing = [d for d in stored_days if d not in raw_days]
    unstored = sorted(set(raw_days) - set(stored_days))
    store = ctx.store

    def each() -> Iterable[tuple[date, DayResult]]:
        for day in stored_days:
            if day not in raw_days:
                continue
            source = read_source(format_for(day), store.read_raw(raw_days[day]))
            stored = store.read_bars(day).select("isin", "series", *FIELDS).to_dicts()
            yield day, compare_day(day, stored, source, known)

    return summarise(each(), missing, unstored)


def find_raw_days(ctx: Context, start: date) -> dict[date, str]:  # pragma: no cover - real data
    """The window's sessions with a bhavcopy file under `raw/`, by the file's name."""
    found = {}
    for day in _weekdays_and_weekends(start, ctx.end):
        try:
            name = archive_name(day)
        except BhavcopyError:  # the weekend between the formats
            continue
        if ctx.store.raw_exists(name):
            found[day] = name
    return found


def _weekdays_and_weekends(start: date, end: date) -> Iterable[date]:
    day = start
    while day <= end:
        yield day
        day = date.fromordinal(day.toordinal() + 1)


__all__ = ["SourceRow", "compare_day", "read_source", "run", "summarise"]
