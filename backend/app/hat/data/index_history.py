"""Index history: NIFTY 50, NIFTY 500, Smallcap 250 and India VIX (HAT-DATA-095), and the
NIFTY 500 total-return index the backtest report charts against.

The MCP's `get_index_history` keeps about two years; NSE's daily index archive
(`ind_close_all_DDMMYYYY.csv`) fills the days before, and niftyindices' copy of the same files
fills the days NSE's archive lacks (some regular sessions and most special ones). The history
build loads them (`load_index_history`), and the nightly ingest reads the archive file's levels
for one day (`parse_index_archive_csv`). The TRI comes from niftyindices' total-return series,
close only, and niftyindices' price history fills a day neither file source has (both
`NiftyIndicesSource`). `backfill` loads the bar days an index lacks, from each source in turn;
a day no source has stays missing, never carried. The nightly ingest loads the TRI of the night's
session the same way (`nightly_tri`), and `verify-tri` checks the stored TRI against niftyindices'
answers kept on disk under `raw_tri/`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..errors import HatError
from ..money import exact
from .bhavcopy import BROWSER_HEADERS, Fetcher, FetchResult
from .mcp_client import CODE_NO_DATA, McpClient, McpError, parse_data_date
from .pacing import Pacer
from .store import (
    DATASET_BARS,
    DATASET_INDEX,
    DataStore,
    IndexRow,
    StoreError,
    atomic_replace,
    keep_once,
)
from .store_coverage import index_coverage

logger = logging.getLogger(__name__)

NIFTY_50 = "NIFTY 50"
NIFTY_500 = "NIFTY 500"
INDIA_VIX = "INDIA VIX"
NIFTY_SMALLCAP_250 = "NIFTY SMALLCAP 250"
INDEX_NAMES = (NIFTY_50, NIFTY_500, NIFTY_SMALLCAP_250, INDIA_VIX)
MCP_HISTORY_MONTHS = 12
NIFTY_500_TRI = "NIFTY 500 TRI"
# [A] NSE's daily index archive; the MCP keeps only about two years (HAT-DATA-095).
INDEX_ARCHIVE_URL = "https://nsearchives.nseindia.com/content/indices/"
# [A] niftyindices' copy of the same files (byte-identical where both have a day), which also
# holds days NSE's archive answers 404 for; a file it lacks comes back as an HTML page.
INDEX_MIRROR_URL = "https://www.niftyindices.com/Daily_Snapshot/"
# [A] niftyindices' total-return series: a JSON list of {"Date": "09 Oct 2013",
# "TotalReturnsIndex": "6419.91", ...} for a date range, no login. Each TRI by its price index.
TRI_URL = "https://www.niftyindices.com/Backpage/getTotalReturnIndexString"
TRI_INDICES = {NIFTY_500_TRI: NIFTY_500}
# [A] niftyindices' price history, the same request: a JSON list of {"HistoricalDate":
# "15 Dec 2014", "OPEN": .., "HIGH": .., "LOW": .., "CLOSE": ..}. It has regular sessions that
# neither day-file source holds (2014-12-15, 2015-03-12, 2015-07-08); India VIX is not there.
HISTORY_URL = "https://www.niftyindices.com/Backpage/getHistoricaldatatabletoString"
HISTORY_INDICES = {n: n for n in (NIFTY_50, NIFTY_500, NIFTY_SMALLCAP_250)}
# [A] Each index's first day in the archive (the first row of the first file holding it, read
# from the stored archive on 2026-10-23): no source reaches further back, so coverage starts here.
INDEX_STARTS = {
    NIFTY_50: date(2012, 2, 21),
    NIFTY_500: date(2012, 2, 21),
    NIFTY_SMALLCAP_250: date(2019, 1, 14),
    INDIA_VIX: date(2014, 5, 14),
    NIFTY_500_TRI: date(2012, 2, 21),  # from its price index's first day, as the regime reads it
}
# [A] Names the archive used before NSE's 2015 renaming; read as today's names.
INDEX_ALIASES = {
    "CNX NIFTY": NIFTY_50,
    "S&P CNX NIFTY": NIFTY_50,
    "CNX 500": NIFTY_500,
    "S&P CNX 500": NIFTY_500,
}


@dataclass(frozen=True)
class IndexPoint:
    day: date
    row: IndexRow


IndexSource = Callable[[str, date, date], Iterable[IndexPoint]]


def index_point_from_mcp(name: str, row: dict) -> IndexPoint | None:
    """One `get_index_history` row; None when a value or the date is missing."""
    day = parse_data_date(row.get("date"))
    values = [
        exact(row.get(k), floats=True, grouped=True) for k in ("open", "high", "low", "close")
    ]
    if day is None or any(v is None for v in values):
        return None
    o, h, low, c = values
    assert o is not None
    assert h is not None
    assert low is not None
    assert c is not None
    return IndexPoint(day, IndexRow(name.upper(), o, h, low, c))


def _chunks(client: McpClient, name: str, start: date, end: date) -> Iterator[list[dict]]:
    """The `get_index_history` chunks (12 months each) back from `end` to `start`, newest first.
    A "no data" answer is the server's depth and ends them; any other failure raises."""
    cursor: date | None = end
    while cursor is not None and cursor >= start:
        try:
            rows, nxt = client.index_history(name, cursor, MCP_HISTORY_MONTHS)
        except McpError as exc:
            if exc.code == CODE_NO_DATA:
                return
            raise
        yield rows
        cursor = nxt if nxt is not None and nxt < cursor else None


