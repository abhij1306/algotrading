"""The corporate-action research import's pure seams: the market factor and its cross-checks
(CoA, priced ratios), the dates a source states, the research rows' merge and matching, and the
rights override. Plain examples from the research files' real rows; no database, no bars."""

from datetime import date
from fractions import Fraction

from app.hat.data.ca_import import REVIEW, Proposal, RightsTerms
from app.hat.data.ca_research import (
    FILE_A,
    FILE_C,
    FILE_D,
    ResultingLeg,
    RightsResearch,
    Source,
    agrees,
    coa_factor,
    date_check,
    isin_for,
    leading_number,
    market_factor,
    match_rows,
    merge_events,
    premium_in,
    ratio_factor,
    rights_override,
    same_company,
    stated_dates,
)

EX = date(2025, 10, 14)


def _leg(
    name: str,
    *,
    parent: str = "",
    own: str = "",
    ratio: tuple[int, int] = (1, 1),
    file: str = FILE_A,
    conf: int = 1,
) -> ResultingLeg:
    return ResultingLeg(
        name,
        "",
        "",
        (Fraction(ratio[0]), Fraction(ratio[1])) if ratio else None,
        Fraction(parent) if parent else None,
        Fraction(own) if own else None,
        Source(file, 2, conf, "u", "q"),
    )


def test_coa_factor_is_the_parents_own_share() -> None:
    assert coa_factor([_leg("TMCV", parent="68.85", own="31.15")]) == Fraction(6885, 10000)
    vedl = [
        _leg(n, parent="52.34", own=o)
        for n, o in (("VAML", "7.15"), ("Talwandi", "12.23"), ("Malco", "21.49"), ("VISL", "6.79"))
    ]
    assert coa_factor(vedl) == Fraction(5234, 10000)  # multi-leg: the parent's own %
    assert coa_factor([_leg("x")]) is None
    assert coa_factor([_leg("a", parent="40"), _leg("b", parent="41")]) == "coa_disagrees"
    # ARVIND 2018: 30.00 + 19.66 + 0.34 is 50, not 100
    arvind = [_leg("AFL", parent="30.00", own="19.66"), _leg("AHEL", own="0.34")]
    assert coa_factor(arvind) == "coa_does_not_sum"
    assert coa_factor([_leg("x", parent="100")]) == "coa_out_of_range"


def test_ratio_factor_over_every_leg() -> None:
    one = (Fraction(1), Fraction(1))
    # TMPV: 1 TMCV per share, TMCV's first close 329.45, TMPV's previous close 660.75
    assert float(ratio_factor([(one, Fraction("329.45"))], Fraction("660.75"))) == (
        1 - 329.45 / 660.75
    )
    # IIFL 2019: 1 IIFL Securities and 1/7 IIFL Wealth per share
    two = [(one, Fraction("23.10")), ((Fraction(1), Fraction(7)), Fraction("1270.50"))]
    assert ratio_factor(two, Fraction("417.45")) == 1 - (
        Fraction("23.10") + Fraction("1270.50") / 7
    ) / Fraction("417.45")
    assert ratio_factor([(one, Fraction(700))], Fraction(660)) is None  # worth more than the parent
    assert ratio_factor([], Fraction(10)) is None


def test_market_factor_stores_the_gap_never_a_cross_check() -> None:
    gap = Fraction(400) / Fraction("660.75")  # TMPV 2025-10-14: 0.6054
    checks = {"coa": Fraction("0.6885"), "ratio": Fraction("0.5014")}
    factor, why = market_factor(gap, checks, "", None)
    assert factor == gap
    assert why == "agrees with coa"
    # the gap disagrees with every cross-check: review
    assert market_factor(gap, {"coa": Fraction("0.95")}, "confirmed", None) == (
        None,
        "gap_disagrees_with_cross_checks",
    )
    # no cross-check: a material drop on a confirmed date that the price holds
    drop = Fraction("0.62")
    assert market_factor(drop, {}, "confirmed", Fraction("0.60"))[0] == drop
    assert market_factor(drop, {}, "", Fraction("0.60"))[1] == "no_cross_check_date_unconfirmed"
    assert market_factor(Fraction("0.95"), {}, "confirmed", Fraction("0.95"))[1] == (
        "no_cross_check_gap_not_material"
    )
    # TATACOMM 2019-09-17: open 0.62 of the previous close, back to 0.79 five sessions on
    assert market_factor(drop, {}, "confirmed", Fraction("0.79"))[1] == (
        "no_cross_check_gap_not_held"
    )
    assert market_factor(Fraction(1), checks, "confirmed", None) == (None, "no_drop")


