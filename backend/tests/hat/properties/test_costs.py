"""PBT costs.*: slippage and statutory charges of
`app.hat.costs` on generated cost models and fills, in Decimal and compared exactly,
against the rules written out here."""

from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.hat.costs import (
    MAX_SLIPPAGE_BPS,
    BacktestFills,
    CashBoundFills,
    CostError,
    CostSchedule,
    charges,
    check_slippage,
    load_cost_schedule,
    parse_cost_model,
    slipped,
)

D = Decimal
PAISE = D("0.01")
PLACES = D("0.0001")
PRICE = st.integers(1, 10_000_000).map(lambda n: D(n) / 10_000)  # 0.0001 .. 1000.0000
QTY = st.integers(1, 100_000)
BPS = st.integers(0, 9_999).map(D) | st.integers(0, 99_990).map(lambda n: D(n) / 10)
SIDE = st.sampled_from(["buy", "sell"])
RATE = st.integers(0, 50_000).map(lambda n: str(D(n) / 10_000_000))  # 0 .. 0.5%
DAY = date(2025, 1, 10)  # under the 2024-10-01 schedule


@st.composite
def models(draw: st.DrawFn) -> dict[str, Any]:
    names = draw(st.lists(st.sampled_from("abcdef"), unique=True, max_size=6))
    components = []
    for name in names:
        component: dict[str, Any] = {
            "name": name,
            "sides": draw(st.sampled_from([["buy"], ["sell"], ["buy", "sell"], []])),
            "rate": draw(RATE),
            "taxable": draw(st.booleans()),
        }
        if draw(st.booleans()):
            component["per_order"] = str(D(draw(st.integers(0, 2_000))) / 100)
        if draw(st.booleans()):
            component["cap"] = str(D(draw(st.integers(0, 10_000))) / 100)
        components.append(component)
    return {
        "version": "gen",
        "effective_from": "2016-01-01",
        "gst_rate": draw(RATE),
        "dp_per_sell_day": "0",
        "components": components,
    }


def _money(value: D) -> D:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


@given(PRICE, SIDE, BPS)
@example(D("100"), "buy", D("9999"))
@example(D("0.0001"), "sell", D("9999.9"))
def test_slippage_adverse(price: D, side: str, bps: D) -> None:
    """A buy fills at p x (1 + s/10000) rounded up to 4 places, a sell at p x (1 - s/10000)
    rounded down (never below 0.0001): never better than the reference price."""
    got = slipped(price, side, bps)
    if side == "buy":
        assert got == (price * (1 + bps / 10_000)).quantize(PLACES, rounding=ROUND_CEILING)
        assert got >= price
    else:
        want = (price * (1 - bps / 10_000)).quantize(PLACES, rounding=ROUND_FLOOR)
        assert got == max(want, PLACES)
        assert got <= price


@given(PRICE, PRICE, QTY, BPS, BPS)
def test_slippage_adverse_net(entry: D, exit_: D, qty: int, low: D, high: D) -> None:
    """The net P&L of a round trip is non-increasing in slippage."""
    low, high = sorted((low, high))
    schedule = load_cost_schedule()

    def net(bps: D) -> D:
        fills = BacktestFills(schedule, bps)
        bought, buy_fees = fills.fill("buy", qty, entry, DAY)
        sold, sell_fees = fills.fill("sell", qty, exit_, DAY)
        return (sold - bought) * qty - buy_fees - sell_fees

    assert net(high) <= net(low)


@given(
    st.one_of(
        st.integers(-100, -1).map(D),
        st.integers(10_000, 20_000).map(D),
        st.sampled_from([D("NaN"), D("Infinity"), D("-Infinity")]),
        st.floats(allow_nan=True),
        st.integers(),
    )
)
def test_slippage_adverse_bounds(bps: object) -> None:
    """A slippage outside 0 <= s < 10000 bps, or not a Decimal, is refused when a run starts."""
    with pytest.raises(CostError):
        check_slippage(bps)  # type: ignore[arg-type]
    assert MAX_SLIPPAGE_BPS == 10_000


