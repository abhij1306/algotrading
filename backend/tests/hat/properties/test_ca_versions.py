"""Versioned point-in-time factors (CA6): the selection, the dates, the pending sessions, the
demerger band and the regression rule. Plain examples; no database, no bars."""

from datetime import date
from decimal import Decimal
from fractions import Fraction

from app.hat.data.ca_demergers import Ratio, disputed, neutral_gap, ratio_from_journal
from app.hat.data.ca_versions import (
    ACCEPTED,
    CONFLICT,
    CROSS_CHECK,
    DELETED,
    Stored,
    Version,
    available_on,
    band,
    pending_days,
    projection_mismatches,
    select,
)

EX = date(2024, 9, 20)  # ROSSELLIND's demerger, a Friday
# the sessions from the Thursday before: Monday 23 to Friday 27 follow the ex-date
DAYS = [date(2024, 9, 19), EX, *(date(2024, 9, d) for d in range(23, 28)), date(2024, 9, 30)]
A, B = Decimal("0.2892"), Decimal("0.3")


def test_available_on_is_never_before_the_ex_date_or_its_evidence() -> None:
    assert available_on(EX, [date(2024, 9, 1)]) == EX
    assert available_on(EX, [date(2024, 9, 26), date(2024, 9, 23)]) == date(2024, 9, 26)
    assert available_on(EX, [date(2024, 9, 26), None]) is None  # one source undated
    assert available_on(EX, []) is None


def test_select_hindsight_and_as_of() -> None:
    first = Version(1, ACCEPTED, A, available_on=date(2024, 9, 26))
    corrected = Version(2, ACCEPTED, B, available_on=date(2024, 9, 30))
    noise = [Version(3, CONFLICT, Decimal("0.5"), available_on=EX), Version(4, CROSS_CHECK, A)]
    versions = [first, corrected, *noise]
    assert select(versions, None) == corrected  # hindsight: highest precedence
    assert select(versions, date(2024, 9, 25)) is None  # nothing available yet
    assert select(versions, date(2024, 9, 26)) == first
    assert select(versions, date(2024, 9, 30)) == corrected
    undated = [first, Version(2, ACCEPTED, B)]
    assert select(undated, date(2030, 1, 1)) == first  # an unknown date never qualifies
    assert select(undated, None) == undated[1]
    deleted = [first, Version(2, DELETED, A, available_on=date(2024, 9, 27))]
    assert select(deleted, None) is None
    assert select(deleted, date(2024, 9, 26)) == first


def test_pending_days_from_the_ex_date_to_the_day_before_available_on() -> None:
    def pending(versions: list[Version], ex: date = EX) -> list[date]:
        return pending_days(versions, ex, DAYS, (A, None))

    assert pending([Version(1, ACCEPTED, A, available_on=date(2024, 9, 26))]) == [
        EX,
        *DAYS[2:5],
    ]
    assert pending([Version(1, ACCEPTED, A, available_on=EX)]) == []  # known at the ex open
    assert pending([Version(1, ACCEPTED, A)]) == DAYS[1:]  # undated: every session from ex
    holiday = date(2024, 9, 21)  # a span starting on a holiday covers from the next session
    assert pending([Version(1, ACCEPTED, A)], holiday)[0] == date(2024, 9, 23)


def test_pending_days_when_an_earlier_value_differs_from_the_series() -> None:
    # a value available at the ex open, corrected on the 26th: the series applies the
    # correction, so the days the earlier value was the one available are pending
    versions = [
        Version(1, ACCEPTED, B, available_on=EX),
        Version(2, ACCEPTED, A, available_on=date(2024, 9, 26)),
    ]
    assert pending_days(versions, EX, DAYS, (A, None)) == [EX, *DAYS[2:5]]
    # a dividend amount is part of the value
    cash = [Version(1, ACCEPTED, Decimal(1), Decimal("5"), EX)]
    assert pending_days(cash, EX, DAYS, (Decimal(1), Decimal("6"))) == DAYS[1:]


