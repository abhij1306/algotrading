"""The financial-results store's pure seams (plan A1-A3): the XBRL parser's rules on recorded NSE
documents (`fixtures/nse_results/`, real documents trimmed to the facts read), the timing rules,
point-in-time growth and `results_as_of`. No network, no pipeline run."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest
from hat_support import memory_engine, memory_session

from app.hat.data.results import growth, nse, timing, xbrl
from app.hat.data.results.derive import results_as_of
from app.hat.data.results.growth import ResultRow
from app.hat.data.results.ingest import document_period
from app.hat.data.results.raw import RawCache
from app.hat.data.results.secondary import page_url, parse_page
from app.hat.data.results.universe import passing
from app.hat.models import HatFinancialResult
from app.hat.time import IST

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nse_results"
CR = Decimal(10) ** 7


def _doc(name: str) -> xbrl.Parsed:
    return xbrl.parse((FIXTURES / name).read_bytes())


def _ist(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=IST)  # type: ignore[misc]


# --- A2 rule 1: the quarter is the One context, never the year to date ---------------------


def test_results__q3_quarter_is_not_the_year_to_date() -> None:
    """RELIANCE Dec 2020 consolidated: the nine months are about 2.7x the quarter; the quarter
    must be read from `OneD`, the nine months from `FourD`."""
    doc = _doc("reg33_indas_q3_reliance.xml")
    assert doc.quarter is not None
    assert doc.ytd is not None
    assert (doc.period_start, doc.period_end) == (date(2020, 10, 1), date(2020, 12, 31))
    assert doc.quarter.revenue == Decimal("123997") * CR
    assert doc.ytd.revenue == Decimal("331430") * CR
    assert doc.ytd_start == date(2020, 4, 1)
    assert 2.5 < doc.ytd.revenue / doc.quarter.revenue < 3
    assert doc.quarter.net_profit == Decimal("13101") * CR  # attributable to owners
    assert (doc.quarter.eps_basic, doc.quarter.eps_diluted) == (Decimal("20.33"), Decimal("19.93"))


# --- A2 rule 2: banks and NBFCs -------------------------------------------------------------


def test_results__bank_interest_earned_is_revenue() -> None:
    """FEDERALBNK Dec 2021 (Reg 33 banking): no revenue-from-operations tag, so interest earned
    is the revenue; the document gives only the period end, so the quarter starts 3 months
    earlier."""
    doc = _doc("reg33_banking_q3_federalbnk.xml")
    assert doc.kind.format == "reg33_banking"
    assert doc.quarter is not None
    assert doc.quarter.revenue == Decimal("3442.56") * CR
    assert doc.quarter.net_profit == Decimal("521.73") * CR
    assert doc.quarter.eps_basic == Decimal("2.48")
    assert (doc.period_start, doc.period_end) == (date(2021, 10, 1), date(2021, 12, 31))


def test_results__nbfc_revenue_from_operations_wins() -> None:
    """MANAPPURAM Dec 2021 (NBFC template): both tags present; revenue from operations (which
    includes the interest) is the revenue, not interest earned."""
    doc = _doc("reg33_nbfc_q3_manappuram.xml")
    assert doc.kind.format == "reg33_nbfc"
    assert doc.quarter is not None
    assert doc.quarter.revenue == Decimal("1484.45") * CR
    assert doc.quarter.revenue != Decimal("1446.13") * CR  # InterestEarned
    assert doc.quarter.net_profit == Decimal("261") * CR


# --- A2 rule 3: the period is the document's ------------------------------------------------


def test_results__period_from_the_document_mismatch_flagged() -> None:
    assert document_period(date(2024, 12, 31), date(2024, 12, 31)) == (date(2024, 12, 31), False)
    assert document_period(date(2024, 9, 30), date(2024, 12, 31)) == (date(2024, 9, 30), True)
    assert document_period(None, date(2024, 12, 31)) == (date(2024, 12, 31), False)


# --- A2 rule 4: Q4 from the year and nine months --------------------------------------------


def test_results__q4_with_only_the_year_is_derived() -> None:
    """Built from two recorded HDFCBANK standalone documents: the year to Mar 2024 as the only
    period of a Q4 document, and the nine months to Dec 2023 in a context of its own. Q4 = year
    - nine months equals the quarter the real Mar 2024 document reports (interest earned
    71,472.75 crore, total income 89,639.00, profit 16,511.85); EPS, not additive across a
    changing share count, differs by 3 paise (21.77 against the reported 21.74)."""
    doc = _doc("reg33_banking_q4_annual_only_hdfcbank.xml")
    assert doc.quarter is None
    assert doc.annual is not None
    assert doc.nine_month is not None
    assert (doc.period_start, doc.period_end) == (date(2024, 1, 1), date(2024, 3, 31))
    q4 = growth.derive_q4(doc.annual, doc.nine_month)
    assert q4.revenue == Decimal("71472.75") * CR
    assert q4.total_income == Decimal("89639.00") * CR
    assert q4.net_profit == Decimal("16511.85") * CR
    assert q4.eps_basic == Decimal("21.77")


def test_results__q1_equal_to_ytd_is_not_annual() -> None:
    """In Q1 the quarter equals the year to date legitimately; only a period ending the year is
    read as annual."""
    doc = _doc("reg33_indas_q3_reliance.xml")
    assert doc.annual is None


# --- A2 rule 5: units -----------------------------------------------------------------------


def test_results__unit_suspect_20x_either_way() -> None:
    prev = [Decimal(100), Decimal(110), Decimal(90), Decimal(105)]
    assert growth.unit_suspect(Decimal(2500), prev)  # 24x the median
    assert growth.unit_suspect(Decimal(4), prev)  # 1/25
    assert not growth.unit_suspect(Decimal(1500), prev)  # 15x
    assert not growth.unit_suspect(Decimal(2500), [Decimal(100)])  # too little history
    assert not growth.unit_suspect(None, prev)


def test_results__money_must_be_inr() -> None:
    body = (FIXTURES / "reg33_banking_q3_federalbnk.xml").read_bytes()
    usd = body.replace(b"iso4217:INR</xbrli:measure>", b"iso4217:USD</xbrli:measure>")
    with pytest.raises(xbrl.XbrlError, match="unit_not_inr"):
        xbrl.parse(usd)


# --- A2 rule 6: Reg 33 and Integrated into one schema; insurers -----------------------------


@pytest.mark.parametrize(
    ("reg33", "integrated"),
    [
        ("reg33_indas_q3_reliance.xml", "integrated_indas_q3_reliance.xml"),
        ("reg33_banking_q3_federalbnk.xml", "integrated_banking_q3_federalbnk.xml"),
    ],
)
def test_results__reg33_and_integrated_fill_the_same_fields(reg33: str, integrated: str) -> None:
    old, new = _doc(reg33), _doc(integrated)
    assert old.kind.format.removeprefix("reg33_") == new.kind.format.removeprefix("integrated_")
    for doc in (old, new):
        assert doc.quarter is not None
        assert doc.ytd is not None
        assert all(v is not None for v in vars(doc.quarter).values()), doc.kind
        assert doc.period_start is not None
        assert doc.basis in {"standalone", "consolidated"}


def test_results__integrated_reliance_dec_2025() -> None:
    doc = _doc("integrated_indas_q3_reliance.xml")
    assert doc.quarter is not None
    assert doc.quarter.revenue == Decimal("269496") * CR
    assert doc.quarter.eps_basic == Decimal("13.78")


def test_results__insurer_is_metadata_only() -> None:
    doc = _doc("integrated_li_q3_hdfclife.xml")
    assert doc.kind.format == "insurance_skipped"
    assert doc.quarter is None
    assert doc.period_end == date(2025, 12, 31)


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("reg33_indas", "reg33_indas"),
        ("integrated_nbfc", "integrated_nbfc"),
        ("reg33_insurance", "insurance_skipped"),
        ("integrated_li", "insurance_skipped"),
        ("integrated_gi", "insurance_skipped"),
        ("reg33_non_indas", "other"),
        ("integrated_otherthanbank", "other"),
    ],
)
def test_results__store_format(raw: str, stored: str) -> None:
    assert xbrl.store_format(raw) == stored


def test_results__doctype_refused() -> None:
    with pytest.raises(xbrl.XbrlError, match="xbrl_doctype"):
        xbrl.read_facts(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>')


# --- listings and announcements -------------------------------------------------------------


def test_results__listings() -> None:
    xbrl_row, gap, old = nse.parse_results_listing((FIXTURES / "results_listing.json").read_bytes())
    assert (xbrl_row.period_end, xbrl_row.basis, xbrl_row.isin) == (
        date(2024, 12, 31),
        "standalone",
        "INE002A01018",
    )
    assert xbrl_row.broadcast_at == _ist(2025, 1, 16, 20, 20, 21)
    assert gap.xbrl_url is None
    assert old.xbrl_url is None
    rows, total = nse.parse_integrated_listing((FIXTURES / "integrated_listing.json").read_bytes())
    assert total == 19
    assert [(r.basis, r.revision, r.created_at) for r in rows] == [
        ("consolidated", False, _ist(2026, 7, 17, 19, 50, 4)),
        ("standalone", False, _ist(2026, 7, 17, 19, 49, 6)),
    ]
    (insurer,) = nse.parse_results_listing(
        (FIXTURES / "insurance_listing.json").read_bytes(), nse.LISTING_INSURANCE
    )
    assert (insurer.period_end, insurer.listing) == (date(2024, 12, 31), "insurance")


def test_results__results_announcement_rules() -> None:
    items = nse.parse_announcements((FIXTURES / "announcements.json").read_bytes())
    found = timing.results_announcements(items)
    assert [i.at for i in found["RELIANCE"]] == [_ist(2025, 1, 16, 19, 25, 42)]
    board = nse.Announcement("X", _ist(2025, 1, 16, 18, 0), "Outcome of Board Meeting", "results")
    other = nse.Announcement("X", _ist(2025, 1, 15, 18, 0), "Outcome of Board Meeting", "NCD issue")
    update = nse.Announcement("X", _ist(2025, 1, 16, 19, 0), "Financial Result Updates", "")
    assert nse.is_results_announcement(other) == ""
    end = date(2024, 12, 31)
    assert timing.match_announcement([other, board, update], date(2025, 1, 16), end) == update
    assert timing.match_announcement([other, board], date(2025, 1, 16), end) == board
    assert timing.match_announcement([board], date(2025, 1, 20), end) is None  # outside window
    assert timing.match_announcement([board], date(2025, 1, 16), date(2025, 1, 31)) is None


# --- timestamps -----------------------------------------------------------------------------


def _filing(url: str, broadcast: datetime | None, created: datetime | None = None) -> nse.Filing:
    return nse.Filing(
        "reg33", "X", "", "1", date(2026, 6, 30), "", broadcast, created, url, False, ""
    )


STAMPED = "https://x/INTEGRATED_FILING_INDAS_1695741_17072026075004_WEB.xml"  # filed 19:50:04


def test_results__name_stamp_time_is_never_used() -> None:
    """The stamp's clock is 12-hour without AM/PM: only its date counts."""
    assert timing.stamp_date(STAMPED) == date(2026, 7, 17)
    assert timing.stamp_date("https://x/corporate/xbrl/-") is None
    same_day = timing.xbrl_at(_filing(STAMPED, _ist(2026, 7, 17, 19, 50, 4)))
    assert same_day == timing.FiledAt(_ist(2026, 7, 17, 19, 50, 4), False)
    rebroadcast = timing.xbrl_at(_filing(STAMPED, _ist(2026, 8, 30, 10, 0)))
    assert rebroadcast == timing.FiledAt(_ist(2026, 7, 17, 23, 59, 59), True)
    unlisted = timing.xbrl_at(_filing(STAMPED, None))
    assert unlisted is not None
    assert unlisted.at.time() != datetime(2026, 7, 17, 7, 50, 4).time()
    created = timing.xbrl_at(_filing(STAMPED, None, _ist(2026, 7, 17, 19, 50, 4)))
    assert created == timing.FiledAt(_ist(2026, 7, 17, 19, 50, 4), False)


