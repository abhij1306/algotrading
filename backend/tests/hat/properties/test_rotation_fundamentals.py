"""A rotation's fundamentals screen at its seams (`backtest.fundamentals`): the config key, the
point-in-time replay of the filings, the filing lag's cutoff, the share count on the price
basis, the value multiples and the screen's drops and missing-data marks."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.hat.backtest import fundamentals as fx
from app.hat.backtest.overrides import Overrides, parse_set
from app.hat.data.fundamentals.book import KnownFact
from app.hat.data.fundamentals.items import ANNUAL, INSTANT
from app.hat.data.fundamentals.research import Company, Known
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


def _known(day: date, source: str, version: int, *facts: tuple[str, str, int]) -> Known:
    kinds = [KnownFact(kind, FY15, item, D(value)) for kind, item, value in facts]
    return Known(_at(day), source, "consolidated", FY15, version, tuple(kinds))


def _company(*known: Known, bank: bool = False) -> Company:
    return Company("i", "SYM", ("i",), known, (), None, bank)


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


def _balance(equity: int, debt: int, cash: int | None = None) -> Company:
    facts = [
        (ANNUAL, "revenue", 1000),
        (ANNUAL, "net_profit", 60),
        (ANNUAL, "pbt", 80),
        (ANNUAL, "finance_cost", 20),
        (ANNUAL, "cfo", -5),
        (INSTANT, "equity_owners", equity),
        (INSTANT, "borrowings", debt),
    ]
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
    negative = fx.history(_balance(-10, 200), lambda _d: None, date(2016, 1, 1)).figures[0]
    assert negative.gearing == math.inf
    assert negative.net_debt is None  # no cash stated: unknown, never 0


def test_a_bank_has_no_roce_gearing_or_enterprise_value() -> None:
    bank = _company(*_balance(300, 200, 50).known, bank=True)
    figs = fx.history(bank, lambda _d: None, date(2016, 1, 1)).figures[0]
    assert (figs.roce, figs.gearing, figs.ebit, figs.net_debt) == (None, None, None, None)
    assert figs.cfo == -5


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


def _screen(rules: Fundamentals, figs: dict[str, fx.Figures | None]) -> fx.Screen:
    return fx.screen(list(figs), figs.get, lambda _n: 10.0, rules)


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
    assert dict(found.marks) == {
        "b": fx.ROCE,
        "c": fx.GEARING,
        "d": fx.CFO,
        "e": fx.GEARING,
        "f": fx.MISSING_KEPT,
        "g": fx.MISSING_KEPT,
    }
    dropped = _screen(_rules(quality={"min_roce": 0.1}, missing="drop"), figs)
    assert dropped.kept == ("a", "c", "d")
    assert dict(dropped.marks)["f"] == fx.MISSING_DROPPED


def test_value_drops_the_most_expensive_share_losses_first() -> None:
    rules = _rules(value={"multiple": "earnings_yield"})
    figs: dict[str, fx.Figures | None] = {
        f"n{i}": fx.Figures(profit=float(i), shares=1.0) for i in range(1, 10)
    }
    figs["loss"] = fx.Figures(profit=-1.0, shares=1.0)
    figs["none"] = fx.Figures(shares=1.0)  # no profit: not ranked, counted once
    found = _screen(rules, figs)
    # 10 ranked: the 2 cheapest-yielding go, the loss first
    assert dict(found.marks) == {"loss": fx.VALUE, "n1": fx.VALUE, "none": fx.MISSING_KEPT}
    assert "none" in found.kept
    both = _rules(quality={"min_roce": 0.1}, value={"multiple": "earnings_yield"})
    marks = dict(_screen(both, {"x": fx.Figures(shares=1.0)}).marks)
    assert marks == {"x": fx.MISSING_KEPT}
