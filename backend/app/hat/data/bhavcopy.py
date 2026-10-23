"""NSE bhavcopy: file names, the two parsers, the polite downloader and the day reconciliation
(HAT-DATA-090..093, 097; HAT-DATA-011).

Two file formats cover `FIRST_ISIN_DAY` (22 Jun 2011) to today: the old CM bhavcopy up to 5 Jul
2024 and UDiFF from 8 Jul 2024. Both parse into one `BhavRow` keyed by ISIN and series (symbol
changes need no lookup: the ISIN does not change), which `to_bar_rows` turns into the
`bars_daily_raw` rows the nightly ingest writes too. The downloader takes the network as a
`Fetcher` function; only `http_fetcher` here touches a socket and no test or check calls it.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from ..errors import HatError
from ..money import exact
from .reference import EQUITY_SERIES, ISIN_RE
from .rowcheck import Candidate, row_ok
from .store import MAX_VOLUME, BarRow, DataStore, atomic_replace

logger = logging.getLogger(__name__)

OLD_LAST_DAY = date(2024, 7, 5)
UDIFF_FIRST_DAY = date(2024, 7, 8)
# [A] Old-format files before this day have no ISIN column (the 21-JUN-2011 header ends at
# TIMESTAMP), so no bar can be keyed: the history starts here.
FIRST_ISIN_DAY = date(2011, 6, 22)
DEFAULT_INTERVAL_SECONDS = 1.0  # [A] about one request per second (HAT-DATA-092)
SOURCE_BHAVCOPY = "bhavcopy"
_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/zip,application/octet-stream,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
}

FORMAT_OLD = "old"
FORMAT_UDIFF = "udiff"
OLD_COLUMNS = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY"}
OLD_VALUE_COLUMN = "TOTTRDVAL"
UDIFF_COLUMNS = {
    "TradDt",
    "TckrSymb",
    "SctySrs",
    "ISIN",
    "OpnPric",
    "HghPric",
    "LwPric",
    "ClsPric",
    "PrvsClsgPric",
    "TtlTradgVol",
}
UDIFF_VALUE_COLUMN = "TtlTrfVal"


class BhavcopyError(HatError):
    """A bhavcopy file or request cannot be used; `code` is stable and carries no file text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BhavRow:
    """One security-day as both formats normalise it (HAT-DATA-093)."""

    trade_date: date
    isin: str
    series: str
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume: int
    traded_value: Decimal | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.isin, self.series)


@dataclass
class ParsedDay:
    """The rows of one file and what was left out (never silently)."""

    day: date
    rows: list[BhavRow] = field(default_factory=list)
    skipped_series: int = 0
    invalid: int = 0  # unreadable numbers, a bad ISIN, or a failed row check (HAT-DATA-042)
    wrong_date: int = 0  # rows dated another day than the file
    duplicates: int = 0  # a second row for one (ISIN, series): the first is kept


# --- file names (HAT-DATA-090) ------------------------------------------------------------------


def format_for(day: date) -> str:
    """The file format of a trading weekday; weekends, the gap 6-7 Jul 2024 and days before
    `FIRST_ISIN_DAY` have none."""
    if day.weekday() >= 5:
        raise BhavcopyError("bhavcopy_not_weekday")
    if day < FIRST_ISIN_DAY:
        raise BhavcopyError("bhavcopy_no_isin")
    if day <= OLD_LAST_DAY:
        return FORMAT_OLD
    if day >= UDIFF_FIRST_DAY:
        return FORMAT_UDIFF
    raise BhavcopyError("bhavcopy_format_gap")  # unreachable for weekdays; kept explicit


def archive_name(day: date) -> str:
    if format_for(day) == FORMAT_OLD:
        return f"cm{day.day:02d}{_MONTHS[day.month - 1]}{day.year}bhav.csv.zip"
    return f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"


def archive_url(day: date) -> str:
    name = archive_name(day)
    if format_for(day) == FORMAT_OLD:
        return (
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{day.year}/{_MONTHS[day.month - 1]}/{name}"
        )
    return f"https://nsearchives.nseindia.com/content/cm/{name}"


def weekdays(start: date, end: date) -> list[date]:
    """Every Monday-to-Friday from `start` to `end` inclusive (HAT-DATA-091)."""
    days = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


# --- parsers (HAT-DATA-093, 097) ----------------------------------------------------------------


def _integer(raw: str | None) -> int | None:
    value = exact(raw, grouped=True)
    if value is None or value != value.to_integral_value() or value < 0:
        return None
    return int(value)


