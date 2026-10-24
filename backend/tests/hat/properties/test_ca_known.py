"""Point in time (CA5): when a stored factor became knowable, per basis, and the factor-pending
window a backtest skips. Plain examples; no database, no bars."""

from datetime import date, timedelta
from decimal import Decimal
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
    Window,
    earliest,
    from_sources,
    lag_sessions,
    latest,
    pending_days,
    settle_day,
    settle_known,
)
from app.hat.data.ca_research import (
    Priced,
    ResultingLeg,
    Source,
    demerger_known,
    research_supplied,
)
from app.hat.data.corporate_actions import correction_known, default_known
from app.hat.data.point_in_time import Row, classify, log_path

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


def test_pending_days_from_the_ex_date_to_the_day_before_known_on() -> None:
    known = Window("A", EX, date(2024, 9, 26))
    assert sorted(d for _, d in pending_days([known], DAYS)) == [EX, *DAYS[2:5]]
    assert pending_days([Window("B", EX, EX)], DAYS) == set()  # known at the ex open
    undated = pending_days([Window("C", EX, None)], DAYS)
    assert sorted(d for _, d in undated) == DAYS[1:]  # every session from the ex-date
    # a window starting on a holiday covers from the next session
    assert min(d for _, d in pending_days([Window("D", date(2024, 9, 21), None)], DAYS)) == (
        date(2024, 9, 23)
    )


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


def _row(action_type: str, source: str, text: str, factor: str = "0.5") -> Row:
    return Row("r", "i", "INE000A01010", EX, action_type, Decimal(factor), text, source, None)


def test_classify_backfills_from_what_is_stored() -> None:
    gap = (Fraction(2892, 10000), DAYS)
    settle = Known(date(2024, 9, 26), SETTLED_CLOSE)
    assert classify(_row("split", "mcp", "Split"), owner=False, derived="", market=None) == Known(
        EX, EX_OPEN
    )
    assert classify(_row("other", "mcp", "?"), owner=False, derived="", market=None) == UNKNOWN
    rights = _row("other", "nse_ca_file", "Rights 1:10 @ Premium Rs 95/-")
    assert classify(rights, owner=False, derived="", market=None) == Known(EX, EX_OPEN)
    child = "resulting X INE1 first close 66.45 on 2024-12-09 -> factor 0.8445"
    demerger = _row("other", "nse_ca_file", "Demerger 1:1")
    assert classify(demerger, owner=False, derived=child, market=gap) == Known(
        date(2024, 12, 9), RESULTING_FIRST_CLOSE
    )
    gap_only = "no resulting listing found: the ex-date gap is the factor"
    assert classify(demerger, owner=False, derived=gap_only, market=gap) == settle
    research = _row("other", "ca_research", "Demerger 1:1 X [research market_gap]")
    assert classify(research, owner=False, derived="", market=gap) == settle
    # the owner's market-gap demerger, and an owner value that is not the gap
    owner = _row("other", "owner", "ROSSELLIND Demerger [market gap]", "0.2892")
    assert classify(owner, owner=True, derived="", market=gap) == settle
    assert classify(
        _row("other", "owner", "Demerger", "0.5"), owner=True, derived="", market=gap
    ) == (UNKNOWN)
    # an owner's split the ex-date gap confirms, and a public-only bonus it cannot
    split = (Fraction(1046, 10000), DAYS)
    assert classify(
        _row("split", "owner", "FV 10 to 1", "0.1"), owner=True, derived="", market=split
    ) == (Known(EX, EX_OPEN))
    near_one = (Fraction(101, 100), DAYS)  # the market rose: no gap confirms it
    assert classify(
        _row("bonus", "owner", "public only", "0.9705"), owner=True, derived="", market=near_one
    ) == (UNKNOWN)
    dist = _row("dividend", "ca_research", "Dividend bonus_instrument 30: (research f high)", "1")
    assert classify(dist, owner=False, derived="", market=None) == UNKNOWN


def test_one_window_per_day_span() -> None:
    days = [EX + timedelta(days=n) for n in range(10)]
    assert len(pending_days([Window("A", EX, days[3]), Window("A", EX, days[5])], days)) == 5


def test_log_path_stays_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert log_path("out/log.tsv") == tmp_path.resolve() / "out" / "log.tsv"
    for name in ("../log.tsv", "/etc/log.tsv", "log.csv"):
        with pytest.raises(SystemExit):
            log_path(name)
