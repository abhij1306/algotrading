"""PBT bhavcopy.*: file names, the two parsers, the
resumable polite downloader, day reconciliation and the validation report, on the real code with
in-memory zips and fake fetchers (no network)."""

import dataclasses
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from hat_bhav_support import bar, bhav_rows, render_old, render_udiff
from hypothesis import assume, example, given, settings
from hypothesis import strategies as st

from app.hat.data.bhavcopy import (
    BhavcopyError,
    BhavRow,
    BuildState,
    Downloader,
    FetchResult,
    archive_name,
    archive_url,
    parse_bhavcopy,
    reconcile_days,
    to_bar_rows,
    weekdays,
)
from app.hat.data.history_build import validate_build
from app.hat.data.store import DataStore
from app.hat.ids import uuid7

OLD_DAYS = st.dates(date(2010, 1, 1), date(2024, 7, 5)).filter(lambda d: d.weekday() < 5)
NEW_DAYS = st.dates(date(2024, 7, 8), date(2026, 10, 21)).filter(lambda d: d.weekday() < 5)
ANY_WEEKDAY = st.one_of(OLD_DAYS, NEW_DAYS)


@example(day=date(2024, 7, 5))
@example(day=date(2024, 7, 8))
@given(day=ANY_WEEKDAY)
def test_format_by_date(day: date) -> None:
    """Every weekday from 2010-01-01 to today gets the old URL up to 2024-07-05 and the UDiFF URL
    from 2024-07-08, with the date fields written as in table."""
    url = archive_url(day)
    if day <= date(2024, 7, 5):
        month = day.strftime("%b").upper()
        assert url == (
            "https://nsearchives.nseindia.com/content/historical/EQUITIES/"
            f"{day.year}/{month}/cm{day.day:02d}{month}{day.year}bhav.csv.zip"
        )
    else:
        assert url == (
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
        )
    assert archive_name(day) == url.rsplit("/", 1)[1]


@given(day=st.dates(date(2010, 1, 1), date(2026, 10, 21)).filter(lambda d: d.weekday() >= 5))
def test_format_by_date_weekend_has_no_file(day: date) -> None:
    try:
        archive_url(day)
    except BhavcopyError as exc:
        assert exc.code == "bhavcopy_not_weekday"
    else:
        raise AssertionError("a weekend got a URL")


STATUSES = st.sampled_from(["ok", "ok", "not_found", "error"])


@st.composite
def outcomes(draw: st.DrawFn) -> tuple[list[date], dict[date, str], set[date]]:
    start = draw(st.dates(date(2012, 1, 2), date(2024, 6, 1)))
    days = weekdays(start, start + timedelta(days=draw(st.integers(0, 40))))
    status = {d: draw(STATUSES) for d in days}
    holidays = {d for d in days if draw(st.booleans())}
    return days, status, holidays


def _scripted_fetch(status: dict[date, str]) -> Callable[[str], FetchResult]:
    by_url = {archive_url(d): d for d in status}

    def fetch(url: str) -> FetchResult:
        s = status[by_url[url]]
        return FetchResult(s, b"zip-" + url.encode()) if s == "ok" else FetchResult(s)

    return fetch


@given(data=outcomes())
@settings(max_examples=60)
def test_missing_file_is_holiday_candidate(
    data: tuple[list[date], dict[date, str], set[date]],
) -> None:
    """Whatever NSE answers for each weekday, the weekday is downloaded, a holiday candidate that
    the holiday list explains, a candidate it does not (reported as a gap), or unaccounted (a
    transient error); no day is in two buckets and a gap is never a holiday."""
    days, status, holidays = data
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        state = BuildState.load(store.root / "state.json")
        Downloader(store, _scripted_fetch(status), state, interval=0).run(days)
        have = {d for d in days if store.raw_exists(archive_name(d))}
        rec = reconcile_days(days, have, state.candidates, holidays)
    buckets = [rec.downloaded, rec.holidays, rec.gaps, rec.unaccounted]
    assert sorted(d for b in buckets for d in b) == sorted(days)
    assert set(rec.downloaded) == {d for d in days if status[d] == "ok"}
    assert set(rec.holidays) == {d for d in days if status[d] == "not_found" and d in holidays}
    assert set(rec.gaps) == {d for d in days if status[d] == "not_found" and d not in holidays}
    assert set(rec.unaccounted) == {d for d in days if status[d] == "error"}


class CrashError(Exception):
    """An interruption injected into a fetch."""


@given(data=outcomes(), crash_after=st.integers(0, 30))
@settings(max_examples=60)
def test_resume_skips_done(
    data: tuple[list[date], dict[date, str], set[date]], crash_after: int
) -> None:
    """After a run interrupted at any request, `--resume` never requests a day already
    downloaded (or known to have no file) and requests every other day exactly once."""
    days, status, _ = data
    status = {d: s for d, s in status.items() if s != "error"}
    days = list(status)
    first: list[date] = []
    by_url = {archive_url(d): d for d in days}
    scripted = _scripted_fetch(status)

    def crashing(url: str) -> FetchResult:
        if len(first) >= crash_after:
            raise CrashError
        first.append(by_url[url])
        return scripted(url)

    second: list[date] = []

    def recording(url: str) -> FetchResult:
        second.append(by_url[url])
        return scripted(url)

    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        try:
            Downloader(store, crashing, BuildState.load(store.root / "state.json"), interval=0).run(
                days
            )
        except CrashError:
            pass
        Downloader(store, recording, BuildState.load(store.root / "state.json"), interval=0).run(
            days, resume=True
        )
    assert set(first).isdisjoint(second)
    assert sorted(second) == sorted(d for d in days if d not in set(first))
    assert len(second) == len(set(second))