def mcp_index_source(client: McpClient) -> IndexSource:
    """Chain `get_index_history` chunks back from `end` to `start` (`_chunks`), keeping the
    readable rows inside the range."""

    def source(name: str, start: date, end: date) -> Iterable[IndexPoint]:
        points = (
            index_point_from_mcp(name, row)
            for rows in _chunks(client, name, start, end)
            for row in rows
        )
        for point in points:
            if point is not None and start <= point.day <= end:
                yield point

    return source


def parse_index_archive_csv(
    text: str, day: date, names: Collection[str] = INDEX_NAMES
) -> list[IndexPoint]:
    """NSE's `ind_close_all_DDMMYYYY.csv` rows for the wanted indices. A line wider than the
    header (an HTML page served in place of a missing file) keeps only its named fields."""
    wanted = {n.upper() for n in names}
    points = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {
            k.strip().lower(): v.strip() if isinstance(v, str) else ""
            for k, v in raw.items()
            if k is not None
        }
        name = row.get("index name", "").upper()
        name = INDEX_ALIASES.get(name, name)
        if name not in wanted:
            continue
        values = [
            exact(row.get(k), floats=True, grouped=True)
            for k in (
                "open index value",
                "high index value",
                "low index value",
                "closing index value",
            )
        ]
        if any(v is None for v in values):
            continue
        o, h, low, c = values
        assert o is not None
        assert h is not None
        assert low is not None
        assert c is not None
        points.append(IndexPoint(day, IndexRow(name, o, h, low, c)))
    return points


CODE_INDEX_ARCHIVE = "index_archive_error"


class IndexArchiveError(HatError):
    """An index archive file could not be fetched or held no index row; raised after the rows
    that were available, so the index keeps them and is reported failed."""

    code = CODE_INDEX_ARCHIVE


def index_archive_name(day: date) -> str:
    return f"ind_close_all_{day:%d%m%Y}.csv"


@dataclass
class NotFoundMemo:
    """The days NSE's index archive answered 404 for, kept beside the build state so a resume
    does not ask again (like the bhavcopy candidates in `BuildState`)."""

    path: Path
    days: set[date] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> NotFoundMemo:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(path, {date.fromisoformat(d) for d in raw.get("not_found", [])})
        except (OSError, ValueError, AttributeError, TypeError):  # unreadable: ask again
            return cls(path)

    def add(self, day: date) -> None:
        self.days.add(day)
        payload = json.dumps({"not_found": sorted(d.isoformat() for d in self.days)})
        atomic_replace(self.path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))