def test_results__public_at() -> None:
    filed = timing.FiledAt(_ist(2026, 7, 17, 20, 30), False)
    announced = _ist(2026, 7, 17, 19, 25)
    assert timing.public_at(1, announced, filed) == timing.PublicAt(
        announced, "announcement", False
    )
    assert timing.public_at(1, _ist(2026, 7, 18, 9), filed) == timing.PublicAt(
        filed.at, "xbrl", False
    )
    assert timing.public_at(2, announced, filed) == timing.PublicAt(filed.at, "xbrl", False)
    late = timing.FiledAt(_ist(2026, 7, 18, 23, 59, 59), True)
    assert timing.public_at(1, None, late) == timing.PublicAt(late.at, "xbrl_date", True)
    assert timing.public_at(1, None, None) is None


def test_results__announcement_read_12_hours_early() -> None:
    """PERSISTENT 24-Apr-2025: board outcome stamped 05:08, XBRL 20:32 the same day."""
    filed = timing.FiledAt(_ist(2025, 4, 24, 20, 32), False)
    assert timing.pm_fix(_ist(2025, 4, 24, 5, 8), filed) == _ist(2025, 4, 24, 17, 8)
    assert timing.pm_fix(_ist(2025, 4, 24, 10, 0), filed) == _ist(2025, 4, 24, 10, 0)
    estimated = timing.FiledAt(filed.at, True)
    assert timing.pm_fix(_ist(2025, 4, 24, 5, 8), estimated) == _ist(2025, 4, 24, 5, 8)