def test_regression_later_versions_never_change_a_frozen_day() -> None:
    frozen = date(2024, 9, 25)
    versions = [Version(1, ACCEPTED, A, available_on=EX)]
    before = {d: select(versions, d) for d in DAYS if d <= frozen}
    pending_before = set(pending_days(versions, EX, DAYS, (A, None)))
    later = [
        *versions,
        Version(2, ACCEPTED, A, available_on=date(2024, 9, 26)),  # a later confirmation
        Version(3, ACCEPTED, B, available_on=date(2024, 9, 27)),  # a later different value
    ]
    assert select(later, frozen) == select(versions, frozen)
    assert {d: select(later, d) for d in DAYS if d <= frozen} == before
    # the series now applies B: days up to the freeze may become pending, none stops being so,
    # and no value a day up to the freeze reads changes
    pending_after = set(pending_days(later, EX, DAYS, (B, None)))
    assert pending_before <= pending_after
    for day in (d for d in DAYS if d <= frozen and d not in pending_after):
        assert select(later, day) == before[day]


def test_band_and_dispute() -> None:
    assert band([Fraction(3, 10), Fraction(1, 4), Fraction(2, 7)]) == (
        Fraction(1, 4),
        Fraction(3, 10),
    )
    assert not disputed(Fraction(60, 100), Fraction(65, 100))  # 8% apart in log terms
    assert disputed(Fraction(6, 10), Fraction(5, 10))  # TMPV's ratio 0.50 against 0.605


def test_neutral_gap_divides_out_the_index_night() -> None:
    index = {date(2024, 9, 19): (Fraction(100), Fraction(100)), EX: (Fraction(102), Fraction(101))}
    assert neutral_gap(Fraction(3, 10), index, date(2024, 9, 19), EX) == Fraction(3, 10) / Fraction(
        102, 100
    )
    assert neutral_gap(Fraction(3, 10), index, date(2024, 9, 18), EX) is None  # no index day


def test_ratio_from_the_import_journal() -> None:
    derived = "resulting ITCHOTELS INE379A01028 first close 171.8500 on 2025-01-29 -> factor 0.9643"
    ca1 = {"factor": "0.964316860465", "check": {"derived": derived}}
    found = ratio_from_journal(date(2025, 1, 6), ca1, "Demerger 1:10", {})
    assert found == Ratio(Fraction(964316860465, 10**12), date(2025, 1, 29))
    # a research ratio cross-check, dated by the one listing whose first close gives it
    check = {"cross_checks": {"ratio": "0.5218"}, "previous_close": "368.0000"}
    text = "Demerger 1:2 TCI Express Ltd [research market_gap; ratio 0.5218]"
    closes = {"x": (date(2016, 9, 1), Decimal("351.96")), "y": (date(2016, 9, 2), Decimal("10"))}
    found = ratio_from_journal(date(2016, 8, 26), {"check": check}, text, closes)
    assert found is not None
    assert found.available == date(2016, 9, 1)
    assert round(float(found.value), 4) == 0.5218
    undated = ratio_from_journal(date(2016, 8, 26), {"check": check}, text, {})
    assert undated == Ratio(Fraction("0.5218"), None)
    assert ratio_from_journal(date(2016, 8, 26), {"check": {"gap": "0.5"}}, text, {}) is None


def _stored(key: tuple[str, date, str], factor: str, known: date | None) -> Stored:
    return Stored(key, Decimal(factor), None, known, "ex_open" if known else None, "mcp", "t")


def test_projection_mismatches() -> None:
    key, gone = ("i", EX, "split"), ("i", EX, "bonus")
    rows = [_stored(key, "0.5", EX)]
    good = {
        key: [Version(1, ACCEPTED, Decimal("0.5"), None, EX, "ex_open")],
        gone: [Version(1, ACCEPTED, Decimal("0.5")), Version(2, DELETED, Decimal("0.5"))],
    }
    assert projection_mismatches(rows, good) == []
    assert projection_mismatches(rows, {}) == [(key, "no_version")]
    other = {key: [Version(1, ACCEPTED, Decimal("0.4"), None, EX, "ex_open")]}
    assert projection_mismatches(rows, other) == [(key, "value")]
    assert projection_mismatches([], good) == [(key, "not_stored")]
