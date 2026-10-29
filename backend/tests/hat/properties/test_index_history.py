"""The index sources' pure parts: niftyindices' TRI and price-history answers and request, and
the archive's fallback to niftyindices' copy of a day file NSE's archive lacks (a fake fetcher;
no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.data.bhavcopy import FetchResult
from app.hat.data.index_history import (
    INDEX_ARCHIVE_URL,
    INDEX_MIRROR_URL,
    NIFTY_500,
    NIFTY_500_TRI,
    ArchiveIndexSource,
    changed_rows,
    compare_tri,
    fetch_tri_raw,
    latest_tri_raw,
    parse_history_json,
    parse_index_archive_csv,
    parse_tri_json,
    tri_raw_range,
    tri_request,
)
from app.hat.data.pacing import Pacer
from app.hat.data.store import DataStore, IndexRow, StoreError, keep_once

DAY = date(2013, 10, 9)
CSV = (
    b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    b"Closing Index Value\nCNX 500,09-10-2013,4512.3,4596.75,4505.75,4592.45\n"
)


def test_archive_csv_with_an_html_page_has_no_row() -> None:
    """An HTML page served in place of a missing file (lines wider than its first) is no row."""
    page = "<!DOCTYPE html>\n<link href=a,b,c,d rel=x>\n" + CSV.decode()
    assert parse_index_archive_csv(page, DAY) == []
    assert [p.row.close for p in parse_index_archive_csv(CSV.decode(), DAY)] == [Decimal("4592.45")]


def test_parse_tri_json_reads_the_close_only() -> None:
    answer = json.dumps(
        [
            {"Index Name": "Nifty 500", "Date": "09 Oct 2013", "TotalReturnsIndex": "6419.91"},
            {"Index Name": "Nifty 500", "Date": "bad", "TotalReturnsIndex": "1"},
            {"Index Name": "Nifty 500", "Date": "10 Oct 2013", "TotalReturnsIndex": "-"},
        ]
    ).encode()
    points = parse_tri_json(answer, NIFTY_500_TRI)
    assert points is not None
    assert len(points) == 1
    row = points[0].row
    assert points[0].day == DAY
    assert row.index_name == NIFTY_500_TRI
    assert row.open == row.high == row.low == row.close == Decimal("6419.91")
    assert parse_tri_json(b"<html>login</html>", NIFTY_500_TRI) is None
    assert parse_tri_json(b'{"Message": "error"}', NIFTY_500_TRI) is None


def test_parse_history_json_reads_all_four_levels() -> None:
    row = {"INDEX_NAME": "Nifty 500", "HistoricalDate": "15 Dec 2014", "OPEN": "6632.40"}
    answer = json.dumps(
        [
            {**row, "HIGH": "6687.80", "LOW": "6632.40", "CLOSE": "6667.55"},
            {**row, "HIGH": "6687.80", "LOW": "-", "CLOSE": "6667.55"},
            {**row, "HistoricalDate": "", "HIGH": "1", "LOW": "1", "CLOSE": "1"},
        ]
    ).encode()
    points = parse_history_json(answer, NIFTY_500)
    assert points is not None
    assert [(p.day, p.row.open, p.row.close) for p in points] == [
        (date(2014, 12, 15), Decimal("6632.40"), Decimal("6667.55"))
    ]
    assert parse_history_json(b"<html></html>", NIFTY_500) is None


def test_tri_request_names_the_price_index_and_range() -> None:
    body = json.loads(tri_request(NIFTY_500, date(2013, 1, 1), date(2013, 12, 31)))
    assert body == {
        "cinfo": "{'name':'NIFTY 500','startDate':'01-Jan-2013',"
        "'endDate':'31-Dec-2013','indexName':'NIFTY 500'}"
    }


def _archive(tmp_path: Path, answers: dict[str, FetchResult]) -> tuple[ArchiveIndexSource, list]:
    store = DataStore(tmp_path)
    asked: list[str] = []

    def fetch(url: str) -> FetchResult:
        asked.append(url)
        return answers.get(url, FetchResult("not_found"))

    source = ArchiveIndexSource(store, fetch, Pacer(), names=(NIFTY_500,))
    source._bar_days = [DAY]  # the store has no bars; the day under test is a bar day
    return source, asked


def test_archive_falls_back_to_the_mirror(tmp_path: Path) -> None:
    name = "ind_close_all_09102013.csv"
    source, asked = _archive(tmp_path, {INDEX_MIRROR_URL + name: FetchResult("ok", CSV)})
    points = list(source(NIFTY_500, DAY, DAY))
    assert asked == [INDEX_ARCHIVE_URL + name, INDEX_MIRROR_URL + name]
    assert [p.row.close for p in points] == [Decimal("4592.45")]


def test_a_day_neither_has_stays_missing(tmp_path: Path) -> None:
    name = "ind_close_all_09102013.csv"
    page = FetchResult("ok", b"<!DOCTYPE html><html></html>")  # the mirror's answer for no file
    source, _ = _archive(tmp_path, {INDEX_MIRROR_URL + name: page})
    assert list(source(NIFTY_500, DAY, DAY)) == []
    assert source.not_found == [DAY]
    assert source.errors == []
    assert source.absent[NIFTY_500] == {DAY}


def test_compare_tri_reports_each_kind_of_difference() -> None:
    d1, d2, d3, d4 = (date(2026, 10, d) for d in (20, 21, 22, 23))
    stored = {d1: Decimal("100.10"), d2: Decimal("101.0000"), d3: Decimal("102"), DAY: Decimal(1)}
    raw = {d1: Decimal("100.1"), d2: Decimal("101.01"), d4: Decimal("103")}
    check = compare_tri(stored, raw, d1, d4)
    assert check.compared == 2
    assert check.mismatched == [(d2, Decimal("101.0000"), Decimal("101.01"))]
    assert check.not_in_raw == [d3]
    assert check.not_stored == [d4]
    assert not check.clean


def test_tri_raw_range_is_the_year_from_the_index_start_to_today() -> None:
    today = date(2026, 10, 26)
    assert tri_raw_range(NIFTY_500_TRI, 2012, today) == (date(2012, 2, 21), date(2012, 12, 31))
    assert tri_raw_range(NIFTY_500_TRI, 2026, today) == (date(2026, 1, 1), today)
    assert tri_raw_range(NIFTY_500_TRI, 2011, today) is None
    assert tri_raw_range(NIFTY_500_TRI, 2027, today) is None


def test_fetch_tri_raw_keeps_each_answer_once(tmp_path: Path) -> None:
    """A new answer is saved, the same one is kept, a different one never replaces the file, an
    answer that is no series is not kept; the latest kept answer of a year is the one read."""
    row = '{"RequestNumber": "%s", "Date": "02 Jan 2025", "TotalReturnsIndex": "%s"}'
    answers = [
        f"[{row % ('a', '100.5')}]".encode(),
        f"[{row % ('b', '100.50')}]".encode(),  # another request number, the same close
        f"[{row % ('c', '100.6')}]".encode(),
        b"<html>",
        b"[]",
    ]
    asked: list[bytes] = []

    def post(_url: str, body: bytes) -> FetchResult:
        asked.append(body)
        return FetchResult("ok", answers[len(asked) - 1])

    def fetch(year: int, today: date) -> str:
        return fetch_tri_raw(tmp_path, post, Pacer(), [year], today)[year]

    assert [fetch(2025, date(2026, 1, 5)) for _ in range(4)] == [
        "saved",
        "kept",
        "changed",
        "failed",
    ]
    assert fetch(2026, date(2026, 1, 5)) == "saved"
    assert [p.name for p in sorted(tmp_path.iterdir())] == [
        "NIFTY_500_TRI_20250101_20251231.json",
        "NIFTY_500_TRI_20260101_20260105.json",
    ]
    assert (tmp_path / "NIFTY_500_TRI_20250101_20251231.json").read_bytes() == answers[0]
    (tmp_path / "NIFTY_500_TRI_20260101_20260301.json").write_bytes(b"[]")
    latest = latest_tri_raw(tmp_path, NIFTY_500_TRI, 2026)
    assert latest is not None
    assert latest.name == "NIFTY_500_TRI_20260101_20260301.json"
    assert latest_tri_raw(tmp_path, NIFTY_500_TRI, 2024) is None


def test_keep_once_never_replaces_a_file_that_appeared_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fetches racing for one raw name: the one that finds the name taken only after its own
    check (`exists` said no) still cannot overwrite the first's file."""
    path = tmp_path / "NIFTY_500_TRI_20250101_20251231.json"
    path.write_bytes(b"first")
    monkeypatch.setattr(Path, "exists", lambda _self: False)
    with pytest.raises(StoreError):
        keep_once(path, b"second")
    assert path.read_bytes() == b"first"
    assert keep_once(path, b"first") is False
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_reload_rewrites_only_a_stored_index_the_file_gives_otherwise() -> None:
    # NIFTY 50 2025-09-18: the MCP snapshot's high was 25448.95, NSE's file says 25448.55
    stored = {
        "NIFTY 50": IndexRow(
            "NIFTY 50",
            Decimal("25441.05"),
            Decimal("25448.95"),
            Decimal("25329.75"),
            Decimal("25423.6"),
        ),
        "NIFTY 500": IndexRow("NIFTY 500", Decimal(1), Decimal(2), Decimal(1), Decimal("1.50")),
    }
    file_rows = [
        IndexRow(
            "NIFTY 50",
            Decimal("25441.05"),
            Decimal("25448.55"),
            Decimal("25329.75"),
            Decimal("25423.6"),
        ),
        IndexRow("NIFTY 500", Decimal(1), Decimal(2), Decimal(1), Decimal("1.5")),
        IndexRow("INDIA VIX", Decimal(10), Decimal(11), Decimal(9), Decimal(10)),
    ]
    assert changed_rows(stored, file_rows) == [file_rows[0]]
