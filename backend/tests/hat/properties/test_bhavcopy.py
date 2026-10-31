"""Bhavcopy file names by date, the two parsers agreeing, the downloader's pacing and the
traded value kept, on the real code with in-memory zips and fake fetchers (no network)."""

import dataclasses
import io
import tempfile
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from hat_bhav_support import bar, bhav_rows, render_old, render_udiff
from hypothesis import assume, example, given
from hypothesis import strategies as st

from app.hat.clock import FixedClock
from app.hat.data.bhavcopy import (
    FIRST_ISIN_DAY,
    BhavcopyError,
    BhavRow,
    BuildState,
    Downloader,
    FetchResult,
    _old_date,
    archive_name,
    archive_url,
    parse_bhavcopy,
    to_bar_rows,
    traded_isins,
    weekdays,
)
from app.hat.data.pacing import Pacer
from app.hat.data.store import DataStore
from app.hat.ids import uuid7

OLD_DAYS = st.dates(FIRST_ISIN_DAY, date(2024, 7, 5)).filter(lambda d: d.weekday() < 5)
NEW_DAYS = st.dates(date(2024, 7, 8), date(2026, 10, 21)).filter(lambda d: d.weekday() < 5)
ANY_WEEKDAY = st.one_of(OLD_DAYS, NEW_DAYS)


@example(day=date(2024, 7, 5))
@example(day=date(2024, 7, 8))
@given(day=ANY_WEEKDAY)
def test_format_by_date(day: date) -> None:
    """Every weekday from FIRST_ISIN_DAY to today gets the old URL up to 2024-07-05 and the UDiFF
    URL from 2024-07-08, with the date fields written as in table."""
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


def test_weekend_session_has_a_file_but_the_format_gap_has_none() -> None:
    assert archive_name(date(2013, 11, 3)) == "cm03NOV2013bhav.csv.zip"
    assert archive_name(date(2025, 2, 1)) == "BhavCopy_NSE_CM_0_0_0_20250201_F_0000.csv.zip"
    try:
        archive_url(date(2024, 7, 6))
    except BhavcopyError as exc:
        assert exc.code == "bhavcopy_format_gap"
    else:
        raise AssertionError("the weekend between the formats got a URL")


@given(day=st.dates(date(2010, 1, 1), FIRST_ISIN_DAY - timedelta(days=1)))
def test_no_file_before_first_isin_day(day: date) -> None:
    """A weekday before FIRST_ISIN_DAY has no usable file (no ISIN column), so no name."""
    try:
        archive_name(day)
    except BhavcopyError as exc:
        assert exc.code == "bhavcopy_no_isin"
    else:
        raise AssertionError("a day before FIRST_ISIN_DAY got a file name")


@given(
    n=st.integers(1, 12),
    interval=st.integers(1, 5000),
    durations=st.lists(st.integers(0, 8000), min_size=12, max_size=12),
)
def test_rate_limited(n: int, interval: int, durations: list[int]) -> None:
    """Whatever each request's duration, consecutive requests start at least `interval` apart."""
    clock = FixedClock(datetime(2024, 1, 1, tzinfo=UTC))
    starts: list[datetime] = []
    days = weekdays(date(2024, 1, 1), date(2024, 1, 31))[:n]

    def fetch(url: str) -> FetchResult:
        starts.append(clock.now())
        clock.advance(timedelta(seconds=durations[len(starts) - 1]))
        return FetchResult("not_found")

    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        pacer = Pacer(
            min_interval=interval,
            clock=clock,
            sleep=lambda s: clock.advance(timedelta(seconds=s)),
        )
        Downloader(store, fetch, BuildState.load(store.root / "state.json"), pacer).run(days)
    assert len(starts) == n
    gap = timedelta(seconds=interval)
    assert all(b - a >= gap for a, b in zip(starts, starts[1:], strict=False))


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
    ids = {r.isin: uuid7() for r in parsed.rows}
    bars, unresolved = to_bar_rows(parsed, ids.get, uuid7())
    assert unresolved == []
    assert [b.traded_value for b in bars] == [r.traded_value for r in data]
    assert all(b.source == "bhavcopy" for b in bars)
    assert bar(uuid7(), uuid7(), source="mcp", traded=None).traded_value is None


def test_old_date_two_digit_year() -> None:
    """The 13-JUL-2020 file writes "13-Jul-20"; it is the same day as "13-JUL-2020"."""
    assert _old_date("13-Jul-20") == _old_date("13-JUL-2020") == date(2020, 7, 13)


def _csv_zip(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("bhav.csv", text)
    return buffer.getvalue()


def test_traded_isins_reads_every_series_of_either_format() -> None:
    old = _csv_zip(
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
        "TOTALTRADES,ISIN,\n"
        "KOVAI,BE,170,171.5,158,158.95,158,165.65,1437,233751.8,28-FEB-2013,48,INE177F01017,\n"
        "KRITIIND,EQ,5.5,5.5,5.5,5.5,5.5,5.25,200,1100,28-FEB-2013,1,INE479D01038,\n"
        "SOMEBOND,N1,100,100,100,100,100,100,1,100,28-FEB-2013,1,INE002A07015,\n"
        "BROKEN,EQ,1,1,1,1,1,1,1,1,28-FEB-2013,1,,\n"
    )
    assert traded_isins(old) == {"INE177F01017", "INE479D01038", "INE002A07015"}
    udiff = _csv_zip(
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,ClsPric\n"
        "2024-10-01,2024-10-01,CM,NSE,STK,6417,INE479D01038,KRITI,EQ,237.78\n"
        "2024-10-01,2024-10-01,CM,NSE,STK,2885,ine002a01018,RELIANCE,EQ,2929.65\n"
    )
    assert traded_isins(udiff) == {"INE479D01038", "INE002A01018"}
    with pytest.raises(BhavcopyError):
        traded_isins(_csv_zip("SYMBOL,SERIES\nKRITI,EQ\n"))
