"""The backtest's as-of clock (`data.looks_ahead`): which reads a decision may make."""

from __future__ import annotations

from datetime import date, datetime

from app.hat.backtest.data import looks_ahead, public_by
from app.hat.engine.trigger import ResultEvent
from app.hat.time import IST

MON, TUE, WED = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)


def test_no_clock_refuses_nothing() -> None:
    assert not looks_ahead(WED, None, None, inclusive=True)


def test_at_the_open_only_the_last_close_is_known() -> None:
    # Tuesday's session in progress, Monday's close public
    assert not looks_ahead(MON, TUE, MON, inclusive=True)
    assert looks_ahead(TUE, TUE, MON, inclusive=True)  # Tuesday's close is not out yet
    assert not looks_ahead(TUE, TUE, MON, inclusive=False)  # its bar, or anything before it
    assert looks_ahead(WED, TUE, MON, inclusive=False)


def test_at_the_close_the_session_is_known() -> None:
    assert not looks_ahead(TUE, TUE, TUE, inclusive=True)
    assert looks_ahead(WED, TUE, TUE, inclusive=True)


def test_the_first_session_knows_no_close() -> None:
    assert looks_ahead(MON, MON, None, inclusive=True)
    assert not looks_ahead(MON, MON, None, inclusive=False)


def test_a_filing_is_read_only_from_the_day_it_is_public() -> None:
    """A trigger on a day reads the filings public by that day's end (IST), never a later one.
    Harm: an earnings trigger firing on a result not yet filed."""
    late = ResultEvent(
        datetime(2024, 1, 2, 23, 50, tzinfo=IST), date(2023, 12, 31), None, None, False
    )
    early = ResultEvent(
        datetime(2024, 1, 1, 18, 0, tzinfo=IST), date(2023, 12, 31), None, None, False
    )
    assert public_by((early, late), MON) == (early,)
    assert public_by((early, late), TUE) == (early, late)