def test_results__deadline_and_quarter_announcement() -> None:
    assert timing.deadline(date(2025, 3, 31)) == _ist(2025, 5, 30, 23, 59)
    assert timing.deadline(date(2025, 6, 30)) == _ist(2025, 8, 14, 23, 59)
    a = nse.Announcement("X", _ist(2025, 8, 1, 16, 0), "Financial Result Updates", "")
    b = nse.Announcement("X", _ist(2025, 8, 9, 16, 0), "Financial Result Updates", "")
    assert timing.quarter_announcement([a, b], date(2025, 6, 30), set()) == a
    assert timing.quarter_announcement([a, b], date(2025, 6, 30), {a.at}) == b
    assert timing.quarter_announcement([a], date(2025, 9, 30), set()) is None


# --- A3: point-in-time growth ---------------------------------------------------------------

T0 = _ist(2025, 7, 20, 18, 0)


def _row(
    end: date,
    basis: str = "consolidated",
    revenue: str | None = "100",
    eps: str | None = "1",
    *,
    at: datetime = T0,
    version: int = 1,
    source: str = "nse",
    fmt: str = "reg33_indas",
    rid: str | None = None,
) -> ResultRow:
    return ResultRow(
        rid or f"{end}-{basis}-{source}-{version}",
        end,
        basis,
        source,
        fmt,
        version,
        at,
        None if revenue is None else Decimal(revenue),
        None if eps is None else Decimal(eps),
    )


