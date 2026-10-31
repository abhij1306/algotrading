"""The fundamentals history's pure seams: NSE's HTML results pages (trimmed real pages), the
backfill's document choice, and the research panel's timing, shares, flags, coverage and
restatement counts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.hat.data.fundamentals.book import Book, KnownFact
from app.hat.data.fundamentals.nse_html import dated, item_of, label_key, number, parse_page
from app.hat.data.fundamentals.research import (
    Action,
    Pair,
    StoredFact,
    book_value,
    carried,
    coverage,
    fiscal_year,
    known_filings,
    research_known,
    restatement,
    screener_shares,
    worst,
)
from app.hat.data.results.fetch import history_documents
from app.hat.data.results.nse import LISTING_ANNUAL, Filing, parse_announcements
from app.hat.time import IST

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_results"
LAKH = Decimal(10) ** 5


def _items(name: str) -> dict[str, tuple[str, Decimal]]:
    page = parse_page((FIXTURES / name).read_bytes())
    return {x.item: (x.period_kind, x.value) for x in page.facts if x.item}


def test_annual_page_gives_the_year_and_its_balances() -> None:
    page = parse_page((FIXTURES / "html_persistent_fy2016_annual.html").read_bytes())
    assert (page.symbol, page.basis, page.period_end, page.annual) == (
        "PERSISTENT",
        "standalone",
        date(2016, 3, 31),
        True,
    )
    items = _items("html_persistent_fy2016_annual.html")
    assert items["revenue"] == ("annual", Decimal("144713.60") * LAKH)
    assert items["net_profit"] == ("annual", Decimal("26546.10") * LAKH)
    assert items["share_capital"] == ("instant", Decimal("8000.00") * LAKH)
    assert items["reserves"] == ("instant", Decimal("12748.76") * LAKH)
    assert items["eps_basic"] == ("annual", Decimal("33.26"))
    assert "paid_up_capital" not in items  # a year's paid-up capital is its balance


def test_bank_quarter_page() -> None:
    page = parse_page((FIXTURES / "html_bank_quarter.html").read_bytes())
    assert (page.symbol, page.family, page.annual) == ("ICICIBANK", "bank", False)
    items = _items("html_bank_quarter.html")
    assert items["revenue"] == ("quarter", Decimal("1348193.00") * LAKH)
    assert items["finance_cost"] == ("quarter", Decimal("807742.00") * LAKH)
    assert items["paid_up_capital"] == ("quarter", Decimal("116317.00") * LAKH)
    assert items["face_value"] == ("quarter", Decimal(2))
    units = {x.element: x.unit for x in page.facts}
    assert units["Capital Adequacy Ratio"] == "pure"
    assert units["% of Gross/Net NPA"] == "pure"


def test_labels_map_by_whole_pattern_and_rank() -> None:
    assert item_of("(f) Finance costs", "Expenses") == ("finance_cost", 10)
    assert item_of("Tax expense of discontinued operations", "Expenses") is None
    assert item_of("(a) Basic", "Earnings per share (before extraordinary items)")
    assert item_of("(a) Basic", "Earnings per share (after extraordinary items)") is None
    assert item_of("Total income from operations (net)", "Segment Revenue") is None
    better = item_of("Basic EPS for continued and discontinued operations", "")
    fallback = item_of("Basic EPS for continuing operations", "")
    assert better is not None
    assert fallback is not None
    assert better[0] == fallback[0] == "eps_basic"
    assert better[1] < fallback[1]
    assert label_key("ii. Other income") == "other income"
    assert number("(1,234.50)") == Decimal("-1234.50")
    assert number("-") is None


def test_header_typo_takes_the_listing_period() -> None:
    page = parse_page((FIXTURES / "html_bank_quarter.html").read_bytes())
    broadcast = datetime(2017, 5, 3, 18, 0, tzinfo=IST)
    moved = dated(page, date(2017, 3, 31), broadcast)
    assert moved.period_end == date(2017, 3, 31)
    assert {x.period_end for x in moved.facts} == {date(2017, 3, 31)}
    assert dated(page, date(2018, 3, 31), broadcast) is page  # listing end after the broadcast


def _filing(listing: str, end: date, basis: str, xbrl: str | None, html: str | None) -> Filing:
    return Filing(listing, "X", "", "1", end, basis, None, None, xbrl, False, "", html)


def test_history_documents_quarters_by_rule_years_in_both_bases() -> None:
    q, y = date(2015, 12, 31), date(2015, 3, 31)
    filings = [
        _filing("reg33", q, "consolidated", None, "c.html"),
        _filing("reg33", q, "standalone", None, "s.html"),
        _filing(LISTING_ANNUAL, y, "consolidated", None, "yc.html"),
        _filing(LISTING_ANNUAL, y, "standalone", None, "ys.html"),
        _filing(LISTING_ANNUAL, date(2019, 3, 31), "standalone", "y.xml", None),
        _filing("reg33", date(2010, 3, 31), "standalone", None, "old.html"),
    ]
    got = history_documents(filings, date(2012, 3, 31), date(2020, 3, 31), "consolidated-first")
    assert sorted(f.html_url or "" for f in got) == ["c.html", "yc.html", "ys.html"]


def test_announcements_day_with_none() -> None:
    assert parse_announcements(b'{"data": [], "msg": "no data found"}') == []


def test_screener_cash_flow_waits_for_the_annual_report() -> None:
    public = datetime(2016, 5, 20, 12, 0, tzinfo=IST)
    end = date(2016, 3, 31)
    assert research_known("screener", False, public, "pl", "revenue", end) == public
    assert research_known("screener", False, public, "bs", "borrowings", end) == public
    late = research_known("screener", False, public, "cf", "cfo", end)
    assert late.astimezone(IST).date() == date(2016, 9, 30)
    assert research_known("screener", False, public, "cf", "cfo", date(2021, 3, 31)) == public
    assert research_known("nse_html", True, public, "cf", "cfo", end) == public
    facts = [
        StoredFact("pl", "annual", end, "revenue", Decimal(1)),
        StoredFact("cf", "annual", end, "cfo", Decimal(2)),
        StoredFact("ratio", "annual", end, "roce", Decimal(3)),
    ]
    known = known_filings("screener", False, public, "standalone", end, 1, facts)
    assert [(k.source, len(k.facts)) for k in known] == [("screener", 1), ("screener_late", 1)]


def test_shares_undo_later_splits_and_carry_actions() -> None:
    actions = [
        Action(date(2017, 6, 1), Decimal("0.5"), True),  # 2:1 split
        Action(date(2019, 6, 1), Decimal("0.5"), False),  # 1:1 bonus
    ]
    # capital 100 at year end 2016, today's face value 5 (was 10): 10 shares then
    count = screener_shares(Decimal(100), Decimal(5), date(2016, 3, 31), date(2026, 10, 1), actions)
    assert count == Decimal(10)
    assert carried(Decimal(10), date(2016, 3, 31), date(2018, 1, 1), actions) == Decimal(20)
    assert carried(Decimal(10), date(2016, 3, 31), date(2020, 1, 1), actions) == Decimal(40)


def test_flags_and_book_value_check() -> None:
    assert worst(["nse", "nse_html", "screener_late"]) == "screener_late"
    assert worst(["nse", None]) is None
    end = date(2016, 3, 31)
    book = Book()
    book.add(
        "nse_html",
        "standalone",
        end,
        [
            KnownFact("instant", end, "share_capital", Decimal(10)),
            KnownFact("instant", end, "reserves", Decimal(90)),
        ],
    )
    book.add("screener", "standalone", end, [KnownFact("instant", end, "reserves", Decimal(95))])
    assert book_value(book, "standalone", end) == (Decimal(100), "nse_html")
    restated = [
        KnownFact("instant", end, "share_capital", Decimal(10)),
        KnownFact("instant", end, "reserves", Decimal(9000)),
    ]
    book.add("screener", "standalone", end, restated)
    assert book_value(book, "standalone", end) == (Decimal(9010), "screener")


def test_coverage_counts_member_month_ends() -> None:
    days = [date(2015, 3, 31), date(2015, 4, 30)]
    members = {"a": [(date(2015, 1, 1), date(2015, 4, 15))], "b": [(date(2014, 1, 1), None)]}
    rows = {("a", days[0]): {"revenue": 1}, ("b", days[1]): {"revenue": 2}}
    got = coverage(members, rows, days, ["revenue"])
    assert got == {2015: {"revenue": (1, 2)}, 2016: {"revenue": (1, 1)}}
    assert fiscal_year(date(2016, 3, 31)) == 2016
    assert fiscal_year(date(2016, 4, 1)) == 2017


def test_restatement_counts() -> None:
    end = date(2016, 3, 31)
    pairs = [
        Pair("a", end, "standalone", "net_profit", Decimal(100), Decimal(100)),
        Pair("b", end, "standalone", "net_profit", Decimal(100), Decimal(110)),
        Pair("c", end, "standalone", "net_profit", Decimal(100), Decimal(-5)),
    ]
    got = restatement(pairs)[(2016, "net_profit")]
    assert got == {"n": 3, ">1%": 2, ">5%": 2, ">20%": 1, "sign": 1}
