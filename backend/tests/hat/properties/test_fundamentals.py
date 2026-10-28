"""The fundamentals store's pure seams (`app/hat/data/fundamentals/`): the XBRL and page parsers on
trimmed real documents, the formulas, the point-in-time book and the timing of page figures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.data.fundamentals import formulas as f
from app.hat.data.fundamentals.book import Book, KnownFact, Quote, metrics, months_before
from app.hat.data.fundamentals.derive import (
    Prices,
    StoredFiling,
    close_known_on,
    metric_history,
)
from app.hat.data.fundamentals.extract import (
    FilingOut,
    copy_text,
    screener_filings,
    screener_public_at,
)
from app.hat.data.fundamentals.items import nse_statement, screener_label
from app.hat.data.fundamentals.nse import parse_document
from app.hat.data.fundamentals.report import pairs
from app.hat.data.fundamentals.screener import Figure, cell_value, column_date, parse_page
from app.hat.data.results.timing import PublicAt
from app.hat.time import IST

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_results"
CRORE = Decimal(10) ** 7
FETCHED = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)


def _facts(name: str) -> dict[tuple[str, str, str], Decimal]:
    document = parse_document((FIXTURES / name).read_bytes())
    return {(x.context, x.period_kind, x.item or x.element): x.value for x in document.facts}


# --- NSE XBRL ---------------------------------------------------------------------------------


def test_half_year_document_balance_sheet_and_cash_flow() -> None:
    document = parse_document((FIXTURES / "reg33_indas_q2_balance_sheet_reliance.xml").read_bytes())
    assert (document.basis, document.family, document.period_end) == (
        "consolidated",
        "indas",
        date(2024, 9, 30),
    )
    by_item = {(x.item, x.period_kind): x for x in document.facts if x.item}
    assets = by_item[("total_assets", "instant")]
    assert (assets.statement, assets.period_start, assets.value) == (
        "bs",
        None,
        Decimal("18151230000000.00"),
    )
    cfo = by_item[("cfo", "ytd")]
    assert (cfo.statement, cfo.period_start, cfo.unit) == ("cf", date(2024, 4, 1), "inr")
    assert by_item[("revenue", "quarter")].period_start == date(2024, 7, 1)
    assert by_item[("eps_basic", "quarter")].unit == "inr_per_share"
    segment = next(x for x in document.facts if x.context == "OneReportableSegmentRevenue01D")
    assert (segment.statement, segment.period_kind, segment.item) == ("segment", "quarter", None)
    assert all(x.period_end == date(2024, 9, 30) for x in document.facts)


def test_annual_only_document_is_the_year() -> None:
    found = _facts("reg33_banking_q4_annual_only_hdfcbank.xml")
    assert ("OneD", "annual", "revenue") in found
    assert not any(kind == "quarter" for _c, kind, _i in found)


_COMPARATIVE = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:link="http://www.xbrl.org/2003/linkbase" xmlns:xlink="http://www.w3.org/1999/xlink"
 xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">
<link:schemaRef xlink:type="simple" xlink:href="Ind-AS_entry_point_2020-03-31.xsd"/>
<xbrli:context id="OneD"><xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate>
<xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="OneI"><xbrli:period><xbrli:instant>2024-03-31</xbrli:instant></xbrli:period>
</xbrli:context>
<xbrli:context id="PY_I"><xbrli:period><xbrli:instant>2023-03-31</xbrli:instant></xbrli:period>
</xbrli:context>
<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
<in-bse-fin:RevenueFromOperations contextRef="OneD" unitRef="INR">100
</in-bse-fin:RevenueFromOperations>
<in-bse-fin:Assets contextRef="OneI" unitRef="INR">500</in-bse-fin:Assets>
<in-bse-fin:Assets contextRef="PY_I" unitRef="INR">400</in-bse-fin:Assets>
<in-bse-fin:NatureOfReportStandaloneConsolidated contextRef="OneD">Standalone
</in-bse-fin:NatureOfReportStandaloneConsolidated>
</xbrli:xbrl>
"""


def test_comparative_context_is_left_out() -> None:
    document = parse_document(_COMPARATIVE)
    assert {(x.context, x.item, x.value) for x in document.facts} == {
        ("OneD", "revenue", Decimal(100)),
        ("OneI", "total_assets", Decimal(500)),
    }
    assert document.basis == "standalone"