def test_agrees_is_the_gate() -> None:
    assert agrees(Fraction("0.6054"), Fraction("0.6885"))  # 13% off, far from 1
    assert not agrees(Fraction("0.6054"), Fraction("0.8"))  # beyond 20%
    assert not agrees(Fraction("0.99"), Fraction("0.95"))  # a session move explains it as well


def test_stated_dates_and_date_check() -> None:
    assert stated_dates("Record date 24-Aug-2023; allotted 31-Aug-2023.") == [
        ("record", date(2023, 8, 24))
    ]
    assert stated_dates("ex 2019-07-04 (article) and record Aug 2, 2024") == [
        ("ex", date(2019, 7, 4)),
        ("record", date(2024, 8, 2)),
    ]
    assert stated_dates("2016-02-12 (record)", default="record") == [("record", date(2016, 2, 12))]
    assert date_check(EX, [("1:1 record date 14 Oct 2025.", "")]) == "confirmed"
    assert date_check(EX, [("Listing 2025-11-12.", "")]) == ""  # not a record or ex date
    # UEL 2024-05-22: sources give the 22nd and the 17th
    uel = [("record 2024-05-22 or 2024-05-17 (BSE)", "record")]
    assert date_check(date(2024, 5, 22), uel) == "disputed"
    # AARTIIND: the research puts the ex-date a day later
    assert date_check(date(2019, 7, 3), [("ex-date 2019-07-04 (article)", "")]) == "disputed"
    # another event's record date months away is not a dispute
    assert date_check(EX, [("record 2025-10-14; Adani Gas record 7-Sep-2018", "")]) == "confirmed"


def test_merge_events_d_wins_and_keeps_the_leg_type() -> None:
    key = ("ALLCARGO", date(2025, 11, 12))
    earlier = {
        key: [
            _leg("Allcargo Global Ltd", ratio=(1, 1), file="b"),
            _leg("Allcargo Gati Limited", ratio=(63, 10), file="b"),
        ]
    }
    earlier[key][1] = ResultingLeg(**{**earlier[key][1].__dict__, "amalgamation": True})
    later = {
        key: [
            _leg("Allcargo Global Ltd (formerly ...)", parent="87.94", own="12.06", file=FILE_D),
        ]
    }
    merged = merge_events(earlier, later)[key]
    assert [leg.name for leg in merged] == ["Allcargo Global Ltd (formerly ...)"]
    assert merged[0].coa_parent == Fraction("87.94")
    # a d leg keeps the type of the earlier leg of the same company, and duplicates collapse
    adani = ("ADANIENT", date(2015, 6, 3))
    amalg = ResultingLeg(
        **{**_leg("Adani Ports and SEZ Ltd", file="b").__dict__, "amalgamation": True}
    )
    out = merge_events(
        {adani: [amalg]},
        {
            adani: [
                _leg("Adani Ports and SEZ Ltd (shares)", file=FILE_D, conf=1),
                _leg("Adani Ports and SEZ Ltd", file=FILE_D, conf=2),
            ]
        },
    )[adani]
    assert len(out) == 1
    assert out[0].amalgamation
    assert out[0].source.confidence == 2


def test_same_company() -> None:
    assert same_company("Jio Financial Services Limited", "Jio Financial Services Ltd (formerly X)")
    assert not same_company("Manaksia Steels Ltd", "Manaksia Coated Metals & Industries")
    assert not same_company("", "")


def test_match_rows_by_symbol_and_ex_date() -> None:
    rows = [
        {"symbol": "TMPV", "exDate": "14-Oct-2025"},
        {"symbol": "X", "exDate": "01-Jan-2020"},
        {"symbol": "X", "exDate": "01-Jan-2020"},
    ]
    matched, missing = match_rows(rows, [("TMPV", EX), ("X", date(2020, 1, 1)), ("Y", EX)])
    assert list(matched) == [("TMPV", EX)]
    assert missing == [("X", date(2020, 1, 1)), ("Y", EX)]


