"""PBT costs.* (09 §2, HAT-BT-022, HAT-BT-023): slippage and statutory charges of
`app.hat.backtest.costs` on generated cost models and fills, in Decimal and compared exactly,
against the rules of 09 §2 written out here."""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

import hat_backtest_support as s
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from app.hat.backtest.costs import (
    MAX_SLIPPAGE_BPS,
    BacktestFills,
    CostError,
    charges,
    check_slippage,
    load_cost_model,
    parse_cost_model,
    slipped,
)
from app.hat.engine.backtest import run_backtest

D = Decimal
PAISE = D("0.01")
PLACES = D("0.0001")
PRICE = st.integers(1, 10_000_000).map(lambda n: D(n) / 10_000)  # 0.0001 .. 1000.0000
QTY = st.integers(1, 100_000)
BPS = st.integers(0, 9_999).map(D) | st.integers(0, 99_990).map(lambda n: D(n) / 10)
SIDE = st.sampled_from(["buy", "sell"])
RATE = st.integers(0, 50_000).map(lambda n: str(D(n) / 10_000_000))  # 0 .. 0.5%


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
    return {"version": "gen", "gst_rate": draw(RATE), "components": components}


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
    model = load_cost_model()

    def net(bps: D) -> D:
        fills = BacktestFills(model, bps)
        bought, buy_fees = fills.fill("buy", qty, entry)
        sold, sell_fees = fills.fill("sell", qty, exit_)
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


DAYS = s.sessions(34)


@given(s.paths(34), st.integers(0, 50).map(D))
@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_charges_contract_run(paths: dict, bps: D) -> None:
    """In a run, every fill's fees are the cost model's charges on its slipped price, the
    trades' net P&L is their gross P&L minus those charges exactly, and the metrics name the
    cost model's version and digest."""
    model = load_cost_model()
    result = run_backtest(s.spec(s.config(), DAYS, slippage=bps), s.market(paths, DAYS))
    assert result.metrics["cost_model"] == {"version": "cnc-delivery-1", "digest": model.digest}
    for fill in result.fills:
        assert fill.fees == charges(model, fill.side, fill.qty, fill.price).total
    # every position is closed at the end of the period, so the fills are exactly the trades'
    gross = sum((f.price * f.qty * (1 if f.side == "sell" else -1) for f in result.fills), D(0))
    fees = sum((f.fees for f in result.fills), D(0))
    assert sum((t.fees for t in result.trades), D(0)) == fees
    assert sum((t.realized_pnl for t in result.trades), D(0)) == gross - fees


@given(st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.booleans(), st.none()))
def test_charges_contract_float_rates(rate: object) -> None:
    """A rate written as a float (or a boolean, or nothing) is refused: binary rounding never
    reaches money."""
    with pytest.raises(CostError):
        parse_cost_model({"version": "x", "gst_rate": rate, "components": []})