@pytest.mark.parametrize(
    ("element", "unit", "instant", "dimensional", "statement"),
    [
        ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "inr", 0, 0, "cf"),
        ("PurchasesOfStockInTrade", "inr", False, False, "pl"),
        ("AdjustmentsForFinanceCosts", "inr", False, False, "cf"),
        ("ProvisionsNoncurrent", "inr", True, False, "bs"),
        ("SegmentRevenue", "inr", False, True, "segment"),
        ("CET1Ratio", "pure", False, False, "other"),
        ("OtherExpenses", "inr", False, True, "other"),
    ],
)
def test_nse_statement(
    element: str, unit: str, instant: bool, dimensional: bool, statement: str
) -> None:
    assert nse_statement(element, unit, bool(instant), dimensional) == statement


# --- the secondary source ---------------------------------------------------------------------


def test_page_units_and_basis() -> None:
    figures = parse_page(
        (FIXTURES / "secondary_page_reliance_consolidated.html").read_bytes(), date(2026, 10, 28)
    )
    by = {(x.section, x.label, x.period_end): x for x in figures}
    capital = by[("balance-sheet", "Equity Capital", date(2015, 3, 31))]
    assert (capital.item, capital.value, capital.unit, capital.period_kind) == (
        "share_capital",
        2943 * CRORE,
        "inr",
        "instant",
    )
    promoters = by[("shareholding", "Promoters +", date(2023, 12, 31))]
    assert (promoters.item, promoters.value, promoters.unit) == (
        "promoters_pct",
        Decimal("0.5030"),
        "fraction",
    )
    holders = by[("shareholding", "No. of Shareholders", date(2023, 12, 31))]
    assert (holders.value, holders.unit) == (Decimal(3613814), "count")
    cap = by[("top-ratios", "Market Cap", date(2026, 10, 28))]
    assert (cap.item, cap.value) == ("market_cap", 1623904 * CRORE)
    face = by[("top-ratios", "Face Value", date(2026, 10, 28))]
    assert (face.item, face.value, face.unit) == ("face_value", Decimal("10.0"), "inr_per_share")
    assert {x.basis for x in figures} == {"consolidated"}


def test_page_quarters() -> None:
    figures = parse_page(
        (FIXTURES / "secondary_quarters_reliance_consolidated.html").read_bytes(), date(2026, 1, 1)
    )
    items = {x.item for x in figures if x.section == "quarters"}
    assert {"revenue", "opm", "net_profit", "eps_basic", "tax_rate"} <= items
    opm = next(x for x in figures if x.item == "opm")
    assert opm.unit == "fraction"
    assert opm.value < 1


@pytest.mark.parametrize(
    ("text", "key", "end"),
    [
        ("Mar 2015", "2015-03-31", date(2015, 3, 31)),
        ("Dec 2023", None, date(2023, 12, 31)),
        ("Feb 2024", None, date(2024, 2, 29)),
        ("TTM", "TTM", None),
        ("", None, None),
    ],
)
def test_column_date(text: str, key: str | None, end: date | None) -> None:
    assert column_date(text, key) == end


def test_cell_value() -> None:
    assert cell_value("profit-loss", "sales", "1,23,456") == (123456 * CRORE, "inr")
    assert cell_value("profit-loss", "opm %", "18%") == (Decimal("0.18"), "fraction")
    assert cell_value("quarters", "eps in rs", "-3.5") == (Decimal("-3.5"), "inr_per_share")
    assert cell_value("ratios", "debtor days", "12") == (Decimal(12), "days")
    assert cell_value("profit-loss", "sales", "") is None
    assert screener_label("Net Profit\xa0+") == "net profit"


def _figure(
    end: date, statement: str = "pl", kind: str = "quarter", basis: str = "consolidated"
) -> Figure:
    return Figure("quarters", statement, kind, end, "Sales", "revenue", Decimal(1), "inr", basis)


def _nse(end: date, at: datetime, basis: str = "consolidated") -> FilingOut:
    return FilingOut(
        end, basis, "nse", "x", "indas", 1, PublicAt(at, "announcement", False), at, "", "", []
    )