def _old_date(raw: str) -> date | None:
    parts = raw.strip().upper().split("-")
    if len(parts) != 3 or parts[1] not in _MONTHS:
        return None
    year = parts[2]
    if len(year) == 2:  # [A] at least the 13-JUL-2020 file writes "13-Jul-20"
        year = "20" + year
    try:
        return date(int(year), _MONTHS.index(parts[1]) + 1, int(parts[0]))
    except ValueError:
        return None


def _iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _csv_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise BhavcopyError("bhavcopy_bad_archive")
            raw = archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise BhavcopyError("bhavcopy_bad_archive") from exc
    return raw.decode("utf-8-sig", errors="replace")


@dataclass(frozen=True)
class _Layout:
    required: set[str]
    date_col: str
    isin: str
    series: str
    symbol: str
    open: str
    high: str
    low: str
    close: str
    prev: str
    volume: str
    value: str
    parse_date: Callable[[str], date | None]


_OLD = _Layout(
    OLD_COLUMNS | {"ISIN", "TIMESTAMP"},
    "TIMESTAMP",
    "ISIN",
    "SERIES",
    "SYMBOL",
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "PREVCLOSE",
    "TOTTRDQTY",
    OLD_VALUE_COLUMN,
    _old_date,
)
_UDIFF = _Layout(
    UDIFF_COLUMNS,
    "TradDt",
    "ISIN",
    "SctySrs",
    "TckrSymb",
    "OpnPric",
    "HghPric",
    "LwPric",
    "ClsPric",
    "PrvsClsgPric",
    "TtlTradgVol",
    UDIFF_VALUE_COLUMN,
    _iso_date,
)


def _read_row(layout: _Layout, day: date, row: dict[str, str]) -> tuple[BhavRow | None, str]:
    """One file row as a `BhavRow`, or None and the `ParsedDay` counter that counts it."""
    series = row[layout.series].upper()
    if series not in EQUITY_SERIES:
        return None, "skipped_series"
    when = layout.parse_date(row[layout.date_col])
    if when is None or when != day:
        return None, "wrong_date"
    isin = row[layout.isin].upper()
    open_, high, low, close = (
        exact(row[c], grouped=True) for c in (layout.open, layout.high, layout.low, layout.close)
    )
    prev = exact(row[layout.prev], grouped=True)
    volume = _integer(row[layout.volume])
    if (
        not ISIN_RE.match(isin)
        or open_ is None
        or high is None
        or low is None
        or close is None
        or prev is None
        or volume is None
        or volume >= MAX_VOLUME
        or not row[layout.symbol]
        or not row_ok(Candidate(day, open_, high, low, close, prev, volume), day)
    ):
        return None, "invalid"
    symbol = row[layout.symbol].upper()
    value = exact(row.get(layout.value), grouped=True)
    return BhavRow(day, isin, series, symbol, open_, high, low, close, prev, volume, value), ""


def parse_bhavcopy(day: date, data: bytes) -> ParsedDay:
    """Parse the zip for `day` in the format its date selects. Only equity series are kept
    (counted when skipped); a row that cannot be read, or is dated another day, is counted and
    left out. An unusable file raises `BhavcopyError`."""
    layout = _OLD if format_for(day) == FORMAT_OLD else _UDIFF
    reader = csv.DictReader(io.StringIO(_csv_text(data)))
    header = {(h or "").strip() for h in (reader.fieldnames or [])}
    if not layout.required <= header:
        raise BhavcopyError("bhavcopy_columns")
    out = ParsedDay(day)
    seen: set[tuple[str, str]] = set()
    for raw in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
        parsed, left_out = _read_row(layout, day, row)
        if parsed is None:
            setattr(out, left_out, getattr(out, left_out) + 1)
        elif (parsed.isin, parsed.series) in seen:
            out.duplicates += 1
        else:
            seen.add((parsed.isin, parsed.series))
            out.rows.append(parsed)
    return out


class InstrumentIds(Protocol):
    def __call__(self, isin: str, series: str) -> object | None: ...


def to_bar_rows(
    parsed: ParsedDay, instrument_id: InstrumentIds, run_id: object
) -> tuple[list[BarRow], list[tuple[str, str]]]:
    """`bars_daily_raw` rows for the parsed day and the (isin, series) keys with no instrument,
    which are reported and not written."""
    rows: list[BarRow] = []
    unresolved: list[tuple[str, str]] = []
    for r in parsed.rows:
        found = instrument_id(r.isin, r.series)
        if found is None:
            unresolved.append(r.key)
            continue
        rows.append(
            BarRow(
                found,  # type: ignore[arg-type]
                r.isin,
                r.symbol,
                r.series,
                r.open,
                r.high,
                r.low,
                r.close,
                r.prev_close,
                r.volume,
                r.traded_value,
                SOURCE_BHAVCOPY,
                run_id,  # type: ignore[arg-type]
            )
        )
    return rows, unresolved