Q, P = date(2025, 6, 30), date(2024, 6, 30)


def test_results__basis_rule_never_mixes() -> None:
    both = [_row(Q, revenue="150", eps="2"), _row(P), _row(Q, "standalone"), _row(P, "standalone")]
    found = growth.growth_as_of(both, Q, T0, set())
    assert (found.basis, found.sales_yoy, found.eps_yoy) == (
        "consolidated",
        Decimal("0.5"),
        Decimal(1),
    )
    no_prior_consolidated = [_row(Q), _row(Q, "standalone", "130"), _row(P, "standalone")]
    found = growth.growth_as_of(no_prior_consolidated, Q, T0, set())
    assert (found.basis, found.sales_yoy) == ("standalone", Decimal("0.3"))
    mixed = [_row(Q), _row(P, "standalone")]
    found = growth.growth_as_of(mixed, Q, T0, set())
    assert (found.basis, found.sales_yoy, found.reason) == (None, None, "no_common_basis")


def test_results__eps_growth_and_turnaround() -> None:
    loss_to_profit = growth.growth_as_of([_row(Q, eps="2"), _row(P, eps="-1")], Q, T0, set())
    assert (loss_to_profit.eps_yoy, loss_to_profit.turnaround) == (None, True)
    assert loss_to_profit.reason == "eps_not_positive"
    zero_prior = growth.growth_as_of([_row(Q, eps="2"), _row(P, eps="0")], Q, T0, set())
    assert (zero_prior.eps_yoy, zero_prior.turnaround) == (None, True)
    profit_to_loss = growth.growth_as_of([_row(Q, eps="-2"), _row(P, eps="1")], Q, T0, set())
    assert (profit_to_loss.eps_yoy, profit_to_loss.turnaround) == (None, False)
    no_sales = growth.growth_as_of([_row(Q), _row(P, revenue="0")], Q, T0, set())
    assert no_sales.sales_yoy is None
    assert no_sales.reason == "prior_revenue_not_positive"