def test_screener_public_at() -> None:
    announced = datetime(2024, 7, 19, 13, 46, tzinfo=UTC)
    nse = {date(2024, 6, 30): [_nse(date(2024, 6, 30), announced)]}
    got = screener_public_at([_figure(date(2024, 6, 30))], nse, "consolidated", FETCHED)
    assert got == PublicAt(announced, "announcement", False)
    quarter = screener_public_at([_figure(date(2018, 6, 30))], {}, "consolidated", FETCHED)
    assert quarter == PublicAt(datetime(2018, 8, 14, 23, 59, tzinfo=IST), "deadline", True)
    year = screener_public_at(
        [_figure(date(2018, 3, 31), "pl", "annual")], {}, "consolidated", FETCHED
    )
    assert year.at == datetime(2018, 5, 30, 23, 59, tzinfo=IST)
    holding = screener_public_at(
        [_figure(date(2018, 12, 31), "shareholding", "instant")], {}, "consolidated", FETCHED
    )
    assert holding.at == datetime(2019, 1, 21, 23, 59, tzinfo=IST)
    recent = screener_public_at([_figure(date(2026, 9, 30))], {}, "consolidated", FETCHED)
    assert recent == PublicAt(FETCHED, "observed", True)  # seen before its deadline
    snapshot = screener_public_at(
        [_figure(date(2026, 10, 28), "snapshot", "instant")], {}, "consolidated", FETCHED
    )
    assert snapshot == PublicAt(FETCHED, "observed", False)


def test_screener_filings_consolidated_first() -> None:
    q1, q2 = date(2024, 6, 30), date(2024, 9, 30)
    pages = [
        ("u1", "s1", FETCHED, [_figure(q1, basis="standalone"), _figure(q2, basis="standalone")]),
        ("u2", "s2", FETCHED, [_figure(q2, basis="standalone")]),
    ]
    out = screener_filings(pages, [], [q1])
    assert [(x.period_end, x.basis, x.source_url) for x in out] == [(q2, "standalone", "u1")]


def test_copy_text() -> None:
    assert copy_text(None) == "\\N"
    assert copy_text(True) == "t"
    assert copy_text("a\tb\\c\n") == "a\\tb\\\\c\\n"


# --- formulas ---------------------------------------------------------------------------------


def test_formulas() -> None:
    assert f.cagr(Decimal(200), Decimal(100), 1) == pytest.approx(Decimal(1))
    assert f.cagr(Decimal(121), Decimal(100), 2).quantize(Decimal("0.0001")) == Decimal("0.1000")
    assert f.cagr(Decimal(121), Decimal(-100), 2) is None
    assert f.growth(Decimal(110), Decimal(100)) == Decimal("0.1")
    assert f.growth(Decimal(110), Decimal(-5)) is None
    assert f.owners_profit(Decimal(0), Decimal(50)) == Decimal(50)
    assert f.owners_profit(Decimal(40), Decimal(50)) == Decimal(40)
    assert f.equity(None, Decimal(100), Decimal(10), None, None) == Decimal(90)
    assert f.equity(None, None, None, Decimal(5), Decimal(95)) == Decimal(100)
    assert f.borrowings(None, Decimal(3), Decimal(7), Decimal(5), None) == Decimal(15)
    assert f.operating_profit(Decimal(100), Decimal(80), Decimal(5), Decimal(10)) == Decimal(35)
    assert f.days(Decimal(10), Decimal(365)) == Decimal(10)
    assert f.shares(Decimal(1000), Decimal(10)) == Decimal(100)
    assert f.free_cash_flow(Decimal(50), Decimal(-20)) == Decimal(30)
    assert f.dividends(Decimal(-12), None, None) == Decimal(12)
    assert f.dividends(None, Decimal("0.1"), Decimal(200)) == Decimal(20)
    assert f.positive_ratio(Decimal(1), Decimal(-1)) is None
    assert f.total([Decimal(1), None]) is None


# --- the book ---------------------------------------------------------------------------------

Q = [date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31), date(2024, 3, 31)]


def _q(end: date, item: str, value: int, kind: str = "quarter") -> KnownFact:
    return KnownFact(kind, end, item, Decimal(value))


def test_book_versions_and_sources() -> None:
    book = Book()
    book.add("screener", "consolidated", Q[0], [_q(Q[0], "revenue", 90)])
    book.add("nse", "consolidated", Q[0], [_q(Q[0], "revenue", 100), _q(Q[0], "pbt", 9)])
    assert book.get("consolidated", "quarter", Q[0], "revenue") == Decimal(100)
    book.add("nse", "consolidated", Q[0], [_q(Q[0], "revenue", 101)])  # version 2 replaces
    assert book.get("consolidated", "quarter", Q[0], "revenue") == Decimal(101)
    assert book.get("consolidated", "quarter", Q[0], "pbt") is None


