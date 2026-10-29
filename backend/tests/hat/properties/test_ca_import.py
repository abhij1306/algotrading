"""The corporate-action file import's pure seams: dates, ratios, split and bonus legs, rights
terms and TERP, the demerger factor and the price gate. Plain examples from the file's real
texts; no database, no bars."""

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from app.hat.data.ca_apply import Leg, Outcome, Proposal, RightsTerms, Stored, dedupe, plan_legs
from app.hat.data.ca_gate import ALREADY, REVIEW, SKIPPED, STORED, gate, nse_date, same_factor
from app.hat.data.ca_import import (
    _split_bonus,
    demerger_factor,
    face_value_changes,
    face_value_on,
    name_key,
    parse_ratio,
    proposals,
    rights_factor,
    rights_terms,
    split_bonus_legs,
)
from app.hat.data.corporate_actions import TYPE_BONUS, TYPE_OTHER, TYPE_SPLIT, ActionRecord


def test_nse_date() -> None:
    assert nse_date("04-Sep-2014") == date(2014, 9, 4)
    assert nse_date(" 1-dec-2019 ") == date(2019, 12, 1)
    for bad in ("-", "", "31-Feb-2020", "2014-09-04", "04-Sept-2014", "04-Xyz-2014"):
        assert nse_date(bad) is None


def test_parse_ratio() -> None:
    assert parse_ratio("1:10") == (Fraction(1), Fraction(10))
    assert parse_ratio(" 1 : 48.18 ") == (Fraction(1), Fraction("48.18"))
    for bad in ("", "1:0", "0:5", "1:2:3", "a:b", "1/2", "1:10 approx"):
        assert parse_ratio(bad) is None


@pytest.mark.parametrize(
    ("text", "legs"),
    [
        ("Bonus 1:2", ((TYPE_BONUS, Fraction(2, 3)),)),
        ("Bonus 1 : 10", ((TYPE_BONUS, Fraction(10, 11)),)),
        ("Fv Splt Frm Rs 10 To Re 1", ((TYPE_SPLIT, Fraction(1, 10)),)),
        (
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
            ((TYPE_SPLIT, Fraction(1, 5)),),
        ),
        (
            "Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share "
            "To Rs 2/- Per Share",
            ((TYPE_BONUS, Fraction(1, 2)), (TYPE_SPLIT, Fraction(1, 5))),
        ),
        (
            "Bonus 1:2 And Face Value Split Rs.10/- To Rs.5/-",
            ((TYPE_BONUS, Fraction(2, 3)), (TYPE_SPLIT, Fraction(1, 2))),
        ),
        # a dividend's amount before the split is not one of the split's numbers
        (
            "Interim Dividend - Rs 1.50/- Per Share / Face Value Split - From Rs 10/- Per Share "
            "To Rs 5/- Per Share (Purpose Revised)",
            ((TYPE_SPLIT, Fraction(1, 2)),),
        ),
        (
            "Annual General Meeting / Dividend - Rs 2/- Per Share / Bonus 1 : 1",
            ((TYPE_BONUS, Fraction(1, 2)),),
        ),
    ],
)
def test_split_bonus_legs(text: str, legs: tuple[tuple[str, Fraction], ...]) -> None:
    assert split_bonus_legs(text) == tuple(Leg(t, f) for t, f in legs)


def test_split_bonus_legs_refuses_what_it_cannot_read() -> None:
    assert split_bonus_legs("Scheme Of Arrangement - Bonus Ncrps 4:1") is None
    assert split_bonus_legs("Face Value Split") is None
    assert split_bonus_legs("Bonus 1:1 And Face Value Split") is None  # a leg without numbers


def test_rights_terms() -> None:
    assert rights_terms("Rights 1:10 @ Premium Rs 95/-", "10") == RightsTerms(1, 10, 105)
    assert rights_terms("RIGHTS 6:25 @ PREMIUM RS 39.50 /-", "5") == RightsTerms(
        6, 25, Fraction("44.5")
    )
    assert rights_terms("Rights - 1:2@ Par Rs 10 Per Share", "10") == RightsTerms(1, 2, 10)
    assert rights_terms("Rights : 3:7 At Par", "2") == RightsTerms(3, 7, 2)
    assert rights_terms("Rights 613:399", "10") == "no_issue_price"
    assert rights_terms("Rights 1:1 Partly Paid @ Premium Rs 10/-", "1") == "non_simple"
    assert rights_terms(
        "Rights 1:5 @ Premium Rs 50/- With 1 Warrants For 5 Equity Shares", "1"
    ) == ("non_simple")
    assert rights_terms("Rights 1:10 @ Premium Rs 95/-", "-") == "unparsed"