# --- download (HAT-DATA-092) --------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    status: Literal["ok", "not_found", "error"]
    data: bytes = b""


Fetcher = Callable[[str], FetchResult]


def http_fetcher(timeout: float = 30.0) -> Fetcher:  # pragma: no cover - the real network
    """The operator's fetcher: browser-like headers, one request per call. Never used by tests."""

    def fetch(url: str) -> FetchResult:
        import requests  # deferred: only a real build needs the HTTP stack

        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        except Exception:
            return FetchResult("error")
        if response.status_code == 404:
            return FetchResult("not_found")
        if response.status_code != 200 or not response.content:
            return FetchResult("error")
        return FetchResult("ok", response.content)

    return fetch


@dataclass
class BuildState:
    """Days NSE had no file for (holiday candidates), kept beside the data so a resume does not
    ask again. The run id of the build lives here too, so a resume writes the same files."""

    path: Path
    candidates: set[date] = field(default_factory=set)
    run_id: str | None = None

    @classmethod
    def load(cls, path: Path) -> BuildState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                path,
                {date.fromisoformat(d) for d in raw.get("candidates", [])},
                raw.get("run_id"),
            )
        except (OSError, ValueError):
            return cls(path)

    def save(self) -> None:
        payload = json.dumps(
            {"run_id": self.run_id, "candidates": sorted(d.isoformat() for d in self.candidates)}
        )
        atomic_replace(self.path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))


@dataclass
class DownloadReport:
    downloaded: list[date] = field(default_factory=list)
    already_have: list[date] = field(default_factory=list)
    candidates: list[date] = field(default_factory=list)  # no file: holiday candidates
    skipped_candidates: list[date] = field(default_factory=list)  # resume: asked before
    errors: list[date] = field(default_factory=list)  # transient failures: retried by a resume
    requested: list[date] = field(default_factory=list)


class Downloader:
    """Requests each missing day once, at least `interval` seconds after the previous request
    started, and keeps every zip untouched in `raw/`."""

    def __init__(
        self,
        store: DataStore,
        fetch: Fetcher,
        state: BuildState,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._state = state
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last_start: float | None = None

    def _wait_turn(self) -> None:
        if self._last_start is not None:
            remaining = self._interval - (self._clock() - self._last_start)
            if remaining > 0:
                self._sleep(remaining)
        self._last_start = self._clock()

    def run(self, days: Iterable[date], *, resume: bool = True) -> DownloadReport:
        report = DownloadReport()
        for day in days:
            name = archive_name(day)
            if self._store.raw_exists(name):
                report.already_have.append(day)
                continue
            if resume and day in self._state.candidates:
                report.skipped_candidates.append(day)
                continue
            self._wait_turn()
            report.requested.append(day)
            result = self._fetch(archive_url(day))
            if result.status == "ok":
                self._store.store_raw(name, result.data)
                self._state.candidates.discard(day)
                report.downloaded.append(day)
            elif result.status == "not_found":
                self._state.candidates.add(day)
                report.candidates.append(day)
            else:
                report.errors.append(day)
            self._state.save()
        return report


# --- day reconciliation (HAT-DATA-091) ----------------------------------------------------------


@dataclass
class DayReconciliation:
    downloaded: list[date] = field(default_factory=list)
    holidays: list[date] = field(default_factory=list)  # candidates the holiday list explains
    gaps: list[date] = field(default_factory=list)  # candidates it does not: a possible lost day
    unaccounted: list[date] = field(default_factory=list)  # neither downloaded nor a candidate
    on_holiday: list[date] = field(default_factory=list)  # downloaded although listed as holiday


def reconcile_days(
    days: Sequence[date],
    downloaded: Collection[date],
    candidates: Collection[date],
    holidays: Collection[date],
) -> DayReconciliation:
    """Put every weekday in exactly one bucket: downloaded, explained holiday, gap or
    unaccounted. A missing day is never counted as a holiday unless NSE's list says so."""
    have, cand, listed = set(downloaded), set(candidates), set(holidays)
    out = DayReconciliation()
    for day in days:
        if day in have:
            out.downloaded.append(day)
            if day in listed:
                out.on_holiday.append(day)
        elif day in cand:
            (out.holidays if day in listed else out.gaps).append(day)
        else:
            out.unaccounted.append(day)
    return out