def test_isin_for() -> None:
    assert isin_for("IN0000000000", "INE013801027") == "INE013801027"  # KSHITIJPOL's placeholder
    assert isin_for("INE053A01029", "") == "INE053A01029"
    assert isin_for("INE053A01029", "INE053A01029") == "INE053A01029"
    assert isin_for("INE053A01029", "INE999A01011") is None


def test_price_cells() -> None:
    assert leading_number("700 per share (FV 10 + premium 690)") == 700
    assert leading_number("10 (FV 10, no premium)") == 10
    assert leading_number("unknown") is None
    assert premium_in("700 per share (FV 10 + premium 690)") == 690
    assert premium_in("10 (FV 10, no premium)") == 0
    assert premium_in("15 (premium Rs 5 on FV 10 per share)") == 5


def _rights(text: str, fv: str, ratio: str) -> tuple[Proposal, dict[str, str]]:
    p = Proposal("c:rights", "2", "X", "INE000X01010", date(2021, 11, 11), text)
    return p, {"faceVal": fv, "entitlement_ratio_parsed": ratio}


def _research(
    m: int,
    n: int,
    price: int,
    *,
    premium: int | None = None,
    conf: int = 2,
    wrong: bool = False,
    partly: bool = False,
    alongside: bool = False,
) -> RightsResearch:
    terms = RightsTerms(Fraction(m), Fraction(n), Fraction(price))
    return RightsResearch(
        terms,
        Fraction(premium) if premium is not None else None,
        partly,
        alongside,
        wrong,
        "",
        Source(FILE_C, 2, conf, "u", "q"),
    )


def test_rights_override() -> None:
    # INDHOTEL 2021-11-11: the research confirms 1:9 at 150
    p, row = _rights("Rights 1:9 @ Premium Rs 149/-", "1", "1:9")
    done, note = rights_override(p, row, _research(1, 9, 150, premium=149))
    assert done.rights == RightsTerms(Fraction(1), Fraction(9), Fraction(150))
    assert not done.status
    assert note == "1:9 @ 150.0000"
    # CANBK 2017: our face value is today's 2, the issue's was 10 (the premium agrees)
    p, row = _rights("Rights 1:10 @ Prem Rs 197/- Per Share", "2", "1:10")
    done, note = rights_override(p, row, _research(1, 10, 207, premium=197))
    assert done.rights.price == 207
    assert "face value at the issue" in note
    # a partly-paid issue: the full issue price, said in the text
    p, row = _rights("Rights 1:20 Partly Paid @ Premium Rs. 440/-", "10", "1:20")
    done, note = rights_override(p, row, _research(1, 20, 450, premium=440, partly=True))
    assert done.rights.price == 450
    assert "partly paid: full issue price" in note
    # the terms differ and the research does not say ours are wrong
    p, row = _rights("Rights 1:9 @ Premium Rs 149/-", "1", "1:9")
    assert rights_override(p, row, _research(1, 8, 150))[0].reason == "research_terms_differ"
    # ARVINDFASN 2020: our terms, but the research says they were revised: review
    p, row = _rights("Rights 16:47 @ Premium Rs 146/-", "4", "16:47")
    flagged = rights_override(p, row, _research(16, 47, 150, premium=146, wrong=True))[0]
    assert (flagged.status, flagged.reason) == (REVIEW, "research_flags_row")
    # corrected terms are used when the research says ours are wrong
    p, row = _rights("Rights 1:9 @ Premium Rs 149/-", "1", "1:9")
    done, note = rights_override(p, row, _research(1, 8, 150, wrong=True))
    assert done.rights.held == 8
    assert "terms corrected" in note
    assert rights_override(p, row, _research(1, 9, 150, conf=0))[0].reason == "low_confidence"
    warrants = _research(1, 9, 150, alongside=True)
    assert rights_override(p, row, warrants)[0].reason == "warrants_or_convertibles_alongside"
