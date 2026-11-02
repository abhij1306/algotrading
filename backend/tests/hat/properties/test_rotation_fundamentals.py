"""A rotation's fundamentals screen at its seams (`backtest.fundamentals`): the config key, the
point-in-time replay of the filings, the filing lag's cutoff, the share count on the price
basis, the value multiples and the screen's drops and missing-data marks."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.hat.backtest import fundamentals as fx
from app.hat.backtest.overrides import Overrides, parse_set
from app.hat.data.fundamentals.book import KnownFact
from app.hat.data.fundamentals.items import ANNUAL, INSTANT
from app.hat.data.fundamentals.research import Action, Company, Known, company_chains, merged
from app.hat.engine.strategy.config import ConfigError, Fundamentals, Rotation, parse_config
from app.hat.engine.strategy.versions import seed_config

D = Decimal
FY15 = date(2015, 3, 31)


def _rules(**fields: object) -> Fundamentals:
    return Fundamentals.model_validate(fields)


# --- the config key ---------------------------------------------------------------------------


def test_the_key_is_left_out_unset_and_set_through_a_run_override() -> None:
    assert "fundamentals" not in Rotation().model_dump(mode="json")
    base = seed_config("momentum-rotation")
    text = (
        'rotation.fundamentals={"quality": {"min_roce": 0.12, "max_gearing": 1.5,'
        ' "positive_cfo": true}, "value": {"multiple": "ebit_ev"}, "missing": "drop"}'
    )
    found = Overrides(sets=(parse_set(text),)).apply_config(base)
    stored = found.to_json()["rotation"]["fundamentals"]
    assert stored["value"] == {"multiple": "ebit_ev", "drop_pct": 20.0}
    assert stored["lag_sessions"] == 1
    assert parse_config(found.to_json()).hash() == found.hash()
    assert parse_config(base.to_json()).hash() == base.hash()


@pytest.mark.parametrize(
    ("block", "code"),
    [
        ({}, "fundamentals_without_screen"),
        ({"quality": {}}, "quality_without_check"),
    ],
)
def test_an_empty_screen_is_refused(block: dict[str, object], code: str) -> None:
    config = seed_config("momentum-rotation").to_json()
    config["rotation"]["fundamentals"] = block
    with pytest.raises(ConfigError) as caught:
        parse_config(config)
    assert any(code in problem for _path, problem in caught.value.problems)


# --- the lag and the replay -------------------------------------------------------------------


def test_the_cutoff_is_the_session_lag_sessions_back() -> None:
    days = [date(2020, 1, d) for d in (1, 2, 3, 6, 7)]
    assert fx.cutoff(days, 4, 0) == date(2020, 1, 7)
    assert fx.cutoff(days, 4, 1) == date(2020, 1, 6)
    assert fx.cutoff(days, 4, 3) == date(2020, 1, 2)
    assert fx.cutoff(days, 1, 2) is None


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def _known(
    day: date,
    source: str,
    version: int,
    *facts: tuple[str, str, int],
    basis: str = "consolidated",
    family: str = "indas",
    end: date = FY15,
) -> Known:
    kinds = [KnownFact(kind, end, item, D(value)) for kind, item, value in facts]
    return Known(_at(day), source, basis, end, version, tuple(kinds), family)


def _company(*known: Known) -> Company:
    return Company("i", "SYM", ("i",), known, (), None)


def test_a_figure_counts_from_its_public_date_first_published_first() -> None:
    year = (ANNUAL, "revenue", 100)
    company = _company(
        _known(date(2015, 5, 10), "screener", 1, year, (ANNUAL, "net_profit", 11)),
        _known(date(2015, 5, 12), "nse", 1, year, (ANNUAL, "net_profit", 10)),
        # a restated version: only from its own public date
        _known(date(2015, 6, 20), "nse", 2, year, (ANNUAL, "net_profit", 12)),
        _known(date(2016, 1, 5), "nse", 3, year, (ANNUAL, "net_profit", 13)),
    )
    found = fx.history(company, lambda _d: None, date(2015, 12, 31))
    assert found.at(date(2015, 5, 9)) is None
    assert found.at(date(2015, 5, 10)).profit == 11  # the secondary source, until NSE's
    assert found.at(date(2015, 6, 19)).profit == 10  # first-published wins
    assert found.at(date(2015, 6, 20)).profit == 12
    assert found.at(date(2016, 2, 1)).profit == 12  # nothing after the period is read


BALANCE = (
    (ANNUAL, "revenue", 1000),
    (ANNUAL, "net_profit", 60),
    (ANNUAL, "pbt", 80),
    (ANNUAL, "finance_cost", 20),
    (ANNUAL, "cfo", -5),
)


def _balance(equity: int, debt: int, cash: int | None = None) -> Company:
    facts = [*BALANCE, (INSTANT, "equity_owners", equity), (INSTANT, "borrowings", debt)]
    if cash is not None:
        facts.append((INSTANT, "cash", cash))
    return _company(_known(date(2015, 5, 12), "nse", 1, *facts))


def test_quality_figures_from_the_latest_year_and_balance_sheet() -> None:
    figs = fx.history(_balance(300, 200, 50), lambda _d: None, date(2016, 1, 1)).figures[0]
    assert figs.roce == pytest.approx(100 / 500)
    assert figs.gearing == pytest.approx(200 / 300)
    assert figs.cfo == -5
    assert figs.ebit == 100
    assert figs.net_debt == 150
    assert (figs.flow_end, figs.balance_end, figs.cfo_end) == (FY15, FY15, FY15)
    negative = fx.history(_balance(-10, 200), lambda _d: None, date(2016, 1, 1)).figures[0]
    assert negative.gearing == math.inf
    assert negative.net_debt is None  # no cash stated: unknown, never 0


def test_one_basis_per_snapshot() -> None:
    # the latest results are consolidated; the balance sheet is standalone only: not mixed in
    company = _company(
        _known(date(2015, 5, 12), "nse", 1, *BALANCE),
        _known(
            date(2015, 5, 12),
            "nse",
            1,
            (INSTANT, "equity_owners", 300),
            (INSTANT, "borrowings", 200),
            basis="standalone",
        ),
    )
    figs = fx.history(company, lambda _d: None, date(2016, 1, 1)).figures[0]
    assert figs.ebit == 100
    assert (figs.roce, figs.gearing, figs.balance_end) == (None, None, None)


def test_the_family_is_the_one_known_on_each_day() -> None:
    # an NBFC that becomes a bank: a bank only from its first bank filing
    nbfc = _known(date(2015, 5, 12), "nse", 1, *BALANCE, family="nbfc")
    bank = _known(date(2016, 5, 12), "nse", 1, *BALANCE, family="bank", end=date(2016, 3, 31))
    page = _known(date(2016, 6, 1), "screener", 1, (ANNUAL, "revenue", 1), family="screener")
    found = fx.history(_company(nbfc, bank, page), lambda _d: None, date(2017, 1, 1))
    assert found.at(date(2016, 5, 11)).family == "nbfc"
    assert found.at(date(2016, 5, 12)).family == "bank"
    assert found.at(date(2016, 7, 1)).family == "bank"  # a restated page tells nothing


def test_a_stale_figure_counts_as_missing() -> None:
    figs = fx.Figures(
        roce=0.2,
        gearing=0.5,
        cfo=1.0,
        profit=2.0,
        ebit=3.0,
        net_debt=4.0,
        flow_end=date(2020, 3, 31),
        balance_end=date(2019, 3, 31),
        cfo_end=date(2020, 3, 31),
    )
    fresh = fx.aged(figs, date(2021, 3, 31), 550)
    assert fresh is not None
    assert (fresh.profit, fresh.cfo) == (2.0, 1.0)
    assert (fresh.roce, fresh.gearing, fresh.net_debt) == (None, None, None)  # 731 days
    old = fx.aged(figs, date(2021, 12, 31), 550)
    assert old is not None
    assert (old.profit, old.ebit, old.cfo) == (None, None, None)
    assert fx.aged(None, date(2021, 1, 1), 550) is None


# --- valuation --------------------------------------------------------------------------------


def test_the_share_count_on_the_price_basis_survives_a_later_split() -> None:
    # 100 shares at a raw close of 50 before a later 1:2 split (factor 0.5): 200 shares on the
    # basis, and the market cap on any later adjusted close is the true one
    shares = fx.basis_shares(D(100), 0.5)
    assert shares == 200
    assert shares * 25.0 == 100 * 50.0  # before the split: adjusted 25, raw 50
    assert fx.basis_shares(D(100), None) is None


def test_the_multiples_as_yields() -> None:
    figs = fx.Figures(profit=-10.0, ebit=50.0, net_debt=-150.0, shares=10.0)
    assert fx.multiple(figs, 10.0, "earnings_yield") == -0.1
    assert fx.multiple(figs, 10.0, "ebit_ev") == math.inf  # cash above the market cap
    assert fx.multiple(fx.Figures(ebit=-1.0, net_debt=-150.0, shares=10.0), 10.0, "ebit_ev") == (
        -math.inf
    )
    assert fx.multiple(fx.Figures(ebit=20.0, net_debt=100.0, shares=10.0), 10.0, "ebit_ev") == 0.1
    assert fx.multiple(fx.Figures(profit=1.0), 10.0, "earnings_yield") is None


# --- the screen -------------------------------------------------------------------------------


def _screen(
    rules: Fundamentals,
    figs: dict[str, fx.Figures | None],
    industries: dict[str, str] | None = None,
) -> fx.Screen:
    found = industries or {}
    return fx.screen(list(figs), figs.get, lambda _n: 10.0, lambda n: found.get(n, "IT"), rules)


def _marks(found: fx.Screen) -> dict[str, str]:
    return {m.name: m.reason for m in found.marks}


def test_quality_drops_the_first_failing_check_and_marks_the_missing() -> None:
    rules = _rules(quality={"min_roce": 0.1, "max_gearing": 1.0, "positive_cfo": True})
    good = fx.Figures(roce=0.2, gearing=0.5, cfo=1.0)
    figs = {
        "a": good,
        "b": fx.Figures(roce=0.05, gearing=2.0, cfo=1.0),
        "c": fx.Figures(roce=0.2, gearing=2.0, cfo=-1.0),
        "d": fx.Figures(roce=0.2, gearing=0.5, cfo=0.0),
        "e": fx.Figures(roce=None, gearing=2.0, cfo=1.0),  # a failure beats a missing figure
        "f": fx.Figures(roce=None, gearing=0.5, cfo=1.0),
        "g": None,
    }
    found = _screen(rules, figs)
    assert found.kept == ("a", "f", "g")
    assert _marks(found) == {
        "b": fx.ROCE,
        "c": fx.GEARING,
        "d": fx.CFO,
        "e": fx.GEARING,
        "f": fx.MISSING_KEPT,
        "g": fx.MISSING_KEPT,
    }
    assert {m.name for m in found.marks if m.kept} == {"f", "g"}
    dropped = _screen(_rules(quality={"min_roce": 0.1}, missing="drop"), figs)
    assert dropped.kept == ("a", "c", "d")
    assert _marks(dropped)["f"] == fx.MISSING_DROPPED
    assert not any(m.kept for m in dropped.marks)


def test_roce_and_gearing_pass_for_a_financial_and_are_missing_for_an_unknown_kind() -> None:
    rules = _rules(quality={"min_roce": 0.1, "max_gearing": 1.0, "positive_cfo": True})
    levered = fx.Figures(roce=0.02, gearing=5.0, cfo=1.0)
    figs = {
        "nbfc": replace(levered, family="nbfc"),
        "insurer": replace(levered, family="insurer"),
        "by_industry": replace(levered, family="html"),  # an HTML page: the industry tells
        "page": replace(levered, family="html"),  # neither tells: not an ordinary company
        "loss": fx.Figures(family="bank", cfo=-1.0),  # the cash flow check still applies
    }
    found = _screen(
        rules,
        figs,
        {"by_industry": fx.FINANCIAL_INDUSTRY, "page": "unknown", "loss": "unknown"},
    )
    assert found.kept == ("nbfc", "insurer", "by_industry", "page")
    assert _marks(found) == {
        "nbfc": fx.FINANCIAL,
        "insurer": fx.FINANCIAL,
        "by_industry": fx.FINANCIAL,
        "page": fx.MISSING_KEPT,
        "loss": fx.CFO + "_financial",
    }
    assert fx.kind("indas", "unknown") == "ordinary"
    assert fx.kind(None, "Realty") == "ordinary"
    assert fx.kind("bank", "Realty") == "financial"


def test_value_drops_the_most_expensive_share_losses_first() -> None:
    rules = _rules(value={"multiple": "earnings_yield"})
    figs: dict[str, fx.Figures | None] = {
        f"n{i}": fx.Figures(profit=float(i), shares=1.0) for i in range(1, 10)
    }
    figs["loss"] = fx.Figures(profit=-1.0, shares=1.0)
    figs["none"] = fx.Figures(shares=1.0)  # no profit: not ranked, counted once
    found = _screen(rules, figs)
    # 10 ranked: the 2 cheapest-yielding go, the loss first
    assert _marks(found) == {"loss": fx.VALUE, "n1": fx.VALUE, "none": fx.MISSING_KEPT}
    assert "none" in found.kept
    both = _rules(quality={"min_roce": 0.1}, value={"multiple": "earnings_yield"})
    assert _marks(_screen(both, {"x": fx.Figures(shares=1.0)})) == {"x": fx.MISSING_KEPT}


def test_under_ebit_ev_the_financials_rank_apart_on_earnings_yield() -> None:
    rules = _rules(value={"multiple": "ebit_ev", "drop_pct": 50})
    figs: dict[str, fx.Figures | None] = {
        "c1": fx.Figures(ebit=1.0, net_debt=0.0, shares=1.0),
        "c2": fx.Figures(ebit=5.0, net_debt=0.0, shares=1.0),
        # a bank's EBIT/EV would rank it cheapest; its earnings yield ranks it with the banks
        "b1": fx.Figures(family="bank", profit=1.0, ebit=50.0, net_debt=0.0, shares=1.0),
        "b2": fx.Figures(family="bank", profit=3.0, ebit=1.0, net_debt=0.0, shares=1.0),
        "u": fx.Figures(family="html", ebit=9.0, net_debt=0.0, shares=1.0),
    }
    found = _screen(rules, figs, {"u": "unknown"})
    assert _marks(found) == {
        "c1": fx.VALUE,
        "b1": fx.VALUE + "_financial",
        "b2": fx.FINANCIAL,
        "u": fx.MISSING_KEPT,
    }
    assert found.kept == ("c2", "b2", "u")


# --- companies across ISIN changes ------------------------------------------------------------


def test_a_company_is_one_chain_under_its_latest_instrument() -> None:
    newer = {"old": "new", "older": "new", "x": "y"}
    # filings under two instruments of one chain: one company; a latest instrument without a
    # filing of its own is still the key and in the chain (its prices and actions)
    chains = company_chains(["old", "older", "solo"], newer)
    assert chains == {"new": ["new", "old", "older"], "solo": ["solo"]}


def test_readings_under_one_member_merge_never_overwrite() -> None:
    a = Company(
        "new",
        "NEW",
        ("new", "old"),
        (_known(date(2015, 5, 12), "nse", 1, (ANNUAL, "revenue", 1)),),
        (Action(date(2016, 1, 1), D("0.5"), split=True),),
        None,
    )
    b = Company(
        "old",
        "OLD",
        ("old",),
        (_known(date(2014, 5, 12), "nse", 1, (ANNUAL, "revenue", 2), end=date(2014, 3, 31)),),
        (
            Action(date(2016, 1, 1), D("0.5"), split=True),
            Action(date(2013, 1, 1), D(1), split=False),
        ),
        (date(2020, 1, 1), D(2)),
    )
    groups = fx.by_member([a, b], {"old": "new"}, {"new"})
    assert list(groups) == ["new"]
    assert len(groups["new"]) == 2
    one = merged(groups["new"])
    assert (one.instrument, one.ids) == ("new", ("new", "old"))
    assert len(one.known) == 2
    assert [x.ex_date for x in one.actions] == [date(2013, 1, 1), date(2016, 1, 1)]
    assert one.face_value_now == (date(2020, 1, 1), D(2))
    assert fx.by_member([b], {}, {"new"}) == {}