def test_results__unit_suspect_row_is_left_out() -> None:
    rows = [_row(Q, rid="bad"), _row(P), _row(Q, "standalone", "120"), _row(P, "standalone")]
    found = growth.growth_as_of(rows, Q, T0, {"bad"})
    assert (found.basis, found.sales_yoy) == ("standalone", Decimal("0.2"))
    history = [
        _row(date(2024, m, d), revenue="100", at=T0 - timedelta(days=400))
        for m, d in ((6, 30), (9, 30), (12, 31))
    ]
    history.append(_row(date(2025, 3, 31), revenue="100", at=T0 - timedelta(days=100)))
    assert growth.suspect_row(_row(Q, revenue="2500"), history)
    assert not growth.suspect_row(_row(Q, revenue="250"), history)


def test_results__known_at_uses_only_what_was_public() -> None:
    """A revision public later replaces the original only from its own public_at; a secondary
    row stands only until an NSE row of its key is public; insurers never appear."""
    t1, t2 = T0, T0 + timedelta(days=30)
    rows = [
        _row(Q, revenue="100", at=t1, rid="v1"),
        _row(Q, revenue="90", at=t2, version=2, rid="v2"),
        _row(P, "standalone", at=t1 - timedelta(days=365), source="secondary", rid="sec"),
        _row(P, "standalone", at=t2, rid="nse-late"),
        _row(Q, "standalone", at=t1, fmt="insurance_skipped", rid="ins"),
    ]
    assert growth.known_at(rows, t1 - timedelta(seconds=1)) == {(P, "standalone"): rows[2]}
    at_t1 = growth.known_at(rows, t1)
    assert {k: r.id for k, r in at_t1.items()} == {
        (Q, "consolidated"): "v1",
        (P, "standalone"): "sec",
    }
    at_t2 = growth.known_at(rows, t2)
    assert {k: r.id for k, r in at_t2.items()} == {
        (Q, "consolidated"): "v2",
        (P, "standalone"): "nse-late",
    }


def test_results__year_ago_and_quarters_before() -> None:
    assert growth.year_ago(date(2024, 2, 29)) == date(2023, 2, 28)
    assert growth.year_ago(date(2025, 3, 31)) == date(2024, 3, 31)
    assert growth.quarter_ends_before(date(2025, 3, 31), 4) == [
        date(2024, 12, 31),
        date(2024, 9, 30),
        date(2024, 6, 30),
        date(2024, 3, 31),
    ]