class ArchiveIndexSource:
    """Index rows from NSE's daily `ind_close_all_DDMMYYYY.csv`, for the days that have bars.
    One file serves every index: it is fetched at most once per run, on the bhavcopy
    downloader's pacer (the same host), from NSE's archive or, when that has none, from
    niftyindices' copy, and kept untouched under `raw/` so a later run reads it
    from disk. A day neither has a file for stays missing, and is remembered in `memo` so a resume
    does not ask again; it and a file without the index's row are that index's `absent` days
    (the coverage reports them apart). A failed fetch, or an answer with no index row (never
    kept, so the next run asks again), fails the indices that needed it. An index not in `names`
    has no rows here."""

    def __init__(
        self,
        store: DataStore,
        fetch: Fetcher,
        pacer: Pacer,
        names: Collection[str] = INDEX_NAMES,
        memo: NotFoundMemo | None = None,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._pacer = pacer
        self._names = names
        self._memo = memo
        self._points: dict[date, list[IndexPoint]] = {}
        self._bar_days: list[date] | None = None
        self.requested: list[date] = []
        self.not_found: list[date] = []
        self.errors: list[date] = []
        self.absent: dict[str, set[date]] = defaultdict(set)

    def __call__(self, name: str, start: date, end: date) -> Iterable[IndexPoint]:
        wanted = name.upper()
        if wanted not in {n.upper() for n in self._names}:
            return  # an index the files do not hold (a TRI) has no rows here
        if self._bar_days is None:
            self._bar_days = self._store.days(DATASET_BARS)
        days = [d for d in self._bar_days if start <= d <= end]
        for day in days:
            points = [p for p in self._day(day) if p.row.index_name == wanted]
            if not points and day not in self.errors:
                self.absent[wanted].add(day)
            yield from points
        if any(day in self.errors for day in days):
            raise IndexArchiveError(CODE_INDEX_ARCHIVE)

    def _day(self, day: date) -> list[IndexPoint]:
        if day not in self._points:
            self._points[day] = self._read(day)
        return self._points[day]

    def _read(self, day: date) -> list[IndexPoint]:
        name = index_archive_name(day)
        if self._store.raw_exists(name):
            return self._parse(self._store.read_raw(name), day)
        if self._memo is not None and day in self._memo.days:
            self.not_found.append(day)
            return []
        self._pacer.wait()
        self.requested.append(day)
        result = self._fetch(INDEX_ARCHIVE_URL + name)
        if result.status == "not_found":
            self._pacer.wait()
            result = self._fetch(INDEX_MIRROR_URL + name)
            if result.status == "ok" and not self._parse(result.data, day):
                result = FetchResult("not_found")  # the mirror's page for a file it lacks
        if result.status == "not_found":
            self.not_found.append(day)
            if self._memo is not None:
                self._memo.add(day)
            return []
        points = self._parse(result.data, day) if result.status == "ok" else []
        if not points:  # an error, or an answer that is no index file (a block page, a cut body)
            self.errors.append(day)
            return []
        self._store.store_raw(name, result.data)
        return points

    def _parse(self, data: bytes, day: date) -> list[IndexPoint]:
        return parse_index_archive_csv(data.decode("utf-8-sig", errors="replace"), day, self._names)


def archive_fill_source(primary: IndexSource, archive: IndexSource) -> IndexSource:
    """The primary source (the MCP) first; the archive fills the days before its first row, or
    the whole range when it has none. A failure of either is raised after every row both could
    give (the primary's first), so the index keeps them and is still reported failed."""

    def source(name: str, start: date, end: date) -> Iterable[IndexPoint]:
        points: list[IndexPoint] = []
        failure: Exception | None = None
        try:
            points.extend(primary(name, start, end))  # keeps the rows taken before a failure
        except Exception as exc:  # re-raised below, after the archive fill
            failure = exc
        yield from points
        first = min((p.day for p in points), default=end + timedelta(days=1))
        if first > start:
            try:
                yield from archive(name, start, first - timedelta(days=1))
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    return source


@dataclass
class IndexLoadReport:
    days_written: int = 0
    points: int = 0
    failed: dict[str, str] = field(default_factory=dict)  # index -> stable code


def load_index_history(
    store: DataStore,
    source: IndexSource,
    start: date,
    end: date,
    names: Sequence[str] = INDEX_NAMES,
    starts: Mapping[str, date] | None = None,
) -> IndexLoadReport:
    """Pull every index from its own start (`starts`, never before `start`), then write each day
    once with all its indices (idempotent). An index whose source fails is recorded with a
    stable code and keeps the rows it already gave; the other indices still load."""
    by_day: dict[date, dict[str, IndexRow]] = defaultdict(dict)
    report = IndexLoadReport()
    for name in names:
        first = max(start, (starts or {}).get(name, start))
        if first > end:
            continue
        try:
            for point in source(name, first, end):
                by_day[point.day][point.row.index_name] = point.row
        except Exception as exc:  # one index never stops the others, validation or the report
            code = getattr(exc, "code", None)
            report.failed[name] = code if isinstance(code, str) else "index_source_failed"
            logger.warning("index history %s failed: %s", name, report.failed[name])
    for day in sorted(by_day):
        store.upsert_index(day, list(by_day[day].values()))
        report.days_written += 1
        report.points += len(by_day[day])
    return report


Poster = Callable[[str, bytes], FetchResult]


def http_poster(timeout: float = 60.0) -> Poster:  # pragma: no cover - the real network
    """The operator's JSON POST: browser-like headers, one request per call. Never used by tests."""

    def post(url: str, body: bytes) -> FetchResult:
        import requests  # deferred: only a real load needs the HTTP stack

        headers = {**BROWSER_HEADERS, "Content-Type": "application/json; charset=UTF-8"}
        try:
            response = requests.post(url, data=body, headers=headers, timeout=timeout)
        except requests.RequestException:
            return FetchResult("error")
        if response.status_code != 200 or not response.content:
            return FetchResult("error")
        return FetchResult("ok", response.content)

    return post


def tri_request(index: str, start: date, end: date) -> bytes:
    """The body of a `TRI_URL` or `HISTORY_URL` request for `index` from `start` to `end`."""
    cinfo = (
        f"{{'name':'{index}','startDate':'{start:%d-%b-%Y}',"
        f"'endDate':'{end:%d-%b-%Y}','indexName':'{index}'}}"
    )
    return json.dumps({"cinfo": cinfo}).encode()


def parse_tri_json(data: bytes, name: str) -> list[IndexPoint] | None:
    """A `TRI_URL` answer as points of `name`, the close in all four columns (the series has a
    close only); a row without a date or a positive value is skipped. None when the answer is no
    JSON list (a login or error page)."""
    try:
        rows = json.loads(data.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("Date", "")).strip(), "%d %b %Y").date()
        except ValueError:
            continue
        close = exact(row.get("TotalReturnsIndex"), floats=True, positive=True, grouped=True)
        if close is not None:
            points.append(IndexPoint(day, IndexRow(name, close, close, close, close)))
    return points


