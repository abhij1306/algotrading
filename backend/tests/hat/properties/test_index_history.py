"""The index sources' pure parts: niftyindices' TRI answer and request, and the archive's
fallback to niftyindices' copy of a day file NSE's archive lacks (a fake fetcher; no network)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.hat.data.bhavcopy import FetchResult
from app.hat.data.index_history import (
    INDEX_ARCHIVE_URL,
    INDEX_MIRROR_URL,
    NIFTY_500,
    NIFTY_500_TRI,
    ArchiveIndexSource,
    parse_tri_json,
    tri_request,
)
from app.hat.data.pacing import Pacer
from app.hat.data.store import DataStore

DAY = date(2013, 10, 9)
CSV = (
    b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
    b"Closing Index Value\nCNX 500,09-10-2013,4512.3,4596.75,4505.75,4592.45\n"
)


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
