"""An overlap run's seams (`RunConventions.overlap`): the allocator's whole-book cap leaving the
rotation out, the leg weights' fit, the order a funding sale sells in, the shares it sells, the
rotation's capital at its next rebalance and the convention's record. Small hand-built inputs;
no run, no database."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from hat_risk_support import policy_with

from app.hat.backtest.portfolio import parse_portfolio
from app.hat.backtest.rotation import funding_order, overlap_capital, shares_to_fund
from app.hat.backtest.spec import (
    ENTRY_BARS_DAILY,
    OVERLAP_FUNDING,
    OVERLAP_ROTATION_CAPITAL,
    BacktestError,
    RunConventions,
    weights_fit,
)
from app.hat.engine.portfolio.allocator import SleeveInput, allocate
from app.hat.engine.portfolio.health import Health
from tests.hat.hat_backtest_support import config, sessions, spec, zero_fills

D = Decimal
ROTATION, VCP = "rotation", "vcp"


def _sleeve(key: str) -> SleeveInput:
    return SleeveInput(
        bot_id=uuid.uuid4(),
        sleeve=key,
        cluster=key,
        lifecycle_state="live",
        regime_table_value=D(1),
        health=Health(D(1), probation=False),
    )


def _weights(overlap: str | None) -> list[Decimal]:
    policy = policy_with(base_weight={ROTATION: "1.0", VCP: "0.3"}, cluster_cap="1.0")
    found = allocate([_sleeve(ROTATION), _sleeve(VCP)], policy, D(1_000_000), overlap=overlap)
    return [a.final_weight for a in found]


def test_the_book_cap_scales_both_legs_without_overlap() -> None:
    rotation, vcp = _weights(None)
    assert rotation + vcp <= 1
    assert rotation == (D("1.0") / D("1.3")).quantize(D("1E-12"), rounding="ROUND_DOWN")


def test_overlap_leaves_the_rotation_out_of_the_book_cap() -> None:
    assert _weights(ROTATION) == [D("1.0"), D("0.3")]


def test_overlap_still_caps_the_setup_sleeves_book() -> None:
    policy = policy_with(base_weight={ROTATION: "1.0", "a": "0.7", "b": "0.6"}, cluster_cap="1.0")
    found = allocate(
        [_sleeve(ROTATION), _sleeve("a"), _sleeve("b")], policy, D(1), overlap=ROTATION
    )
    rotation, a, b = (x.final_weight for x in found)
    assert rotation == D("1.0")
    assert a + b <= 1
    assert a > b


@pytest.mark.parametrize(
    ("weights", "rotations", "overlap", "fits"),
    [
        ([D("1.0"), D("0.3")], [True, False], False, False),
        ([D("1.0"), D("0.3")], [True, False], True, True),
        ([D("0.7"), D("0.3")], [True, False], False, True),
        ([D("1.0"), D("0.6"), D("0.5")], [True, False, False], True, False),
        ([D("1.1"), D("0.3")], [True, False], True, False),  # above the cluster cap
        ([D("1.0"), D(0)], [True, False], True, False),
    ],
)
def test_weights_fit(
    weights: list[Decimal], rotations: list[bool], overlap: bool, fits: bool
) -> None:
    assert weights_fit(weights, rotations, D("1.0"), overlap=overlap) is fits


def test_funding_sells_unranked_then_the_lowest_rank_first() -> None:
    ranks = {"a": 1, "b": 7, "c": 3}
    assert funding_order(["a", "b", "c", "z", "y"], ranks) == ["y", "z", "b", "c", "a"]


def test_shares_to_fund_are_the_fewest_that_cover_the_need() -> None:
    def net(qty: int) -> Decimal:  # ₹100 a share less a ₹15 charge
        return D(100) * qty - D(15)

    assert shares_to_fund(D(0), 10, net) == 0
    assert shares_to_fund(D(185), 10, net) == 2
    assert shares_to_fund(D(186), 10, net) == 3
    assert shares_to_fund(D(10_000), 10, net) == 10  # short: all of them


def test_the_rotation_sizes_on_its_weight_less_what_it_still_funds() -> None:
    equity = D(1_000_000)
    assert overlap_capital(D(1), equity, []) == equity
    # 50,000 funded a position half sold since; 20,000 one still whole
    funded = [(D(50_000), 50, 100), (D(20_000), 10, 10)]
    assert overlap_capital(D(1), equity, funded) == D(955_000)
    assert overlap_capital(D("0.01"), equity, funded) == 0  # never below 0


def test_overlap_is_recorded_only_when_set() -> None:
    def record(conventions: RunConventions) -> dict[str, object]:
        window = (date(2020, 1, 1), date(2020, 12, 31))
        return conventions.record(zero_fills(), window, D("0.3"), {})

    plain = record(RunConventions(entry_bars=ENTRY_BARS_DAILY))
    overlap = record(RunConventions(entry_bars=ENTRY_BARS_DAILY, overlap=True))
    assert "overlap" not in plain
    assert overlap == {
        **plain,
        "overlap": {"funding": OVERLAP_FUNDING, "rotation_capital": OVERLAP_ROTATION_CAPITAL},
    }


def test_overlap_needs_a_rotation_among_several_legs() -> None:
    with pytest.raises(BacktestError, match="backtest_bad_overlap"):
        spec(config(), sessions(30), conventions=RunConventions(overlap=True))
    with pytest.raises(BacktestError, match="backtest_bad_overlap"):
        RunConventions(overlap="yes")  # type: ignore[arg-type]


def test_a_portfolio_file_turns_overlap_on() -> None:
    doc = {
        "portfolio": "r4",
        "universe": "NIFTY 500",
        "window": {"from": "2016-01-01", "to": "2021-12-31"},
        "strategies": [{"version": str(uuid.uuid4())}],
        "conventions": {"overlap": True},
    }
    assert parse_portfolio(doc, lambda _: policy_with()).conventions.overlap is True
    with pytest.raises(BacktestError, match="backtest_bad_overlap"):
        parse_portfolio({**doc, "conventions": {"overlap": "true"}}, lambda _: policy_with())