def test_book_derived_q4_and_ttm() -> None:
    book = Book()
    for end, value in zip(Q[:3], (10, 20, 30), strict=True):
        book.add("nse", "standalone", end, [_q(end, "revenue", value)])
    book.add(
        "nse",
        "standalone",
        Q[2],
        [_q(Q[2], "revenue", 30), _q(Q[2], "revenue", 60, "ytd")],
    )
    book.add("nse", "standalone", Q[3], [_q(Q[3], "revenue", 100, "annual")])
    assert book.quarter("standalone", Q[3], "revenue") == Decimal(40)
    assert book.ttm("standalone", Q[3], "revenue") == Decimal(100)
    assert months_before(date(2024, 2, 29), 12) == date(2023, 2, 28)


def test_metrics_valuation() -> None:
    book = Book()
    for n, end in enumerate(Q):
        book.add(
            "nse",
            "consolidated",
            end,
            [
                _q(end, "revenue", 1000),
                _q(end, "net_profit", 100),
                _q(end, "paid_up_capital", 1000),
                _q(end, "face_value", 10),
                *([_q(end, "equity_owners", 4000, "instant")] if n == 3 else []),
            ],
        )
    quote = Quote(date(2024, 5, 1), Decimal(40))
    found = {m.name: m.value for m in metrics(book, date(2024, 5, 1), quote)}
    assert found["ttm_net_profit"] == Decimal(400)
    assert found["shares"] == Decimal(100)
    assert found["market_cap"] == Decimal(4000)
    assert found["pe"] == Decimal(10)
    assert found["pb"] == Decimal(1)
    assert found["roe"] == Decimal("0.1")
    assert found["npm"] == Decimal("0.1")
    # a 1:1 bonus after the share count's period end: twice the shares at the post-bonus close
    ex = date(2024, 4, 15)
    split = Quote(date(2024, 5, 1), Decimal(20), ((ex, Decimal("0.5"), ex),))
    after = {m.name: m.value for m in metrics(book, date(2024, 5, 1), split)}
    assert (after["shares"], after["market_cap"]) == (Decimal(200), Decimal(4000))
    # a factor known only after the close's session: the share count is not yet known
    pending = Quote(date(2024, 5, 1), Decimal(20), ((ex, Decimal("0.5"), date(2024, 5, 2)),))
    unknown = {m.name: m.value for m in metrics(book, date(2024, 5, 1), pending)}
    assert {"shares", "market_cap", "pe"}.isdisjoint(unknown)


def test_metric_history_is_point_in_time() -> None:
    def at(day: int) -> datetime:
        return datetime(2024, 5, day, 12, tzinfo=UTC)

    filings = [
        StoredFiling("a", "nse", "standalone", Q[2], 1, at(1), "indas"),
        StoredFiling("b", "nse", "standalone", Q[3], 1, at(10), "indas"),
    ]
    facts = {"a": [_q(Q[2], "revenue", 100)], "b": [_q(Q[3], "revenue", 150)]}
    rows = metric_history(filings, facts, Prices())
    qoq = [(r.as_of, r.metric.value) for r in rows if r.metric.name == "revenue_qoq"]
    assert qoq == [(at(10), Decimal("0.5"))]  # nothing before the second quarter is public
    assert close_known_on(datetime(2024, 5, 2, 9, 0, tzinfo=IST)) == date(2024, 5, 1)
    assert close_known_on(datetime(2024, 5, 2, 15, 30, tzinfo=IST)) == date(2024, 5, 2)
    prices = Prices([date(2024, 5, 1), date(2024, 5, 3)], [Decimal(1), Decimal(2)])
    assert prices.at(date(2024, 5, 2)) == Quote(date(2024, 5, 1), Decimal(1))
    assert prices.at(date(2024, 4, 30)) is None


def test_agreement_pairs_only_quarters_both_sources_give() -> None:
    end = date(2024, 6, 30)
    rows = [
        (1, "consolidated", end, "revenue", "nse", Decimal(100)),
        (1, "consolidated", end, "revenue", "screener", Decimal(101)),
        (1, "standalone", end, "revenue", "screener", Decimal(90)),  # no NSE figure
        (2, "consolidated", end, "pbt", "nse", Decimal(5)),  # no secondary figure
    ]
    assert pairs(rows) == [("revenue", Decimal(100), Decimal(101))]