def test_face_value_on_the_rights_ex_date() -> None:
    """The file's `faceVal` is today's: a rights issue before a split is priced at the face
    value of its own day (CANFINHOME 2015: FV 10 then, 2 today)."""
    split = face_value_changes("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/-")
    assert split == Fraction(1, 5)
    assert face_value_changes("Consolidation Of Shares From Re 1/- To Rs 10/-") == 10
    assert face_value_changes("Bonus 1:1") is None
    changes = [(date(2016, 9, 22), Fraction(1, 5))]
    assert face_value_on(Fraction(2), date(2015, 1, 23), changes) == 10
    assert face_value_on(Fraction(2), date(2017, 1, 1), changes) == 2


def _rights_row(**cells: str) -> dict[str, str]:
    row = {"rowid": "1", "symbol": "CANFINHOME", "market": "Equity", "isin": "INE477A01020"}
    row |= {"subject_raw": "Rights 3:10 @ Premium Rs 440/-", "entitlement_ratio_parsed": "3:10"}
    row |= {"premium_rs_parsed": "440", "faceVal": "2", "exDate": "23-Jan-2015"}
    return row | cells


def test_proposals_price_rights_at_the_ex_dates_face_value() -> None:
    split = {"rowid": "2", "symbol": "CANFINHOME", "isin": "INE477A01020", "action_type": "split"}
    split |= {"subject_raw": "Face Value Split From Rs 10/- To Rs 2/-", "instrument_class": ""}
    split |= {"exDate": "22-Sep-2016"}
    (rights,) = [
        p
        for p in proposals({"rights_issues": [_rights_row()], "splits_bonus": [split]})
        if p.table == "rights_issues"
    ]
    assert rights.rights == RightsTerms(3, 10, 450)
    (alone,) = proposals({"rights_issues": [_rights_row()]})
    assert alone.rights == RightsTerms(3, 10, 442)
    # a split announced but not yet in effect has not changed the file's faceVal
    tables = {"rights_issues": [_rights_row()], "splits_bonus": [split]}
    (early,) = [p for p in proposals(tables, date(2016, 9, 1)) if p.table == "rights_issues"]
    assert early.rights == RightsTerms(3, 10, 442)


def test_subdivision_and_grouped_premium() -> None:
    """The two misreads CodeAnt found in the NSE-noodles port: "Subdivision" spelt as one word
    is a split, and a premium with digit groups is read whole, not as 1."""
    text = "Face Value Subdivision - From Rs 10/- Per Share To Re 1/- Per Share"
    assert split_bonus_legs(text) == (Leg(TYPE_SPLIT, Fraction(1, 10)),)
    assert rights_terms("Rights 1:5 @ Premium Rs 1,000/-", "10") == RightsTerms(1, 5, 1010)
    assert rights_terms("Rights 1:5 @ Premium Rs 1,00,000/-", "10") == RightsTerms(1, 5, 100010)


def test_rights_factor() -> None:
    # BHARATGEAR 2021: 1 new per 10 held at 105, cum 173.70
    terms = RightsTerms(Fraction(1), Fraction(10), Fraction(105))
    factor = rights_factor(terms, Fraction("173.7"))
    assert factor == (10 * Fraction("173.7") + 105) / (11 * Fraction("173.7"))
    assert round(float(factor), 4) == 0.964
    assert rights_factor(terms, Fraction(105)) == 1  # at the cum price: neutral
    assert rights_factor(terms, Fraction(90)) == 1  # above it: neutral
    zero = Fraction(0)
    with pytest.raises(ValueError, match="cum_price"):
        rights_factor(terms, zero)


def test_demerger_factor() -> None:
    # GMR 2022: 1 GPUIL per 10 GIL, GPUIL first close 45.60, GIL cum close 48.45
    factor = demerger_factor((Fraction(1), Fraction(10)), Fraction("45.6"), Fraction("48.45"))
    assert factor == 1 - Fraction("4.56") / Fraction("48.45")
    assert demerger_factor((Fraction(1), Fraction(1)), Fraction(500), Fraction(250)) is None
    assert demerger_factor((Fraction(1), Fraction(1)), Fraction(0), Fraction(250)) is None


def test_gate() -> None:
    # J&KBANK 2014 split 10:1: previous close 1579.65, ex open 160
    assert gate(Fraction(1, 10), Fraction("1579.65"), Fraction(160)).confirmed
    # within 20% of the factor, and far closer to it than to 1
    assert gate(Fraction(1, 2), Fraction(100), Fraction(59)).confirmed
    assert gate(Fraction(1, 2), Fraction(100), Fraction(61)).reason == "off_factor"
    assert gate(Fraction(1, 2), Fraction(100), Fraction(100)).reason == "off_factor"
    # a 4% rights factor: an open 2% down could be a plain session move
    assert gate(Fraction(96, 100), Fraction(100), Fraction(98)).reason == "ambiguous"
    assert gate(Fraction(96, 100), Fraction(100), Fraction("96.3")).confirmed
    assert gate(Fraction(96, 100), Fraction(100), Fraction(100)).reason == "ambiguous"
    half, zero, fifty = Fraction(1, 2), Fraction(0), Fraction(50)
    with pytest.raises(ValueError, match="positive"):
        gate(half, zero, fifty)


