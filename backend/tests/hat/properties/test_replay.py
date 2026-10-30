"""The shadow-day replay's comparison (`daily/replay.py`) on hand-built sides: each kind of
difference, its cause, the facts it reads and the exit check on each side's scale."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import polars as pl
import pytest

from app.hat.daily.replay import (
    Book,
    ExitCheck,
    Facts,
    ReplayError,
    RotationNight,
    Side,
    Watch,
    backtest_sizing,
    cause_of,
    compare,
    exit_check,
    facts_of,
    merged,
    no_later,
    prices_0920_of,
    rotation_night,
)
from app.hat.engine.exits import PositionState
from app.hat.engine.rotation import orders
from app.hat.engine.rotation_live import rebalance_orders
from app.hat.engine.strategy.config import ExitCloseBelowSma, ExitHardStop
from app.hat.engine.strategy.versions import seed_config

D = Decimal
DAY = date(2024, 1, 31)


def _side(**facts: Facts) -> Side:
    return Side(facts=dict(facts))


@pytest.mark.parametrize(
    ("live", "backtest", "cause"),
    [
        (Facts(member=True), Facts(), "membership"),
        (Facts(member=True, excluded=True), Facts(member=True), "quality_excluded"),
        (Facts(member=True), Facts(member=True, pending=True), "factor_pending"),
        (Facts(member=True, bars=10), Facts(member=True), "no_bars"),
        (Facts(member=True, bars=10), Facts(member=True, bars=9), "bars_differ"),
        (Facts(bars=10, ret=D("0.1")), Facts(bars=10, ret=D("0.2")), "bars_differ"),
        (
            Facts(member=True, bars=10, ret=D("0.1")),
            Facts(member=True, bars=10, ret=D("0.1")),
            "same_inputs",
        ),
    ],
)
def test_the_cause_is_the_first_input_that_differs(
    live: Facts, backtest: Facts, cause: str
) -> None:
    assert cause_of("x", _side(x=live), _side(x=backtest)) == cause


def test_a_rotation_reads_its_own_membership() -> None:
    live = _side(x=Facts(member=True, rotation_member=True))
    backtest = _side(x=Facts(member=True))
    assert cause_of("x", live, backtest) == "same_inputs"
    assert cause_of("x", live, backtest, rotation_=True) == "membership"


def test_watchlist_rows_scores_and_planned_levels_are_compared() -> None:
    same = Facts(member=True, bars=5, ret=D(0))
    live = _side(a=same, b=same, c=Facts(member=True, bars=5))
    backtest = _side(a=same, b=same, c=Facts(bars=5))
    key = ("bot", "pb")
    live.watchlist = {
        (*key, "a"): Watch(D("0.9"), D("1.02"), D("0.95")),
        (*key, "b"): Watch(D("0.8")),
        (*key, "c"): Watch(D("0.7")),
    }
    backtest.watchlist = {
        (*key, "a"): Watch(D("0.9"), D("1.03"), D("0.95")),
        (*key, "b"): Watch(D("0.7")),
    }
    found = {
        (d.kind, d.key, d.field): (d.live, d.backtest, d.cause) for d in compare(live, backtest)
    }
    assert found == {
        ("signal", "bot:pb:a", "level"): ("1.02", "1.03", "same_inputs"),
        ("watchlist", "bot:pb:b", "score"): ("0.8", "0.7", "same_inputs"),
        ("watchlist", "bot:pb:c", "row"): ("listed", None, "membership"),
    }


def test_gates_and_identical_sides_compare_clean() -> None:
    live, backtest = Side(gates={"bot": True}), Side(gates={"bot": True})
    assert compare(live, backtest) == []
    backtest.gates["bot"] = False
    (only,) = compare(live, backtest)
    assert (only.kind, only.cause) == ("gate", "regime_inputs")


def test_rotation_ranks_targets_and_gates_are_compared_with_their_causes() -> None:
    same = Facts(rotation_member=True, bars=300, ret=D("0.1"))
    live = _side(a=same, b=same, c=same)
    backtest = _side(a=same, b=same, c=Facts(bars=300, ret=D("0.1")))
    live.rotation["bot"] = RotationNight(True, (("a", D(2)), ("b", D(1)), ("c", D(0))), ("a", "b"))
    backtest.rotation["bot"] = RotationNight(False, (("b", D("1.5")), ("a", D("1.4"))), ("b", "a"))
    found = {(d.kind, d.key): d.cause for d in compare(live, backtest)}
    assert found == {
        ("rotation_gate", "bot"): "regime_inputs",
        ("rotation_rank", "bot:a"): "passers_differ",  # c's absence moved the z-scores
        ("rotation_rank", "bot:b"): "passers_differ",
        ("rotation_rank", "bot:c"): "membership",
    }
    backtest.rotation["bot"] = RotationNight(True, live.rotation["bot"].ranking[:2], ("a",))
    backtest.facts["c"] = same
    found = {(d.kind, d.key): d.cause for d in compare(live, backtest)}
    assert found == {
        ("rotation_rank", "bot:c"): "same_inputs",
        ("rotation_target", "bot:b"): "same_inputs",
    }


def test_exits_compare_their_sales_and_new_stops() -> None:
    same = Facts(member=True, bars=5, ret=D(0))
    live, backtest = _side(i=same), _side(i=Facts(member=True))
    live.held = backtest.held = {"p1": "i", "p2": "i", "p3": "i"}
    live.exits = {"p1": ExitCheck(("full:hard_stop",)), "p2": ExitCheck((), D("0.97")), "p3": None}
    backtest.exits = {"p1": ExitCheck(()), "p2": ExitCheck((), D("0.97")), "p3": ExitCheck()}
    found = {(d.key, d.field): (d.live, d.backtest, d.cause) for d in compare(live, backtest)}
    assert found == {
        ("p1", "actions"): ("full:hard_stop", "", "no_bars"),
        ("p3", "view"): (None, "evaluated", "no_bars"),
    }


def _bars(closes: list[float], *, raw: float | None = None, name: str = "i") -> pl.DataFrame:
    days = [date(2024, 1, n) for n in range(31 - len(closes) + 1, 32)]
    frame = pl.DataFrame(
        {
            "instrument_id": [name] * len(closes),
            "date": days,
            "close": closes,
            "low": closes,
            "high": closes,
        }
    )
    return frame if raw is None else frame.with_columns(raw_close=pl.lit(raw))


def test_facts_count_the_bars_and_the_return_through_the_day() -> None:
    frame = _bars([100.0] * 10 + [110.0])
    found = facts_of(frame, DAY)
    assert found == {"i": (11, D("0.1000"))}
    assert facts_of(frame, date(2024, 1, 21)) == {"i": (1, None)}


def test_exit_check_evaluates_the_close_on_the_sides_scale() -> None:
    rules = [ExitHardStop(rule="hard_stop"), ExitCloseBelowSma(rule="close_below_sma", sma=3)]
    state = PositionState(10, 10, D(100), D(90), D(90))
    entry = date(2024, 1, 28)
    # the close 95 under its 3-session SMA of the sessions before (100): the SMA rule sells
    found = exit_check(_bars([100.0, 100.0, 100.0, 95.0]), "i", entry, DAY, rules, state)
    assert found == ExitCheck(("full:1:close_below_sma",), None)
    # bars on twice the raw prices' scale: the stop (90 raw) is 180 there; a close of 170 (85
    # raw) is under it, a close of 190 (95 raw) is not
    hard = rules[:1]
    under = exit_check(_bars([200.0] * 3 + [170.0], raw=85.0), "i", entry, DAY, hard, state)
    assert under == ExitCheck(("full:0:hard_stop",), None)
    over = exit_check(_bars([200.0] * 3 + [190.0], raw=95.0), "i", entry, DAY, hard, state)
    assert over == ExitCheck()
    no_bar = _bars([100.0, 95.0]).filter(pl.col("date") < DAY)
    assert exit_check(no_bar, "i", entry, DAY, rules, state) is None


def test_rotation_night_targets_the_best_from_an_empty_book() -> None:
    config = seed_config("momentum-rotation")
    assert config.rotation is not None
    ranks = [(f"n{k:02}", float(100 - k)) for k in range(30)]
    night = rotation_night(config, ranks, True, Book(), {}, backtest_sizing)
    assert night.target == tuple(f"n{k:02}" for k in range(config.rotation.top_n))
    assert night.ranking[0] == ("n00", D("100.000000"))
    assert night.capital is None  # no allocation: not sized
    assert night.orders == ()


def test_both_sizings_agree_on_a_book_marked_at_its_prices() -> None:
    config = seed_config("momentum-rotation")
    ranks = [(f"n{k:02}", float(100 - k)) for k in range(30)]
    # held: n00 bought at 50, now 100; n35 (unranked) bought at 20, now 10
    book = Book(D(1_000_000), {"n00": [(1000, D(50))], "n35": [(500, D(20))]})
    prices = {f"n{k:02}": D(100) for k in range(36)}
    prices["n35"] = D(10)
    live = rotation_night(config, ranks, True, book, prices, rebalance_orders)
    backtest = rotation_night(config, ranks, True, book, prices, backtest_sizing)
    assert live == backtest
    assert live.capital == D("1045000.00")  # 1,000,000 + 50,000 - 5,000
    assert live.orders[0] == ("n35", "sell", 500)
    # a sizing on the cost basis (the reviewed defect) shows as a capital difference
    at_cost = rotation_night(config, ranks, True, book, prices, _cost_sizing)
    found = compare(_with_night(live), _with_night(at_cost))
    assert ("rotation_size", "bot", "capital", "sizing_basis") in {
        (d.kind, d.key, d.field, d.cause) for d in found
    }
    assert any(d.kind == "rotation_order" and d.cause == "capital_differs" for d in found)


def _cost_sizing(chosen, lots, prices, at_cost, rules):  # noqa: ANN001, ANN202
    held = {name: sum(q for q, _ in parts) for name, parts in lots.items()}
    return at_cost, orders(chosen, held, prices, at_cost, rules)


def _with_night(night: RotationNight) -> Side:
    return Side(rotation={"bot": night})


def test_order_differences_name_a_price_difference_first() -> None:
    a = RotationNight(True, (), ("x",), D(100), (("x", "buy", 10),), (("x", D(5)),))
    b = RotationNight(True, (), ("x",), D(100), (("x", "buy", 9),), (("x", D("5.5")),))
    found = compare(_with_night(a), _with_night(b))
    assert [(d.kind, d.cause) for d in found] == [("rotation_order", "prices_differ")]


def test_merged_keeps_one_row_per_instrument_and_session() -> None:
    first = _bars([1.0, 2.0], name="i")
    second = pl.concat([_bars([1.0, 2.0], name="i"), _bars([5.0], name="j")])
    found = merged(merged(pl.DataFrame(), first), second)
    assert sorted(found["instrument_id"].to_list()) == ["i", "i", "j"]
    assert merged(first, pl.DataFrame()) is first


def test_both_sides_size_on_the_same_0920_prices() -> None:
    from app.hat.data.bars import Bar
    from app.hat.engine.fill_0920 import OPEN_TO_0920, estimated_0920
    from app.hat.time import IST

    first = datetime.combine(DAY, time(9, 15), tzinfo=IST)
    readable = (Bar("a", first, D(100), D(102), D(99), D(101), 10),)
    off_basis = (Bar("b", first, D(150), D(151), D(149), D(150), 10),)  # 50% off its daily open
    opens = {"a": D(100), "b": D(100), "c": D(50)}
    found = prices_0920_of(DAY, {"a": readable, "b": off_basis}, opens)
    assert found["a"] == D(101)  # the 09:15 bar's close
    assert found["b"] == estimated_0920(D(100), "buy", OPEN_TO_0920)  # refused: the estimate
    assert found["c"] == estimated_0920(D(50), "buy", OPEN_TO_0920)  # no bars: the estimate


def test_no_later_refuses_a_frame_past_the_day() -> None:
    frame = _bars([1.0, 2.0])
    assert no_later(frame, DAY, date(2024, 2, 1)) is frame
    with pytest.raises(ReplayError, match="replay_lookahead"):
        no_later(frame, date(2024, 1, 30), DAY)