def test_results__results_as_of_returns_only_public_rows() -> None:
    """`results_as_of` on stored rows: a revision filed after the original is returned only
    from its own public_at."""
    session = memory_session(memory_engine(HatFinancialResult))
    instrument = uuid.uuid4()
    t1, t2 = _ist(2025, 7, 20, 18, 0), _ist(2025, 8, 2, 11, 0)

    def stored(version: int, at: datetime, revenue: str, supersedes: uuid.UUID | None) -> uuid.UUID:
        row = HatFinancialResult(
            id=uuid.uuid4(),
            instrument_id=instrument,
            symbol="X",
            isin="INE000000001",
            period_start=date(2025, 4, 1),
            period_end=Q,
            basis="consolidated",
            format="reg33_indas",
            source="nse",
            revenue=Decimal(revenue),
            public_at=at.astimezone(UTC),  # SQLite keeps the wall time, read back as UTC
            public_at_source="announcement",
            version=version,
            supersedes_id=supersedes,
            listing="reg33",
            listing_seq=str(version),
            source_url=f"u{version}",
            sha256=f"s{version}",
            raw_path="p",
        )
        session.add(row)
        session.flush()
        return row.id

    first = stored(1, t1, "100", None)
    second = stored(2, t2, "90", first)
    assert results_as_of(session, instrument, t1 - timedelta(minutes=1)) == []
    assert [r.id for r in results_as_of(session, instrument, t1)] == [first]
    assert [r.id for r in results_as_of(session, instrument, t2 - timedelta(seconds=1))] == [first]
    assert [r.id for r in results_as_of(session, instrument, t2)] == [second]


# --- the secondary source, the raw cache, the universe --------------------------------------


def test_results__secondary_page() -> None:
    """The recorded RELIANCE consolidated page (its Quarterly Results section): 13 quarters in
    crore, EPS in rupees, the basis named above the table."""
    quarters = parse_page((FIXTURES / "secondary_quarters_reliance_consolidated.html").read_bytes())
    assert len(quarters) == 13
    assert {q.basis for q in quarters} == {"consolidated"}
    first, last = quarters[0], quarters[-1]
    assert (first.period_end, last.period_end) == (date(2023, 6, 30), date(2026, 6, 30))
    assert last.revenue == Decimal("309468") * CR
    assert last.net_profit == Decimal("23196") * CR
    assert last.eps_basic == Decimal("15.48")
    assert parse_page(b"<html><section id='quarters'></section></html>") == []
    assert page_url("M&M", "consolidated") == "https://www.screener.in/company/M%26M/consolidated/"
    assert page_url("TCS", "standalone") == "https://www.screener.in/company/TCS/"


def test_results__raw_cache_keeps_bodies_by_sha(tmp_path: Path) -> None:
    cache = RawCache(tmp_path)
    when = datetime(2026, 10, 28, tzinfo=UTC)
    entry = cache.put("https://x/a.xml", 200, b"<xbrl/>", when)
    assert cache.blob_path(entry.sha256).exists()
    cache.put("https://x/b.xml", 404, b"", when)
    with cache.manifest.open("a") as handle:
        handle.write('{"url": "https://x/torn"')  # an interrupted write
    again = RawCache(tmp_path)
    assert again.entry("https://x/a.xml") == entry
    assert again.body(entry) == b"<xbrl/>"
    assert again.entry("https://x/b.xml").status == 404  # type: ignore[union-attr]
    assert again.entry("https://x/torn") is None


def test_results__universe_turnover_and_series() -> None:
    """20 sessions of 12 crore pass from the 20th session; BE passes, SM never does; the latest
    ingest run's row wins."""
    days = [date(2021, 1, 1) + timedelta(days=n) for n in range(25)]
    rows = []
    for symbol, series in (("A", "EQ"), ("B", "BE"), ("C", "SM")):
        for day in days:
            rows.append((day, symbol, series, Decimal(12 * 10**7), "r1"))
    rows.append((days[24], "A", "EQ", Decimal(0), "r0"))  # an older run's row of the same day
    frame = pl.DataFrame(
        rows,
        schema=["date", "instrument_id", "series", "traded_value", "ingest_run_id"],
        orient="row",
    ).with_columns(close=pl.lit(None, pl.Decimal(18, 4)), volume=pl.lit(None, pl.Int64))
    found = passing(frame, date(2021, 1, 1))
    first = dict(zip(found["instrument_id"], found["first_pass"], strict=True))
    assert first == {"A": days[19], "B": days[19]}