def test_plan_legs() -> None:
    day = date(2016, 9, 8)
    bonus, split = Leg(TYPE_BONUS, Fraction(1, 2)), Leg(TYPE_SPLIT, Fraction(1, 5))
    stored_split = Stored("i", day, TYPE_SPLIT, Decimal("0.2"))
    new, already, conflicts = plan_legs([bonus, split], [stored_split])
    assert (new, already, conflicts) == ([bonus], [split], [])
    wrong = Stored("i", day, TYPE_SPLIT, Decimal("6.6667"))  # a misparsed stored factor
    assert plan_legs([split], [wrong]) == ([], [], [(split, wrong)])
    rights = Leg(TYPE_OTHER, Fraction(9, 10))
    assert plan_legs([rights], [stored_split]) == ([rights], [], [])


def test_same_factor_allows_the_mcp_rounding() -> None:
    assert same_factor(Decimal("0.6667"), Fraction(2, 3))
    assert same_factor(Decimal("0.9091"), Fraction(10, 11))
    assert not same_factor(Decimal("0.6666"), Fraction(2, 3) + Fraction(1, 10000))
    assert not same_factor(Decimal("0.5"), Fraction(1, 5))


def test_name_key() -> None:
    assert name_key("The Tata Power Company Limited") == "TATA"
    assert name_key("GMR Power and Urban Infra Limited") == "GMR"
    assert name_key("") == ""


def _row(subject: str, action_type: str) -> dict[str, str]:
    return {
        "rowid": "1",
        "symbol": "X",
        "isin": "INE000X01010",
        "action_type": action_type,
        "subject_raw": subject,
        "instrument_class": "",
        "exDate": "08-Sep-2016",
    }


def test_a_non_equity_bonus_keeps_the_split_beside_it() -> None:
    p = _split_bonus(
        _row("Bonus Debentures 1:1 / Face Value Split From Rs 10/- To Rs 2/-", "bonus+split")
    )
    assert p.legs == (Leg(TYPE_SPLIT, Fraction(1, 5)),)
    assert not p.status
    alone = _split_bonus(_row("Scheme Of Arrangement - Bonus Ncrps 4:1", "bonus"))
    assert (alone.status, alone.reason) == (SKIPPED, "non_equity_text")


def test_stored_kind_tells_a_demerger_from_rights() -> None:
    day = date(2025, 1, 6)
    assert Stored("i", day, TYPE_OTHER, Decimal("0.96"), demerger=True).kind == "demerger"
    assert Stored("i", day, TYPE_OTHER, Decimal("0.96")).kind == TYPE_OTHER
    assert Stored("i", day, TYPE_SPLIT, Decimal("0.5")).kind == TYPE_SPLIT


def _outcome(*legs: tuple[str, str]) -> Outcome:
    p = Proposal("splits_bonus", "1", "X", "", date(2016, 9, 8), "t")
    records = [
        ActionRecord("i", date(2016, 9, 8), kind, Decimal(f), "t", "nse_ca_file")
        for kind, f in legs
    ]
    return Outcome(p, STORED, "", "", records)


def test_dedupe_per_key() -> None:
    # the same split twice: the later row keeps its bonus leg, its split is dropped
    first, second = (
        _outcome((TYPE_SPLIT, "0.2")),
        _outcome((TYPE_BONUS, "0.5"), (TYPE_SPLIT, "0.2")),
    )
    dedupe([first, second])
    assert [r.action_type for r in first.records] == [TYPE_SPLIT]
    assert [r.action_type for r in second.records] == [TYPE_BONUS]
    assert second.status == STORED
    # the same split with different bonus legs elsewhere is no disagreement
    a, b = _outcome((TYPE_SPLIT, "0.2"), (TYPE_BONUS, "0.5")), _outcome((TYPE_SPLIT, "0.2"))
    dedupe([a, b])
    assert (b.status, b.reason, b.records) == (ALREADY, "duplicate_in_file", [])
    assert len(a.records) == 2
    # two factors for one key: both rows go to review
    c, d = _outcome((TYPE_SPLIT, "0.2")), _outcome((TYPE_SPLIT, "0.5"))
    dedupe([c, d])
    assert {(o.status, o.reason) for o in (c, d)} == {(REVIEW, "file_disagrees_with_itself")}
