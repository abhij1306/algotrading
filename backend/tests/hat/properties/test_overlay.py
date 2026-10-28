"""The idle-cash ETF overlay's rules (`backtest.overlay`) and its tax (`backtest.tax`): the
regime band, the sizing and rebalance, the funding from the sleeve with its same-day credit,
FIFO lots, each tax rule, set-off and carry-forward, LIQUIDBEES dividend units. Small hand-built
inputs; no run of the loop."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.hat.backtest import overlay as ov
from app.hat.backtest import tax
from app.hat.costs import load_cost_schedule
from app.hat.time import IST

D = Decimal


def _closes(values: list[float], start: date = date(2020, 1, 1)) -> list[tuple[date, Decimal]]:
    return [(start + timedelta(days=n), D(str(v))) for n, v in enumerate(values)]


BAND = ov.Band(sma=3, confirm=2)


# --- 1.1 regime --------------------------------------------------------------------------------


def test_regime_starts_off_until_a_switch() -> None:
    states = ov.band_states(_closes([100, 100, 100, 100]), BAND)
    assert not any(states.values())


def test_regime_switches_on_after_confirmed_closes_above_the_band() -> None:
    # SMA3 of (100, 100, 110) is 103.33; 110 > 105.4 (x 1.02): one close, then a second
    states = list(ov.band_states(_closes([100, 100, 110, 112]), BAND).values())
    assert states == [False, False, False, True]


def test_regime_inside_the_band_never_switches() -> None:
    # every close within 2% of its SMA
    states = ov.band_states(_closes([100, 101, 100, 101, 100, 101, 100]), BAND)
    assert not any(states.values())


def test_regime_one_close_outside_then_back_inside_resets_the_streak() -> None:
    # 110 is above the band, 104 falls back inside it (SMA 104.67 x 1.02 = 106.8), 112 is one
    states = list(ov.band_states(_closes([100, 100, 110, 104, 112]), BAND).values())
    assert states == [False] * 5


def test_regime_switches_off_after_confirmed_closes_below_the_band() -> None:
    values = [100, 100, 110, 112, 115, 100, 95, 90]
    states = list(ov.band_states(_closes(values), BAND).values())
    # on from the 4th close; 100 vs SMA 109 x 0.98 = 106.8 below, 95 below: off at the 7th
    assert states == [False, False, False, True, True, True, False, False]


def test_regime_needs_the_full_sma_window() -> None:
    states = ov.band_states(_closes([100, 200, 300]), ov.Band(sma=5, confirm=1))
    assert not any(states.values())


def test_sleeve_assets_per_variant() -> None:
    assert ov.sleeve_asset(ov.NONE, True, True) is None
    assert ov.sleeve_asset(ov.LIQUID, True, True) == ov.LIQUIDBEES
    assert ov.sleeve_asset(ov.NIFTY, False, False) == ov.NIFTYBEES
    assert ov.sleeve_asset(ov.REGIME, True, False) == ov.NIFTYBEES
    assert ov.sleeve_asset(ov.REGIME, False, True) == ov.LIQUIDBEES
    assert ov.sleeve_asset(ov.REGIME_GOLD, False, True) == ov.GOLDBEES
    assert ov.sleeve_asset(ov.REGIME_GOLD, False, False) == ov.LIQUIDBEES
    assert ov.sleeve_asset(ov.REGIME_GOLD, True, True) == ov.NIFTYBEES


# --- 1.3 sizing --------------------------------------------------------------------------------


def test_target_keeps_the_buffer_and_floors_at_zero() -> None:
    assert ov.target_value(D(1000), D(300), D("0.05")) == D(650)
    assert ov.target_value(D(1000), D(990), D("0.05")) == 0


def test_rebalance_only_beyond_the_threshold() -> None:
    assert not ov.needs_rebalance(D(640), D(650), D(1000), D("0.02"))  # 1% of equity
    assert not ov.needs_rebalance(D(630), D(650), D(1000), D("0.02"))  # exactly 2%: not beyond
    assert ov.needs_rebalance(D(629), D(650), D(1000), D("0.02"))
    assert ov.needs_rebalance(D(680), D(650), D(1000), D("0.02"))


def test_exposure_cap() -> None:
    assert ov.exposure_ok(D(400), D(600), D(1000))
    assert not ov.exposure_ok(D(400), D(601), D(1000))


# --- 1.4 funding -------------------------------------------------------------------------------


def test_credit_is_80_pct_before_2024_10_07_and_100_from_it() -> None:
    credit = ov.CreditSchedule()
    assert credit.on(date(2024, 10, 4)) == D("0.8")
    assert credit.on(date(2024, 10, 7)) == 1
    assert credit.on(date(2026, 1, 1)) == 1


def test_units_to_fund_grosses_up_for_the_credit() -> None:
    # 800 short at 100 net a unit: 10 units at 100% credit, 10 / 0.8 = 12.5 -> 13 at 80%
    assert ov.units_to_fund(D(800), D(100), D(1), 50) == 8
    assert ov.units_to_fund(D(800), D(100), D("0.8"), 50) == 10
    assert ov.units_to_fund(D(801), D(100), D("0.8"), 50) == 11
    assert ov.units_to_fund(D(800), D(100), D("0.8"), 6) == 6  # no more than held
    assert ov.units_to_fund(D(0), D(100), D("0.8"), 6) == 0


def test_refusal_when_underfunded_is_insufficient_credit_only_with_a_sleeve() -> None:
    assert ov.refusal_reason("size_zero_cash", True) == ov.INSUFFICIENT_CREDIT
    assert ov.refusal_reason("size_below_min", True) == ov.INSUFFICIENT_CREDIT
    assert ov.refusal_reason("size_zero_cash", False) == "size_zero_cash"
    assert ov.refusal_reason("size_zero_risk", True) == "size_zero_risk"


def _bar(price: str) -> ov.EtfBar:
    p = D(price)
    return ov.EtfBar(p, p, p, p)


def _overlay(days: list[date], bars_5m: bool = False, **config: object) -> ov.Overlay:
    daily = {a: {d: _bar("100") for d in days} for a in ov.ASSETS}
    five = (
        {
            ov.LIQUIDBEES: {
                d: [
                    (
                        datetime.combine(d, datetime.min.time(), IST).replace(hour=9, minute=30),
                        _bar("101"),
                    )
                ]
                for d in days
            }
        }
        if bars_5m
        else {}
    )
    data = ov.EtfData(daily, five, {}, (), _closes([1] * len(days), days[0]))
    zero = {a: D(0) for a in ov.ASSETS}
    return ov.Overlay(
        ov.OverlayConfig(sleeve=ov.LIQUID, slippage_bps=zero, tax=False, **config),  # type: ignore[arg-type]
        data,
        load_cost_schedule(),
    )


@pytest.mark.parametrize(("day", "pct"), [(date(2024, 10, 4), D("0.8")), (date(2024, 10, 8), D(1))])
def test_funding_sells_the_sleeve_and_credits_by_date(day: date, pct: Decimal) -> None:
    sleeve = _overlay([day])
    sleeve.units[ov.LIQUIDBEES] = 100
    at = datetime.combine(day, datetime.min.time(), IST).replace(hour=10)
    sleeve.fund(day, at, D(1000))
    sold = 100 - sleeve.units[ov.LIQUIDBEES]
    proceeds = sleeve._proceeds(ov.LIQUIDBEES, sold, D(100), day)  # noqa: SLF001
    short = sleeve._proceeds(ov.LIQUIDBEES, sold - 1, D(100), day)  # noqa: SLF001
    assert proceeds * pct >= 1000 > short * pct  # the fewest units that cover it
    assert sleeve.cash == proceeds  # the account has all of it
    assert sleeve.pending == proceeds - proceeds * pct  # the rest credited next session
    assert sleeve.counts[ov.SLEEVE_PRICE_ESTIMATED] == 1  # no 5-minute bar: the day's open


def test_funding_prices_at_the_5_minute_bar_of_the_fill() -> None:
    day = date(2025, 1, 6)
    sleeve = _overlay([day], bars_5m=True)
    sleeve.units[ov.LIQUIDBEES] = 100
    at = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=32)
    assert sleeve._price_at(ov.LIQUIDBEES, day, at) == (D(101), False)  # noqa: SLF001
    sleeve.fund(day, at, D(500))
    assert sleeve.counts[ov.SLEEVE_PRICE_ESTIMATED] == 0


def test_pending_credit_is_free_the_next_session() -> None:
    days = [date(2024, 10, 3), date(2024, 10, 4)]
    sleeve = _overlay(days)
    sleeve.units[ov.LIQUIDBEES] = 100
    sleeve.fund(days[0], datetime.combine(days[0], datetime.min.time(), IST), D(1000))
    assert sleeve.pending > 0
    sleeve.open_session(days[1], D(0))
    assert sleeve.pending == 0


def test_close_plans_a_rebalance_beyond_the_threshold_and_buys_at_the_open() -> None:
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    sleeve = _overlay(days)
    sleeve.close_session(
        days[0], None, days[1], equity=D(100_000), positions=D(0), free_cash=D(100_000)
    )
    assert sleeve.plan is not None
    assert sleeve.plan.target == D(95_000)
    sleeve.open_session(days[1], D(100_000))
    assert sleeve.units[ov.LIQUIDBEES] * 100 <= 95_000
    assert sleeve.units[ov.LIQUIDBEES] > 900


# --- 1.6 tax -----------------------------------------------------------------------------------

DAY = date(2024, 6, 3)  # FY 2024-25


def test_fy_and_long_term() -> None:
    assert tax.fy_of(date(2024, 3, 31)) == 2023
    assert tax.fy_of(date(2024, 4, 1)) == 2024
    assert not tax.long_term(date(2023, 6, 3), date(2024, 6, 3))  # exactly 12 months
    assert tax.long_term(date(2023, 6, 3), date(2024, 6, 4))
    assert tax.long_term(date(2024, 1, 31), date(2025, 2, 1))


def test_fifo_lots() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("X", tax.EQUITY, 10, D(1000), date(2024, 5, 1))  # 100 a unit
    book.buy("X", tax.EQUITY, 10, D(2000), date(2024, 5, 2))  # 200 a unit
    assert book.sell("X", 15, D(3000), DAY) == D(3000) - D(1000) - D(1000)  # 10 @100, 5 @200
    assert book.held("X") == 5
    with pytest.raises(tax.TaxError):
        book.sell("X", 6, D(1), DAY)


def test_equity_short_term_at_20_pct() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("X", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.sell("X", 1, D(2000), DAY)
    assert book.settle(2024).tax == D(1000) * D("0.20") * D("1.04")


def test_equity_long_term_above_the_shared_exemption() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("X", tax.EQUITY, 1, D(100_000), date(2022, 5, 1))
    book.buy("NIFTYBEES", tax.EQUITY, 1, D(100_000), date(2022, 5, 1))
    book.sell("X", 1, D(200_000), DAY)  # 1 lakh long term
    book.sell("NIFTYBEES", 1, D(200_000), DAY)  # 1 lakh more: one exemption for both
    found = book.settle(2024)
    assert found.exemption_used == tax.LTCG_EXEMPTION
    assert found.tax == (D(200_000) - D(125_000)) * D("0.125") * D("1.04")


def test_gold_short_term_at_slab_and_long_term_without_exemption() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("GOLDBEES", tax.GOLD, 2, D(2000), date(2022, 5, 1))
    book.buy("GOLDBEES", tax.GOLD, 1, D(1000), date(2024, 5, 1))
    book.sell("GOLDBEES", 3, D(6000), DAY)  # 2000 long term, 1000 short term
    found = book.settle(2024)
    assert found.exemption_used == 0
    assert found.tax == (D(2000) * D("0.125") + D(1000) * D("0.3")) * D("1.04")


def test_debt_gains_at_slab_whatever_the_holding() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("LIQUIDBEES", tax.DEBT, 1, D(1000), date(2020, 5, 1))
    book.sell("LIQUIDBEES", 1, D(1100), DAY)
    assert book.settle(2024).tax == D(100) * D("0.3") * D("1.04")


def test_a_short_term_loss_sets_off_the_higher_rate_first_below_a_20_pct_slab() -> None:
    book = tax.TaxBook(D("0.1"))
    book.buy("GOLDBEES", tax.GOLD, 1, D(1000), date(2024, 5, 1))
    book.buy("X", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.buy("Y", tax.EQUITY, 1, D(2000), date(2024, 5, 1))
    book.sell("GOLDBEES", 1, D(2000), DAY)  # 1000 at the 10% slab
    book.sell("X", 1, D(2000), DAY)  # 1000 at 20%
    book.sell("Y", 1, D(1000), DAY)  # a 1000 loss: sets off the 20% gain
    assert book.settle(2024).tax == D(1000) * D("0.1") * D("1.04")


def test_liquidbees_dividend_units_are_income_and_cost_on_receipt() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("LIQUIDBEES", tax.DEBT, 10, D(10_000), date(2024, 5, 1))
    book.dividend_units("LIQUIDBEES", 1, D(1000), date(2024, 5, 2))
    assert book.held("LIQUIDBEES") == 11
    # selling the dividend unit at its value on receipt is no further gain
    assert book.sell("LIQUIDBEES", 11, D(11_000), DAY) == 0
    assert book.settle(2024).tax == D(1000) * D("0.3") * D("1.04")


def test_liquidbees_distribution_credits_whole_units_and_carries_the_fraction() -> None:
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    daily = {a: {d: _bar("1000") for d in days} for a in ov.ASSETS}
    tr = {days[0]: D(100), days[1]: D("100.05")}  # 0.05%: 0.5 a unit on 1000
    data = ov.EtfData(daily, {}, tr, (), _closes([1, 1], days[0]))
    zero = {a: D(0) for a in ov.ASSETS}
    sleeve = ov.Overlay(
        ov.OverlayConfig(sleeve=ov.LIQUID, slippage_bps=zero, slab_rate=D("0.3")),
        data,
        load_cost_schedule(),
    )
    sleeve.units[ov.LIQUIDBEES] = 3000
    sleeve.book.buy("LIQUIDBEES", tax.DEBT, 3000, D(3_000_000), days[0])  # type: ignore[union-attr]
    sleeve._distribute(days[1], days[0])  # noqa: SLF001
    assert sleeve.units[ov.LIQUIDBEES] == 3001  # 1500 paid: one unit, 500 carried
    assert sleeve.residue == D(500)
    assert sleeve.book.years[2024].income == D(1000)  # type: ignore[union-attr]


def test_implied_distribution() -> None:
    assert ov.implied_distribution(D(1000), D(1000), D(100), D("100.01")) == D("0.1")
    assert ov.implied_distribution(D(1000), D(1001), D(100), D(100)) == 0  # a price rise pays none


def test_short_term_loss_sets_off_any_gain_long_term_loss_only_long_term() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("A", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.buy("B", tax.EQUITY, 1, D(1000), date(2022, 5, 1))
    book.buy("C", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.sell("A", 1, D(600), DAY)  # 400 short-term loss
    book.sell("B", 1, D(1300_000), DAY)  # long-term gain
    book.sell("C", 1, D(1500), DAY)  # 500 short-term gain
    found = book.settle(2024)
    # the short-term loss sets off the short-term gain first (taxed higher)
    assert found.taxable["st_equity"] == D(100)
    long = D(1300_000) - D(1000)
    assert found.taxable["lt_equity"] == long

    book = tax.TaxBook(D("0.3"))
    book.buy("A", tax.EQUITY, 1, D(1000), date(2022, 5, 1))
    book.buy("C", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.sell("A", 1, D(600), DAY)  # 400 long-term loss
    book.sell("C", 1, D(1500), DAY)  # 500 short-term gain: the long-term loss cannot touch it
    found = book.settle(2024)
    assert found.taxable["st_equity"] == D(500)
    assert found.carried_lt == D(400)


def test_dividend_income_is_set_off_by_no_loss() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("A", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.sell("A", 1, D(600), DAY)
    book.income(DAY, D(1000))
    found = book.settle(2024)
    assert found.tax == D(1000) * D("0.3") * D("1.04")
    assert found.carried_st == D(400)


def test_losses_carry_forward_eight_years_then_lapse() -> None:
    book = tax.TaxBook(D("0.3"))
    book.buy("A", tax.EQUITY, 1, D(1000), date(2015, 5, 1))
    book.sell("A", 1, D(600), date(2015, 6, 1))  # 400 short-term loss in FY 2015
    assert book.settle(2015).carried_st == D(400)
    book.buy("B", tax.EQUITY, 1, D(1000), date(2016, 5, 1))
    book.sell("B", 1, D(1100), date(2016, 6, 1))  # 100 gain in FY 2016, set off
    found = book.settle(2016)
    assert found.tax == 0
    assert found.carried_st == D(300)
    for fy in range(2017, 2023):
        book.settle(fy)
    book.buy("C", tax.EQUITY, 1, D(1000), date(2023, 5, 1))
    book.sell("C", 1, D(1100), date(2023, 6, 1))  # FY 2023: 8 years on, still usable
    assert book.settle(2023).carried_st == D(200)
    book.buy("D", tax.EQUITY, 1, D(1000), date(2024, 5, 1))
    book.sell("D", 1, D(1100), date(2024, 6, 1))  # FY 2024: lapsed
    found = book.settle(2024)
    assert found.carried_st == 0
    assert found.tax == D(100) * D("0.20") * D("1.04")


def test_parse_overlay_defaults_and_refusals() -> None:
    found = ov.parse_overlay({"sleeve": "regime_gold", "tax": {"slab_rate": 0.2}})
    assert found.sleeve == ov.REGIME_GOLD
    assert found.slab_rate == D("0.2")
    assert found.slippage_bps == {ov.NIFTYBEES: 5, ov.GOLDBEES: 5, ov.LIQUIDBEES: 1}
    assert found.credit.on(date(2024, 1, 1)) == D("0.8")
    for bad in ({"sleeve": "bonds"}, {"unknown": 1}, {"slippage_bps": {"X": 1}}, {"tax": "yes"}):
        with pytest.raises(ov.OverlayError):
            ov.parse_overlay(bad)