@given(models(), SIDE, QTY, PRICE, QTY)
def test_charges_contract(raw: dict[str, Any], side: str, qty: int, price: D, more: int) -> None:
    """Each charge is >= 0 and applies only to the sides its entry names; GST is charged only on
    the taxable components; the total is their sum plus GST, and it does not fall as the traded
    value grows."""
    model = parse_cost_model(raw)
    got = charges(model, side, qty, price)
    applies = {c["name"] for c in raw["components"] if side in c["sides"]}
    assert set(got.by_component) == applies
    taxable = D(0)
    for component in raw["components"]:
        if component["name"] not in applies:
            continue
        amount = D(component["rate"]) * price * qty + D(component.get("per_order", "0"))
        if "cap" in component:
            amount = min(amount, D(component["cap"]))
        assert got.by_component[component["name"]] == _money(amount) >= 0
        if component["taxable"]:
            taxable += got.by_component[component["name"]]
    assert got.gst == _money(taxable * D(raw["gst_rate"])) >= 0
    assert got.total == sum(got.by_component.values(), D(0)) + got.gst
    assert charges(model, side, qty + more, price).total >= got.total
    assert charges(model, side, qty, price + D(1)).total >= got.total


@given(st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.booleans(), st.none()))
def test_charges_contract_float_rates(rate: object) -> None:
    """A rate written as a float (or a boolean, or nothing) is refused: binary rounding never
    reaches money."""
    with pytest.raises(CostError):
        parse_cost_model(
            {
                "version": "x",
                "effective_from": "2016-01-01",
                "gst_rate": rate,
                "dp_per_sell_day": "0",
                "components": [],
            }
        )


@pytest.mark.parametrize(
    ("cash", "price", "bps"),
    [
        (D("100000"), D("250.05"), D(20)),
        (D("1000000"), D("0.5"), D(20)),
        (D("12345.67"), D("12345.67"), D(0)),
        (D("500"), D("499.9"), D(20)),
        (D("0"), D("10"), D(20)),
        (D("-1"), D("10"), D(20)),
    ],
)
def test_affordable_is_the_largest_quantity_whose_outlay_fits(cash: D, price: D, bps: D) -> None:
    """The cash a backtest entry is sized on is net of its slippage and charges: the quantity
    `affordable` returns fits the cash with both, and one more share does not."""
    fills = BacktestFills(load_cost_schedule(), bps)
    qty = fills.affordable(cash, price, DAY)
    assert qty >= 0
    if qty:
        filled, fees = fills.fill("buy", qty, price, DAY)
        assert fills.outlay(qty, price, DAY) == filled * qty + fees <= cash
    assert fills.outlay(qty + 1, price, DAY) > cash
    assert qty <= max(cash, D(0)) // price  # never more than the cash term without the reserve


def test_cash_bound_fills_refuse_an_overdraw() -> None:
    """A buy whose value plus charges exceeds the cash is refused, as the broker refuses it; a
    buy that fits and every sell fill as the run's fills do."""
    fills = BacktestFills(load_cost_schedule(), D(20))
    price = D("100")
    qty = fills.affordable(D("10000"), price, DAY)
    bound = fills.within(D("10000"))
    assert isinstance(bound, CashBoundFills)
    assert bound.fill("buy", qty, price, DAY) == fills.fill("buy", qty, price, DAY)
    assert bound.fill("sell", qty + 1, price, DAY) == fills.fill("sell", qty + 1, price, DAY)
    with pytest.raises(CostError, match="cost_cash_overdraw"):
        bound.fill("buy", qty + 1, price, DAY)


@pytest.mark.parametrize(
    ("day", "effective_from"),
    [
        (date(2016, 1, 1), date(2016, 1, 1)),
        (date(2017, 3, 31), date(2016, 6, 1)),
        (date(2017, 6, 30), date(2017, 4, 1)),
        (date(2017, 7, 1), date(2017, 7, 1)),
        (date(2024, 9, 30), date(2024, 4, 1)),
        (date(2024, 10, 1), date(2024, 10, 1)),
        (date(2026, 10, 23), date(2024, 10, 1)),
    ],
)
def test_schedule_picks_the_file_in_force(day: date, effective_from: date) -> None:
    """A fill is charged under the latest file effective on or before its trade date."""
    assert load_cost_schedule().on(day).effective_from == effective_from