def parse_history_json(data: bytes, name: str) -> list[IndexPoint] | None:
    """A `HISTORY_URL` answer as points of `name`; a row without a date or with a missing or
    non-positive level is skipped. None when the answer is no JSON list."""
    try:
        rows = json.loads(data.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("HistoricalDate", "")).strip(), "%d %b %Y").date()
        except ValueError:
            continue
        levels = [
            exact(row.get(k), floats=True, positive=True, grouped=True)
            for k in ("OPEN", "HIGH", "LOW", "CLOSE")
        ]
        o, h, low, c = levels
        if o is not None and h is not None and low is not None and c is not None:
            points.append(IndexPoint(day, IndexRow(name, o, h, low, c)))
    return points


Parser = Callable[[bytes, str], list[IndexPoint] | None]


class NiftyIndicesSource:
    """The indices of `names` (each by niftyindices' name) from the niftyindices JSON series at
    `url`, one request per calendar year of the range on `pacer`; any other index has no rows
    here. A failed request or an answer that is no series fails the index after the rows
    already given."""

    def __init__(
        self, post: Poster, pacer: Pacer, url: str, names: Mapping[str, str], parse: Parser
    ) -> None:
        self._post = post
        self._pacer = pacer
        self._url = url
        self._names = names
        self._parse = parse

    def __call__(self, name: str, start: date, end: date) -> Iterable[IndexPoint]:
        base = self._names.get(name.upper())
        cursor = start
        while base is not None and cursor <= end:
            stop = min(end, date(cursor.year, 12, 31))
            self._pacer.wait()
            result = self._post(self._url, tri_request(base, cursor, stop))
            points = self._parse(result.data, name.upper()) if result.status == "ok" else None
            if points is None:
                raise IndexArchiveError(CODE_INDEX_ARCHIVE)
            yield from (p for p in points if cursor <= p.day <= stop)
            cursor = stop + timedelta(days=1)


def tri_source(post: Poster, pacer: Pacer) -> NiftyIndicesSource:
    """The TRI series (`TRI_INDICES`) from niftyindices, one request per calendar year."""
    return NiftyIndicesSource(post, pacer, TRI_URL, TRI_INDICES, parse_tri_json)


BACKFILL_NAMES = (*INDEX_NAMES, NIFTY_500_TRI)
# Seconds between niftyindices requests: the backfill's default and the nightly TRI's pace.
NIFTY_INDICES_INTERVAL = 2.0


