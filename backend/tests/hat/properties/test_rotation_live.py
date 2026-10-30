"""A rotation's live inputs and its buy's risk check, on small hand-built inputs: `decide` under
`ROTATION_BUY`, `sizing.rotation_quantity` and the closes window the live ranking reads
(`rotation_live.closes_window`)."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from app.hat.daily.books import sold_gain
from app.hat.data.live import PRICE_OUT_OF_BAND, PRICE_STALE, PRICE_UNKNOWN
from app.hat.engine.rotation import SELL, TOP_UP, TRIM, Order, Plan, book_equity, targets
from app.hat.engine.rotation_live import (
    Rebalanced,
    closes_window,
    priced_targets,
    rebalance_orders,
    waits_for_prices,
)
from app.hat.engine.strategy.config import Rotation
from app.hat.risk.decision import ROTATION_BUY, decide
from app.hat.risk.kill import KillState
from app.hat.risk.sizing import UNBOUNDED_CASH, SizingInputError, rotation_quantity
from app.hat.time import IST
from tests.hat.hat_risk_support import TRADE_DAY, valid_ctx

D = Decimal


def _buy(**changes: object) -> object:
    """A rotation buy every check accepts: 100 shares at 100 with ample cash, no stop."""
    return decide(
        valid_ctx(
            **{
                "action": ROTATION_BUY,
                "quantity": 100,
                "stop_price": None,
                "adv20": None,
                "playbook": "rotation",
                "is_virtual": True,
                **changes,
            }
        )
    )


def test_a_rotation_buy_takes_its_shares_with_no_stop_or_risk() -> None:
    found = _buy()
    assert found.accepted
    assert found.quantity == 100
    assert found.stop == 0
    assert found.risk_amount == 0
    assert found.binding == "rotation"
    assert found.warnings == ()


def test_a_rotation_buy_is_cut_to_the_free_cash() -> None:
    found = _buy(free_cash=D(2550))
    assert found.accepted
    assert found.quantity == 25
    assert found.warnings == ("size_bound_by_cash",)
    assert _buy(free_cash=D(99)).reason == "size_zero_cash"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"kill": KillState(account_tripped=True)}, "kill_account"),
        ({"kill": KillState(bot_tripped=True)}, "kill_bot"),
        ({"kill": KillState(health_tripped=True)}, "kill_health"),
        ({"kill": KillState(paused=True)}, "bot_paused"),
        ({"at": datetime.combine(TRADE_DAY, time(16, 0), tzinfo=IST)}, "market_closed"),
        ({"feed_stale": True}, "stale_feed"),
        ({"quality_failed": True}, "quality_failed"),
        ({"quantity": 0}, "size_zero_position"),
        ({"quantity": None}, "missing_input"),
        ({"free_cash": None}, "missing_input"),
        ({"entry_price": 100.0}, "missing_input"),
    ],
)
def test_a_rotation_buy_is_refused_as_an_entry_is(changes: dict[str, object], reason: str) -> None:
    found = _buy(**changes)
    assert not found.accepted
    assert found.reason == reason


def test_a_rotation_buy_reads_no_regime_gate_slot_or_open_risk() -> None:
    # the gate, the slots and the day's entry count bind a swing entry, never a rotation buy
    assert _buy(gate_on=False, entries_today=10_000, open_positions=None).accepted


def test_an_entry_still_needs_its_stop() -> None:
    assert decide(valid_ctx(quantity=100, stop_price=None)).reason == "missing_input"


def test_rotation_quantity_floors_to_the_cash() -> None:
    assert rotation_quantity(10, D(100), D(5000)) == (10, "rotation")
    assert rotation_quantity(10, D(100), D(999)) == (9, "cash")
    assert rotation_quantity(10, D(100), UNBOUNDED_CASH) == (10, "rotation")
    for bad in (0, -1, True):
        with pytest.raises(SizingInputError):
            rotation_quantity(bad, D(100), D(5000))  # type: ignore[arg-type]
    with pytest.raises(SizingInputError):
        rotation_quantity(1, D(0), D(5000))


def _bars(rows: list[tuple[str, date, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["instrument_id", "date", "close"], orient="row")


def test_closes_window_lays_out_the_last_sessions_through_the_night() -> None:
    days = [date(2024, 1, d) for d in (1, 2, 3, 4)]
    bars = _bars(
        [
            ("a", days[0], 10.0),
            ("a", days[1], 11.0),
            ("a", days[3], 13.0),  # no bar on the 3rd
            ("b", days[1], 20.0),
            ("b", days[2], 21.0),
            ("b", days[3], 22.0),
            ("z", days[3], 99.0),  # not asked for
        ]
    )
    found = closes_window(bars, ["a", "b"], days[2], 3)  # the 4th is after the night
    assert found[:2, 0].tolist() == [10.0, 11.0]
    assert math.isnan(found[2, 0])
    assert math.isnan(found[0, 1])
    assert found[1:, 1].tolist() == [20.0, 21.0]
    # more rows than sessions: the oldest rows stay empty
    longer = closes_window(bars, ["b"], days[3], 6)
    assert np.isnan(longer[:3, 0]).all()
    assert longer[3:, 0].tolist() == [20.0, 21.0, 22.0]


def test_closes_window_has_no_column_without_names() -> None:
    bars = _bars([("a", date(2024, 1, 1), 1.0)])
    assert closes_window(bars, [], date(2024, 1, 1), 3).shape == (3, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"playbook": "s1-vcp"},  # another playbook's entry cannot skip the stop and caps
        {"playbook": None},
        {"is_virtual": False},  # a real order
        {"is_virtual": None},
    ],
)
def test_a_rotation_buy_is_refused_unless_it_is_the_rotation_playbooks_on_paper(
    changes: dict[str, object],
) -> None:
    found = _buy(**changes)
    assert not found.accepted
    assert found.reason == "rotation_not_paper"


RULES = Rotation.model_construct(
    short_sessions=2, long_sessions=4, skip_sessions=1, vol_sessions=4, min_bars=3,
    top_n=2, buffer_rank=2, reweight=True, min_change_pct=2.0, market_filter=False,
)  # fmt: skip


def test_the_capital_counts_the_holdings_at_their_prices() -> None:
    # the review's case: capital 100 in two names at 50; A doubles. At cost the book is 100
    # (sizing 50 each would trim the winner to 50 and idle cash); marked it is 150: 75 each
    lots = {"A": [(1, D(50))], "B": [(1, D(50))]}
    prices = {"A": D(100), "B": D(50)}
    assert book_equity(D(100), lots, prices) == D(150)
    assert book_equity(D(100), lots, {"B": D(40)}) == D(90)  # A unpriced: at its cost
    chosen = Plan(date(2024, 1, 31), (), ("A", "B"))
    lots = {"A": [(10, D(50))], "B": [(10, D(50))]}  # 1000 at cost: A worth 1000, B 500
    equity, orders = rebalance_orders(chosen, lots, prices, D(1000), RULES)
    assert equity == D(1500)
    assert orders == [
        Order("A", TRIM, 2),  # 1000 -> 750: 2.5 shares, floored
        Order("B", TOP_UP, 5),
    ]


def test_a_top_up_and_a_trim_reach_the_same_target_from_lots() -> None:
    chosen = Plan(date(2024, 1, 31), (), ("A",))
    lots = {"A": [(4, D(100)), (2, D(120))], "X": [(3, D(10))]}
    equity, orders = rebalance_orders(chosen, lots, {"A": D(100), "X": D(10)}, D(1000), RULES)
    assert equity == D(1000) - D(40)  # the top-up lot at 120 now at 100
    assert orders[0] == Order("X", SELL, 3)
    assert orders[1] == Order("A", TRIM, 1)  # 600 held, 480 the target, floor(1.2)


def test_a_partial_sale_books_its_gain_or_loss() -> None:
    assert sold_gain(D(100), 10, D(150), D(3)) == D(497)
    assert sold_gain(D(100), 10, D(90), D(3)) == D(-103)


def test_a_waiting_sale_of_a_name_back_in_the_target_is_off() -> None:
    done = Rebalanced(date(2024, 2, 1))
    done.waiting = {"X": "rotation_out", "Y": "rotation_out"}
    later = Rebalanced(date(2024, 2, 1), waiting={"Z": "rotation_not_eligible"}, target=("X", "W"))
    later.refused = {"W": "kill_bot"}
    done.absorb(later)
    assert done.waiting == {"Y": "rotation_out", "Z": "rotation_not_eligible"}
    assert done.refused == {"W": "kill_bot"}
    assert done.target == ("X", "W")


def _quotes(table: dict[str, tuple[Decimal | None, str | None]]):  # noqa: ANN202
    return lambda name: table.get(name, (D(10), None))


def test_a_name_without_a_tick_is_not_passed_over_for_a_lower_one() -> None:
    ranking = [("a", 3.0), ("b", 2.0), ("c", 1.0)]
    # b has no tick yet: the selection fills with c, and says so, so the caller trades nothing
    target, _, _, unpriced = priced_targets(
        ranking, {}, RULES, gate_on=True, price=_quotes({"b": (None, PRICE_UNKNOWN)})
    )
    assert target == ("a", "c")
    assert unpriced == ["b"]
    stale = priced_targets(
        ranking, {}, RULES, gate_on=True, price=_quotes({"a": (None, PRICE_STALE)})
    )
    assert stale[3] == ["a"]


def test_a_name_outside_its_band_is_passed_over_as_unbuyable() -> None:
    ranking = [("a", 3.0), ("b", 2.0), ("c", 1.0)]
    target, _, quote, unpriced = priced_targets(
        ranking, {}, RULES, gate_on=True, price=_quotes({"b": (None, PRICE_OUT_OF_BAND)})
    )
    assert target == ("a", "c")
    assert unpriced == []
    assert quote("a") == D(10)


CUTOFF = datetime.combine(TRADE_DAY, time(9, 45), tzinfo=IST)


def test_a_rebalance_waits_for_prices_only_until_the_cutoff() -> None:
    before = datetime.combine(TRADE_DAY, time(9, 30), tzinfo=IST)
    assert waits_for_prices(["b"], before, CUTOFF)
    assert not waits_for_prices(["b"], CUTOFF, CUTOFF)  # at the cutoff: trade the rest
    assert not waits_for_prices(["b"], datetime.combine(TRADE_DAY, time(9, 46), tzinfo=IST), CUTOFF)
    assert not waits_for_prices([], before, CUTOFF)  # every name priced: nothing to wait for


def test_after_the_cutoff_an_unpriced_name_is_passed_over_as_one_with_no_bar() -> None:
    ranking = [("a", 4.0), ("b", 3.0), ("c", 2.0), ("d", 1.0)]
    live, _, _, unpriced = priced_targets(
        ranking, {"d": 5}, RULES, gate_on=True, price=_quotes({"b": (None, PRICE_STALE)})
    )
    # the backtest's selection with b unbuyable (no bar at the open): the same shared rule
    backtest, _ = targets(ranking, {"d": 5}, RULES, gate_on=True, can_buy=lambda n: n != "b")
    assert live == backtest
    assert unpriced == ["b"]