def test_schedule_refuses_a_day_before_its_first_file_and_records_the_window() -> None:
    """No charges exist before the first file; a run records each file in force in its window."""
    schedule = load_cost_schedule()
    before = date(2015, 12, 31)
    with pytest.raises(CostError, match="cost_no_schedule"):
        schedule.on(before)
    recorded = schedule.to_json(date(2020, 5, 4), date(2021, 2, 1))
    assert recorded["version"] == "cnc-delivery-2"
    assert [s["effective_from"] for s in recorded["schedules"]] == [
        "2019-04-01",
        "2020-06-01",
        "2020-07-01",
        "2021-01-01",
    ]
    with pytest.raises(CostError, match="cost_bad_schedule"):
        CostSchedule("x", ())


@pytest.mark.parametrize(
    ("day", "side", "price", "want"),
    [
        # 100 x 500 = 50,000 on 2025-01-10: STT 50.00, stamp 7.50, NSE 0.00297% 1.485 -> 1.49,
        # SEBI 0.05, IPFT 0.05, GST 18% of 1.59 = 0.2862 -> 0.29
        (date(2025, 1, 10), "buy", D(500), D("59.38")),
        # 100 x 550 = 55,000: STT 55.00, NSE 1.6335 -> 1.63, SEBI 0.055 -> 0.06, IPFT 0.06,
        # GST 18% of 1.75 = 0.315 -> 0.32, DP 14.75
        (date(2025, 1, 10), "sell", D(550), D("71.82")),
        # 2016-03-01: STT 55.00, Maharashtra stamp 0.01% 5.50 on the sell too, NSE 0.00325%
        # 1.7875 -> 1.79, SEBI Rs 20/cr 0.11 (no tax), service tax 14.5% of 1.79 = 0.25955
        # -> 0.26, DP 17.75 (Rs 15.50 + 14.5%)
        (date(2016, 3, 1), "sell", D(550), D("80.41")),
        # 2018-06-01 buy 50,000: STT 50.00, stamp 0.01% 5.00, NSE 1.625 -> 1.63, SEBI Rs 15/cr
        # 0.075 -> 0.08 (no GST), GST 18% of 1.63 = 0.2934 -> 0.29
        (date(2018, 6, 1), "buy", D(500), D("57.00")),
        # 2021-06-01 sell 55,000: STT 55.00, no stamp, NSE 0.00345% 1.8975 -> 1.90, SEBI 0.06
        # (no GST), GST 18% of 1.90 = 0.342 -> 0.34, DP 18.29 (Rs 15.50 + 18%)
        (date(2021, 6, 1), "sell", D(550), D("75.59")),
        # 2022-09-01 sell 55,000: as above but SEBI's fee bears GST (from 2022-07-18): GST 18%
        # of 1.96 = 0.3528 -> 0.35, and DP 14.75 (Rs 12.50 + 18%, from 2021-08-05)
        (date(2022, 9, 1), "sell", D(550), D("72.06")),
    ],
)
def test_charges_hand_worked(day: date, side: str, price: D, want: D) -> None:
    """The charges of a 100-share fill, worked by hand from the file in force."""
    fills = BacktestFills(load_cost_schedule(), D(0))
    assert fills.fill(side, 100, price, day) == (price, want)


def test_dp_once_per_instrument_per_sell_day() -> None:
    """A sell pays the flat DP charge; a later sale of the instrument the same day does not, and
    a buy never does."""
    fills = BacktestFills(load_cost_schedule(), D(0))
    _, first = fills.fill("sell", 10, D(550), DAY)
    _, again = fills.fill("sell", 10, D(550), DAY, sold_today=True)
    assert first - again == D("14.75")
    _, buy = fills.fill("buy", 10, D(550), DAY)
    _, buy_flagged = fills.fill("buy", 10, D(550), DAY, sold_today=True)
    assert buy == buy_flagged


def test_a_limit_buy_never_fills_above_its_limit() -> None:
    """Slippage moves a buy up, but a LIMIT order fills at most at its limit; a sell is never
    capped. 100 at 500 under 20 bps slips to 501.0000; the limit holds it at 500."""
    fills = BacktestFills(load_cost_schedule(), D(20))
    assert fills.fill("buy", 100, D(500), DAY)[0] == D("501.0000")
    capped = fills.within(D(10**7), D(500))
    price, fees = capped.fill("buy", 100, D(500), DAY)
    assert (
        (price, fees) == fills.fill("buy", 100, D(500), DAY, limit=D(500)) == (D(500), D("59.38"))
    )
    assert capped.fill("sell", 100, D(500), DAY)[0] == D("499.0000")
