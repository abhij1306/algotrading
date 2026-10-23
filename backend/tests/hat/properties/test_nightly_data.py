"""PR 72's pure seams: the recorded holiday history, the build's day list, the index coverage
split, the not-found memo, the stop reason of a partial report and the series a nightly bhavcopy
bar carries. The build and the ingest themselves are verified on real data, never here."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.hat.data import calendar as hat_calendar
from app.hat.data.bhavcopy import BhavRow, ParsedDay
from app.hat.data.history_build import build_days, split_absent
from app.hat.data.index_history import NotFoundMemo
from app.hat.data.ingest import _bhav_candidates
from app.hat.data.run_report import stop_reason
from app.hat.data.universe import Member
from app.hat.errors import HatError
from app.hat.risk.sessions import SessionCalendar
from app.hat.time import IST


def test_recorded_history_reads_holidays_and_sessions() -> None:
    days = {d.day: d for d in hat_calendar.recorded_history()}
    assert {d.year for d in days} == set(range(2010, 2026))
    assert not days[date(2010, 1, 26)].is_trading
    # NSE closed on 15 Nov 2013 where BSE closed on the 14th; the archive has a file for the 14th
    assert not days[date(2013, 11, 15)].is_trading
    assert date(2013, 11, 14) not in days
    budget, muhurat = days[date(2025, 2, 1)], days[date(2024, 11, 1)]
    assert budget.is_trading
    assert muhurat.is_trading
    assert (budget.open_at, budget.close_at) == (None, None)


def test_recorded_history_session_wins_over_holiday() -> None:
    text = (
        '{"holidays": {"2020": ["2020-11-14", "2020-11-16"]},'
        ' "sessions": [{"date": "2020-11-14", "note": "Muhurat"}]}'
    )
    days = hat_calendar.parse_recorded_history(text)
    assert [(d.day, d.is_trading, d.note) for d in days] == [
        (date(2020, 11, 14), True, "Muhurat"),
        (date(2020, 11, 16), False, None),
    ]


def test_calendar_holidays_is_a_set() -> None:
    opens = datetime(2025, 2, 1, 9, 15, tzinfo=IST)
    cal = SessionCalendar.from_rows(
        [
            (date(2025, 1, 31), False, None, None),
            (date(2025, 2, 1), True, opens, opens + timedelta(hours=6)),
        ]
    )
    assert cal.holidays == frozenset({date(2025, 1, 31)})


def test_build_days_add_weekend_sessions_in_range() -> None:
    sessions = [date(2025, 2, 1), date(2025, 2, 3), date(2020, 2, 1)]
    days = build_days(date(2025, 1, 30), date(2025, 2, 4), sessions)
    assert days == [
        date(2025, 1, 30),
        date(2025, 1, 31),
        date(2025, 2, 1),
        date(2025, 2, 3),
        date(2025, 2, 4),
    ]


def test_split_absent_keeps_only_unexplained_missing() -> None:
    a, b, c = date(2015, 3, 12), date(2015, 3, 13), date(2021, 2, 12)
    missing, absent = split_absent(
        {"NIFTY 50": [a, b], "INDIA VIX": [c]}, lambda name: {a} if name == "NIFTY 50" else set()
    )
    assert missing == {"NIFTY 50": [b], "INDIA VIX": [c]}
    assert absent == {"NIFTY 50": [a], "INDIA VIX": []}
    assert split_absent({"NIFTY 50": [a]}, None) == ({"NIFTY 50": [a]}, {"NIFTY 50": []})


def test_not_found_memo_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "build" / "index_not_found.json"
    path.parent.mkdir()
    memo = NotFoundMemo.load(path)
    assert memo.days == set()
    memo.add(date(2012, 1, 2))
    memo.add(date(2011, 12, 30))
    assert NotFoundMemo.load(path).days == {date(2011, 12, 30), date(2012, 1, 2)}
    for bad in ("[1, 2]", '{"not_found": 5}', '{"not_found": [5]}'):
        path.write_text(bad, encoding="utf-8")
        assert NotFoundMemo.load(path).days == set()


class _CodedError(HatError):
    code = "store_write_failed"


def test_stop_reason_is_a_code_never_text() -> None:
    assert stop_reason(KeyboardInterrupt()) == "interrupted"
    assert stop_reason(_CodedError("disk full at /secret/path")) == "store_write_failed"
    assert stop_reason(RuntimeError("password=x")) == "failed"


def test_bhav_candidates_carry_the_rows_series() -> None:
    day = date(2026, 10, 20)
    iid = uuid.uuid4()
    row = BhavRow(
        day,
        "INE000000001",
        "BE",
        "MOVED",
        *(Decimal(v) for v in ("10", "11", "9", "10.5", "10")),
        1000,
        Decimal("10500"),
    )
    out = _bhav_candidates(
        ParsedDay(day, [row], invalid=2), [Member(iid, "INE000000001", "MOVED", "EQ")]
    )
    assert out.series == {"MOVED": "BE"}
    assert out.traded == {"MOVED": Decimal("10500")}
    assert out.violations == 2
    assert out.accepted["MOVED"].close == Decimal("10.5")