def only_days(source: IndexSource, days: Mapping[str, Collection[date]]) -> IndexSource:
    """`source` asked only for each index's `days`, one call per calendar year holding any, and
    giving only their points."""

    def wrapped(name: str, start: date, end: date) -> Iterable[IndexPoint]:
        wanted = {d for d in days.get(name, ()) if start <= d <= end}
        for year in sorted({d.year for d in wanted}):
            in_year = sorted(d for d in wanted if d.year == year)
            yield from (p for p in source(name, in_year[0], in_year[-1]) if p.day in wanted)

    return wrapped


@dataclass
class BackfillReport:
    missing: dict[str, list[date]]  # the bar days each index had no row for
    found: dict[str, list[IndexPoint]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # index -> stable code
    left: dict[str, list[date]] = field(default_factory=dict)  # still no row: never carried


def missing_index_days(
    store: DataStore, start: date, end: date, names: Sequence[str] = BACKFILL_NAMES
) -> dict[str, list[date]]:
    """Per index, the bar days from its start (`INDEX_STARTS`) in [start, end] with no row."""
    bar_days = [d for d in store.days(DATASET_BARS) if start <= d <= end]
    starts = {name: max(start, INDEX_STARTS.get(name, start)) for name in names}
    return {name: days for name, days in index_coverage(store, bar_days, starts).items() if days}


def backfill(
    store: DataStore,
    sources: Sequence[IndexSource],
    start: date,
    end: date,
    *,
    apply: bool,
    names: Sequence[str] = BACKFILL_NAMES,
) -> BackfillReport:
    """Fill each index's missing bar days from `sources` in turn, each asked only for the days
    the ones before did not give (`only_days`): without `apply` only read what it would write;
    with it, merge them into each day as `load_index_history` does. The days no source has stay
    missing (`left`): an index day is never carried forward. A source that fails is reported
    (`failed`) while days are left. Only the indices of `names` are looked at."""
    report = BackfillReport(missing_index_days(store, start, end, names))
    for name, days in report.missing.items():
        found: list[IndexPoint] = []
        for source in sources:
            rest = sorted(set(days) - {p.day for p in found})
            if not rest:
                break
            try:
                found.extend(only_days(source, {name: rest})(name, rest[0], rest[-1]))
            except Exception as exc:  # one index or source never stops the others
                code = getattr(exc, "code", None)
                report.failed[name] = code if isinstance(code, str) else "index_source_failed"
        report.found[name] = found
        if set(days) <= {p.day for p in found}:
            report.failed.pop(name, None)
    if apply:
        for day in sorted({p.day for points in report.found.values() for p in points}):
            store.upsert_index(
                day, [p.row for ps in report.found.values() for p in ps if p.day == day]
            )
        left = missing_index_days(store, start, end, names)
        report.left = {name: left[name] for name in report.missing if name in left}
    else:
        for name, days in report.missing.items():
            have = {p.day for p in report.found.get(name, [])}
            if rest := [d for d in days if d not in have]:
                report.left[name] = rest
    return report


# [A] How far back the nightly TRI step looks for bar days without a TRI close: niftyindices
# may publish a session's TRI after that night's ingest, so the next nights ask for it again.
TRI_LOOKBACK = timedelta(days=14)


def nightly_tri(
    store: DataStore, source: IndexSource, trade_date: date, *, apply: bool
) -> BackfillReport:
    """The nightly ingest's TRI step: `backfill` of `NIFTY_500_TRI` alone over the bar days from
    `TRI_LOOKBACK` before `trade_date` to it, from `source` (`tri_source`). A day niftyindices
    does not have stays missing (`left`), never carried."""
    return backfill(
        store, (source,), trade_date - TRI_LOOKBACK, trade_date, apply=apply, names=(NIFTY_500_TRI,)
    )


# niftyindices' TRI answers as fetched by `verify-tri --fetch`, under HAT_DATA_ROOT, one file per
# request: `<index>_<first day>_<last day>.json`, a calendar year (to the day of the fetch for
# the current year). A kept file is never rewritten.
TRI_RAW_DIR = "raw_tri"


def tri_raw_name(name: str, start: date, end: date) -> str:
    return f"{name.replace(' ', '_')}_{start:%Y%m%d}_{end:%Y%m%d}.json"


def tri_raw_range(name: str, year: int, today: date) -> tuple[date, date] | None:
    """The days a `verify-tri` request asks for in `year`: the calendar year from the index's
    start, cut at `today`; None when the year holds none of them."""
    start = max(date(year, 1, 1), INDEX_STARTS.get(name, date(year, 1, 1)))
    end = min(date(year, 12, 31), today)
    return (start, end) if start <= end else None


def latest_tri_raw(folder: Path, name: str, year: int) -> Path | None:
    """The kept answer for `year` that reaches furthest (its last day ends the name)."""
    files = folder.glob(f"{name.replace(' ', '_')}_{year}????_{year}????.json")
    return max(files, key=lambda p: p.stem.rsplit("_", 1)[-1], default=None)


def _tri_closes(data: bytes) -> dict[date, Decimal] | None:
    points = parse_tri_json(data, NIFTY_500_TRI)
    return None if points is None else {p.day: Decimal(p.row.close) for p in points}


def fetch_tri_raw(
    folder: Path, post: Poster, pacer: Pacer, years: Iterable[int], today: date
) -> dict[int, str]:
    """Ask niftyindices for each year's `NIFTY_500_TRI` (`tri_raw_range`) and keep the answer
    untouched under `folder`. Per year: "saved"; "kept" (a file with the same closes was there:
    each answer carries its own request number); "changed" (one with other closes is kept under
    the name, and stays); "failed" (no answer, or one that is no series: nothing kept)."""
    status: dict[int, str] = {}
    for year in years:
        days = tri_raw_range(NIFTY_500_TRI, year, today)
        if days is None:
            continue
        pacer.wait()
        result = post(TRI_URL, tri_request(TRI_INDICES[NIFTY_500_TRI], *days))
        if result.status != "ok" or parse_tri_json(result.data, NIFTY_500_TRI) is None:
            status[year] = "failed"
            continue
        path = folder / tri_raw_name(NIFTY_500_TRI, *days)
        try:  # create-only: a file another fetch kept first is never replaced
            status[year] = "saved" if keep_once(path, result.data) else "kept"
        except StoreError:
            same = _tri_closes(path.read_bytes()) == _tri_closes(result.data)
            status[year] = "kept" if same else "changed"
    return status


@dataclass
class TriCheck:
    compared: int = 0
    mismatched: list[tuple[date, Decimal, Decimal]] = field(default_factory=list)  # stored, raw
    not_in_raw: list[date] = field(default_factory=list)  # stored, absent from the raw files
    not_stored: list[date] = field(default_factory=list)  # in the raw files, absent from the store
    no_file: list[int] = field(default_factory=list)  # years with no kept answer

    @property
    def clean(self) -> bool:
        return not (self.mismatched or self.not_in_raw or self.not_stored or self.no_file)


def compare_tri(
    stored: Mapping[date, Decimal], raw: Mapping[date, Decimal], start: date, end: date
) -> TriCheck:
    """Every stored close in [start, end] against the raw close of the same day (exact decimal
    equality), and the raw days in the range the store lacks."""
    check = TriCheck()
    for day in sorted(d for d in stored if start <= d <= end):
        if day not in raw:
            check.not_in_raw.append(day)
            continue
        check.compared += 1
        if stored[day] != raw[day]:
            check.mismatched.append((day, stored[day], raw[day]))
    check.not_stored = sorted(d for d in raw if start <= d <= end and d not in stored)
    return check


def verify_tri(store: DataStore, folder: Path, start: date, end: date, today: date) -> TriCheck:
    """Compare the stored `NIFTY_500_TRI` closes in [start, end] with the kept raw answers
    (`latest_tri_raw` per year). Reads only."""
    stored: dict[date, Decimal] = {}
    for day in store.days(DATASET_INDEX):
        if start <= day <= end:
            for row in store.read_index(day):
                if row.index_name == NIFTY_500_TRI:
                    stored[day] = Decimal(row.close)
    raw: dict[date, Decimal] = {}
    no_file = []
    for year in range(start.year, end.year + 1):
        if tri_raw_range(NIFTY_500_TRI, year, today) is None:
            continue
        path = latest_tri_raw(folder, NIFTY_500_TRI, year)
        points = parse_tri_json(path.read_bytes(), NIFTY_500_TRI) if path is not None else None
        if points is None:
            no_file.append(year)
            continue
        raw.update((p.day, Decimal(p.row.close)) for p in points)
    check = compare_tri(stored, raw, start, end)
    check.no_file = no_file
    return check


def _print_tri_check(
    check: TriCheck, fetched: Mapping[int, str], start: date, end: date
) -> None:  # pragma: no cover - output
    out = sys.stdout
    out.write(f"verify-tri {NIFTY_500_TRI} {start}..{end}\n")
    for year, status in sorted(fetched.items()):
        out.write(f"  fetch {year}: {status}\n")
    out.write(
        f"compared {check.compared}, mismatched {len(check.mismatched)}, "
        f"stored not in raw {len(check.not_in_raw)}, raw not stored {len(check.not_stored)}, "
        f"years without a raw file {len(check.no_file)}\n"
    )
    for day, stored, raw in check.mismatched[:40]:
        out.write(f"  mismatch {day} stored {stored} raw {raw}\n")
    for label, days in (
        ("stored not in raw", check.not_in_raw),
        ("raw not stored", check.not_stored),
    ):
        if days:
            out.write(f"  {label} ({len(days)}): {', '.join(str(d) for d in days[:40])}\n")
    if check.no_file:
        out.write(f"  no raw file: {', '.join(str(y) for y in check.no_file)}\n")


def _print_backfill(report: BackfillReport, apply: bool) -> None:  # pragma: no cover - output
    out = sys.stdout
    out.write(f"index backfill ({'applied' if apply else 'dry run'})\n")
    if not report.missing:
        out.write("no bar day lacks an index row\n")
    for name, days in report.missing.items():
        found = sorted(report.found.get(name, []), key=lambda p: p.day)
        out.write(f"{name}: missing {len(days)}, found {len(found)}")
        if name in report.failed:
            out.write(f", failed {report.failed[name]}")
        out.write("\n")
        if len(found) <= 40:
            for p in found:
                r = p.row
                out.write(f"  {p.day} {p.day:%a} O {r.open} H {r.high} L {r.low} C {r.close}\n")
        else:
            out.write(
                f"  {found[0].day} .. {found[-1].day} (first C {found[0].row.close}, "
                f"last C {found[-1].row.close})\n"
            )
        if left := report.left.get(name):
            out.write(f"  still missing {len(left)}: {', '.join(str(d) for d in left)}\n")


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - the network
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.index_history")
    commands = parser.add_subparsers(dest="command", required=True)
    fill = commands.add_parser("backfill", help="fill the bar days an index has no row for")
    fill.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2011, 1, 1))
    fill.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    fill.add_argument("--apply", action="store_true")
    night = commands.add_parser("nightly-tri", help="the nightly ingest's TRI step for one day")
    night.add_argument("--day", type=date.fromisoformat, required=True)
    night.add_argument("--apply", action="store_true")
    check = commands.add_parser("verify-tri", help="the stored TRI against niftyindices' files")
    check.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    check.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    check.add_argument("--fetch", action="store_true", help="first keep each year's answer")
    for sub in (fill, night, check):
        sub.add_argument(
            "--interval",
            type=float,
            default=NIFTY_INDICES_INTERVAL,
            help="seconds between requests",
        )
    args = parser.parse_args(argv)
    from ..settings import operator_settings
    from .bhavcopy import http_fetcher

    logging.basicConfig(level=logging.INFO)
    settings = operator_settings()
    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    pacer = Pacer(min_interval=args.interval)
    post = http_poster()
    if args.command == "nightly-tri":
        report = nightly_tri(store, tri_source(post, pacer), args.day, apply=args.apply)
        _print_backfill(report, args.apply)
        return 1 if report.failed or report.left else 0
    if args.command == "verify-tri":
        folder = store.root / TRI_RAW_DIR
        today = date.today()
        years = range(args.start.year, args.end.year + 1)
        fetched = fetch_tri_raw(folder, post, pacer, years, today) if args.fetch else {}
        result = verify_tri(store, folder, args.start, args.end, today)
        _print_tri_check(result, fetched, args.start, args.end)
        return 0 if result.clean and set(fetched.values()) <= {"saved", "kept"} else 1
    archive = ArchiveIndexSource(store, http_fetcher(), pacer, names=INDEX_NAMES)
    history = NiftyIndicesSource(post, pacer, HISTORY_URL, HISTORY_INDICES, parse_history_json)
    end = args.end or date.today()
    sources = (archive, tri_source(post, pacer), history)
    report = backfill(store, sources, args.start, end, apply=args.apply)
    _print_backfill(report, args.apply)
    return 1 if report.failed or report.left else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