@given(
    n=st.integers(1, 12),
    interval=st.integers(1, 5000),
    durations=st.lists(st.integers(0, 8000), min_size=12, max_size=12),
)
def test_rate_limited(n: int, interval: int, durations: list[int]) -> None:
    """Whatever each request's duration, consecutive requests start at least `interval` apart."""
    now = [0]
    starts: list[int] = []
    days = weekdays(date(2024, 1, 1), date(2024, 1, 31))[:n]

    def fetch(url: str) -> FetchResult:
        starts.append(now[0])
        now[0] += durations[len(starts) - 1]
        return FetchResult("not_found")

    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        Downloader(
            store,
            fetch,
            BuildState.load(store.root / "state.json"),
            interval=interval,
            clock=lambda: now[0],
            sleep=lambda s: now.__setitem__(0, now[0] + s),
        ).run(days)
    assert len(starts) == n
    assert all(b - a >= interval for a, b in zip(starts, starts[1:], strict=False))


def _key(row: BhavRow) -> BhavRow:
    return dataclasses.replace(row, trade_date=date(2000, 1, 1))


@given(rows_old=st.data(), old_day=OLD_DAYS, new_day=NEW_DAYS, extra=st.integers(0, 3))
def test_parsers_agree(rows_old: st.DataObject, old_day: date, new_day: date, extra: int) -> None:
    """The same security rows rendered as an old CM bhavcopy and as a UDiFF file parse to
    identical normalised rows (ISIN, series, symbol, OHLC, prev_close, volume, traded value);
    other series are left out of both and counted."""
    rows = rows_old.draw(bhav_rows(old_day))
    old = parse_bhavcopy(old_day, render_old(old_day, rows, extra))
    new = parse_bhavcopy(
        new_day,
        render_udiff(new_day, [dataclasses.replace(r, trade_date=new_day) for r in rows], extra),
    )
    assert [_key(r) for r in old.rows] == [_key(r) for r in new.rows] == [_key(r) for r in rows]
    assert old.skipped_series == new.skipped_series == extra
    assert old.invalid == new.invalid == old.wrong_date == new.wrong_date == 0


@given(day=OLD_DAYS, other=OLD_DAYS, rows=st.data())
def test_parsers_agree_wrong_date_reported(day: date, other: date, rows: st.DataObject) -> None:
    """A file whose rows carry another day's date yields no rows, only a count (never stored)."""
    assume(day != other)
    data = rows.draw(bhav_rows(day, 1, 4))
    parsed = parse_bhavcopy(other, render_old(day, data))
    assert parsed.rows == []
    assert parsed.wrong_date == len(data)


@given(day=st.one_of(OLD_DAYS, NEW_DAYS), rows=st.data())
def test_traded_value_kept(day: date, rows: st.DataObject) -> None:
    """Each parsed bhavcopy row keeps the traded value of its file (null only when the file has
    none), and it reaches the stored bar; an MCP bar's traded value is null."""
    data = rows.draw(bhav_rows(day))
    blob = render_old(day, data) if day <= date(2024, 7, 5) else render_udiff(day, data)
    parsed = parse_bhavcopy(day, blob)
    assert [r.traded_value for r in parsed.rows] == [r.traded_value for r in data]
    ids = {r.key: uuid7() for r in parsed.rows}
    bars, unresolved = to_bar_rows(parsed, lambda i, s: ids[(i, s)], uuid7())
    assert unresolved == []
    assert [b.traded_value for b in bars] == [r.traded_value for r in data]
    assert all(b.source == "bhavcopy" for b in bars)
    assert bar(uuid7(), uuid7(), source="mcp", traded=None).traded_value is None


@given(
    closes=st.lists(st.decimals("1", "999", places=2), min_size=1, max_size=70),
    wrong=st.sets(st.integers(0, 69), max_size=5),
    missing=st.sets(st.integers(0, 69), max_size=5),
    nudge=st.sampled_from([Decimal("0.01"), Decimal("-0.01"), Decimal("100")]),
    seed=st.integers(0, 3),
)
def test_validation_flags_mismatch(
    closes: list[Decimal], wrong: set[int], missing: set[int], nudge: Decimal, seed: int
) -> None:
    """The build fails iff some sampled close differs from the MCP close (exact), lists the row
    count of every built day, and samples min(50, rows) distinct symbol-days."""
    base = date(2024, 1, 1)
    run = uuid7()
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        days = weekdays(base, base + timedelta(days=200))[: len(closes)]
        for day, close in zip(days, closes, strict=True):
            store.write_bars(day, [bar(uuid7(), run, str(close))])
        order = {d: i for i, d in enumerate(days)}
        asked: list[int] = []

        def theirs(symbol: str, day: date) -> Decimal | None:
            i = order[day]
            asked.append(i)
            if i in missing:
                return None
            truth = closes[i]
            return truth + nudge if i in wrong else truth

        report = validate_build(store, theirs, seed=seed)
    expected = any(i in wrong and i not in missing for i in asked)
    assert report.failed == expected
    assert len(asked) == len(set(asked)) == min(50, len(closes)) == len(report.samples)
    assert sorted(report.row_counts) == days
    assert all(n == 1 for n in report.row_counts.values())
