"""Point in time (CA5): when a stored factor became knowable, per basis, and the copy of an event
the series applies. Plain examples; no database, no bars. The pending sessions are
`test_ca_versions`'."""

from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from app.hat.data.ca_known import (
    CORRECTION,
    EX_OPEN,
    RESEARCH_SOURCE_DATE,
    RESULTING_FIRST_CLOSE,
    SETTLED_CLOSE,
    UNKNOWN,
    Known,
    earliest,
    from_sources,
    lag_sessions,
    latest,
    settle_day,
    settle_known,
)
from app.hat.data.ca_research import Priced, demerger_known, research_supplied
from app.hat.data.ca_research_rows import ResultingLeg, Source
from app.hat.data.corporate_actions import correction_known, default_known
from app.hat.data.point_in_time import log_path, series_events

EX = date(2024, 9, 20)  # ROSSELLIND's demerger, a Friday
# the sessions from the Thursday before: Monday 23 to Friday 27 follow the ex-date
DAYS = [date(2024, 9, 19), EX, *(date(2024, 9, d) for d in range(23, 28)), date(2024, 9, 30)]


def test_ex_open_bases() -> None:
    for action_type in ("split", "bonus", "dividend", "notice"):
        assert default_known(action_type, EX) == Known(EX, EX_OPEN)
    assert default_known("other", EX) == UNKNOWN  # an MCP factor of another type: never guessed


def test_settled_close_is_the_fifth_session_from_the_ex_session() -> None:
    assert settle_day(DAYS, EX) == date(2024, 9, 26)
    assert settle_known(DAYS, EX) == Known(date(2024, 9, 26), SETTLED_CLOSE)
    assert settle_known(DAYS[:5], EX) == UNKNOWN  # the bars do not reach it


def test_source_dates_are_never_before_the_ex_date_and_never_guessed() -> None:
    assert from_sources(EX, [date(2024, 8, 1)], RESEARCH_SOURCE_DATE) == Known(
        EX, RESEARCH_SOURCE_DATE
    )
    later = [date(2024, 10, 1), date(2024, 12, 9)]
    assert from_sources(EX, later, RESULTING_FIRST_CLOSE) == Known(
        date(2024, 12, 9), RESULTING_FIRST_CLOSE
    )
    assert from_sources(EX, [date(2024, 10, 1), None], RESEARCH_SOURCE_DATE) == UNKNOWN
    assert from_sources(EX, [], RESEARCH_SOURCE_DATE) == UNKNOWN


def test_earliest_route_and_latest_part() -> None:
    settle = Known(date(2024, 9, 26), SETTLED_CLOSE)
    ratio = Known(date(2024, 12, 9), RESULTING_FIRST_CLOSE)
    assert earliest([UNKNOWN, ratio, settle]) == settle
    assert earliest([UNKNOWN]) == UNKNOWN
    assert latest([Known(EX, EX_OPEN), ratio]) == ratio
    assert latest([Known(EX, EX_OPEN), UNKNOWN]) == UNKNOWN


def test_correction_known() -> None:
    assert correction_known(EX, date(2026, 10, 24), price_neutral=False) == Known(
        date(2026, 10, 24), CORRECTION
    )
    assert correction_known(EX, date(2024, 1, 1), price_neutral=False) == Known(EX, CORRECTION)
    assert correction_known(EX, None, price_neutral=False) == UNKNOWN
    assert correction_known(EX, None, price_neutral=True) == Known(EX, EX_OPEN)


def test_lag_sessions() -> None:
    assert lag_sessions(DAYS, EX, EX) == 0
    assert lag_sessions(DAYS, EX, date(2024, 9, 26)) == 4
    assert lag_sessions(DAYS, EX, None) is None


def _priced(day: date) -> Priced:
    return Priced(None, day, Fraction(100), True)  # type: ignore[arg-type]


def test_demerger_known_takes_the_first_accepting_route() -> None:
    def leg(day: date | None) -> ResultingLeg:
        return ResultingLeg("Child", "", "", (1, 1), None, None, Source("a", 2, 2, "", "", day))

    gap = Fraction(30, 100)
    listed = _priced(date(2024, 12, 9))
    # no cross-check: the gap held to the fifth session
    assert demerger_known(EX, gap, {}, [leg(None)], [None], DAYS) == Known(
        date(2024, 9, 26), SETTLED_CLOSE
    )
    # an agreeing ratio check with a dated source, known at the child's close, comes later
    ratio = {"ratio": Fraction(31, 100)}
    assert demerger_known(EX, gap, ratio, [leg(EX)], [listed], DAYS).basis == SETTLED_CLOSE
    # without bars to settle on, the dated ratio route; undated research: unknown
    assert demerger_known(EX, gap, ratio, [leg(EX)], [listed], []) == Known(
        date(2024, 12, 9), RESULTING_FIRST_CLOSE
    )
    assert demerger_known(EX, gap, ratio, [leg(None)], [listed], []) == UNKNOWN


def test_research_supplied_rights_terms() -> None:
    assert not research_supplied("Rights 9:20 @ Premium Rs 10/- [research c: 9:20 @ 20.0000]")
    assert not research_supplied(
        "Rights 9:77 Partly Paid @ Premium Rs 100/- [research b: 9:77 @ 110.0000; partly paid]"
    )
    assert research_supplied("Rights 2:7 [research b: 2:7 @ 201.0000]")  # no price in NSE's text
    assert research_supplied(
        "Rights 3:7 @ Premium Rs 14/- [research c: 3:7 @ 24.0000; face value at the issue per"
        " research]"
    )
    assert research_supplied(
        "Rights 1:8 @ Premium Rs 5/- [research b: terms corrected by research]"
    )
    cut = "Rights 9:20 @ Premium Rs 10/- [research c: 9:20 @ 20.0000; face val"
    assert research_supplied(cut)  # a tag cut at TEXT_MAX may have lost its markers


def test_log_path_stays_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert log_path("out/log.tsv") == tmp_path.resolve() / "out" / "log.tsv"
    for name in ("../log.tsv", "/etc/log.tsv", "log.csv"):
        with pytest.raises(SystemExit):
            log_path(name)


def test_series_events_keep_the_copy_the_series_applies() -> None:
    heads = {"old": ("new", 1), "new": ("new", 0)}
    rows = [
        ("old", EX, "other"),  # a copy the series ignores
        ("new", EX, "other"),
        ("old", EX, "dividend"),  # the only copy: kept, under the chain's id
        ("solo", EX, "split"),
    ]
    assert series_events(rows, heads) == {
        ("new", EX, "dividend"): "old",
        ("new", EX, "other"): "new",
        ("solo", EX, "split"): "solo",
    }
