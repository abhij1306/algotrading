"""The rotation strategy's pure rules (`engine/rotation.py`) and its config block
(`strategy.config.Rotation`), on small hand-built inputs."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import numpy as np
import pytest

from app.hat.engine.rotation import (
    BUY,
    SELL,
    TOP_UP,
    TRIM,
    Order,
    is_rebalance,
    order_diff,
    ranked,
    scores,
    select,
    window_rows,
    zscores,
)
from app.hat.engine.strategy.config import ConfigError, Rotation, parse_config
from app.hat.engine.strategy.versions import seed_config

D = Decimal


def test_month_end_rebalances_on_the_last_session_of_a_month() -> None:
    assert is_rebalance(date(2024, 1, 31), date(2024, 2, 1), "month_end")
    assert is_rebalance(date(2024, 3, 28), date(2024, 4, 1), "month_end")  # 29th-31st closed
    assert not is_rebalance(date(2024, 1, 30), date(2024, 1, 31), "month_end")


def test_fortnight_adds_the_last_session_of_the_first_half() -> None:
    assert is_rebalance(date(2024, 6, 14), date(2024, 6, 17), "fortnight")  # 15th a Saturday
    assert is_rebalance(date(2024, 6, 28), date(2024, 7, 1), "fortnight")
    assert not is_rebalance(date(2024, 6, 13), date(2024, 6, 14), "fortnight")
    assert not is_rebalance(date(2024, 6, 14), date(2024, 6, 17), "month_end")


def test_weekly_rebalances_on_the_last_session_of_an_iso_week() -> None:
    assert is_rebalance(date(2024, 6, 14), date(2024, 6, 17), "weekly")  # Friday -> Monday
    assert is_rebalance(date(2024, 12, 27), date(2024, 12, 30), "weekly")
    assert not is_rebalance(date(2024, 6, 12), date(2024, 6, 13), "weekly")
    with pytest.raises(ValueError, match="unknown_rebalance"):
        is_rebalance(date(2024, 6, 12), date(2024, 6, 13), "daily")


def test_zscores_use_the_sample_deviation_and_skip_nan() -> None:
    found = zscores(np.array([1.0, 2.0, 3.0, np.nan]))
    assert found[:3] == pytest.approx([-1.0, 0.0, 1.0])
    assert math.isnan(found[3])
    assert np.isnan(zscores(np.array([5.0, np.nan]))).all()  # one value: no spread
    assert np.isnan(zscores(np.array([2.0, 2.0]))).all()


# windows far below the config's bounds, so a test can hold every close (built unvalidated)
SMALL = Rotation.model_construct(
    short_sessions=2, long_sessions=4, skip_sessions=1, vol_sessions=4, min_bars=3, top_n=2
)


def _closes(*columns: list[float]) -> np.ndarray:
    """Columns of closes, the decision session last, padded at the top to the window."""
    rows = window_rows(SMALL)
    padded = [[np.nan] * (rows - len(c)) + c for c in columns]
    return np.array(padded, dtype=float).T


def test_r6_r12_1_and_hi52_read_the_closes_back_from_the_decision_session() -> None:
    closes = _closes([100, 100, 110, 120, 132.0])
    r6 = scores(closes, SMALL.model_copy(update={"score": "r6"}))
    assert r6[0] == pytest.approx(132 / 110 - 1)
    r12_1 = scores(closes, SMALL.model_copy(update={"score": "r12_1"}))
    assert r12_1[0] == pytest.approx(120 / 100 - 1)
    hi52 = scores(_closes([100, 120, 110, 90, 108.0]), SMALL.model_copy(update={"score": "hi52"}))
    assert hi52[0] == pytest.approx(0.9 + 1e-5 * (90 / 100 - 1))


def test_a_close_carries_over_a_short_gap_and_too_few_bars_score_nan() -> None:
    gap = _closes([100, 100, 110, np.nan, 132.0])  # the short base carried from 110
    r6 = SMALL.model_copy(update={"score": "r6"})
    assert scores(gap, r6)[0] == pytest.approx(132 / 110 - 1)
    sparse = _closes([100, np.nan, np.nan, 120, 132.0])  # 2 bars of the last 4 < min_bars 3
    assert math.isnan(scores(sparse, r6)[0])


def test_riskadj_is_the_mean_of_two_cross_sectional_zscores() -> None:
    closes = _closes(
        [100, 101, 103, 102, 106.0], [100, 104, 103, 108, 107.0], [100, 99, 101, 100, 99.0]
    )
    found = scores(closes, SMALL)
    filled = closes[-5:]
    lr = np.diff(np.log(filled), axis=0)
    vol = lr.std(axis=0, ddof=1) * math.sqrt(252)
    r6 = filled[-1] / filled[-3] - 1
    r12 = filled[-1] / filled[0] - 1

    def z(x: np.ndarray) -> np.ndarray:
        return (x - x.mean()) / x.std(ddof=1)

    assert found == pytest.approx(0.5 * z(r6 / vol) + 0.5 * z(r12 / vol))
    assert sum(found) == pytest.approx(0.0)


def test_ranked_drops_nan_and_breaks_ties_by_name() -> None:
    assert ranked(["b", "a", "c"], np.array([1.0, 1.0, np.nan])) == [("a", 1.0), ("b", 1.0)]


def test_select_keeps_holdings_within_the_buffer_then_fills_best_first() -> None:
    ranking = ["a", "b", "c", "d", "e", "f"]
    # e (rank 5) is within a buffer of 5, f (rank 6) is not; b cannot be bought today
    found = select(ranking, {"e", "f"}, 3, 5, lambda n: n != "b")
    assert found == ["e", "a", "c"]
    # a holding not ranked at all (ineligible) leaves; kept names come first
    assert select(ranking, {"z", "x"}, 2, 4, lambda n: True, kept=["x"]) == ["x", "a"]


def test_select_caps_kept_holdings_at_top_n_best_first() -> None:
    assert select(["a", "b", "c"], {"c", "b", "a"}, 2, 3, lambda n: True) == ["a", "b"]


def test_order_diff_sells_then_trims_then_buys_and_skips_small_changes() -> None:
    prices = {"a": D(100), "b": D(50), "c": D(10), "d": D(200)}
    held = {"a": 120, "b": 199, "x": 7}  # a 12000, b 9950
    orders = order_diff(["a", "b", "c", "d"], held, prices, D(40000), 4, D(2), reweight=True)
    # each target 10000: a trims 20 (2000 > 200), b stays (50 < 200), c buys 1000, d buys 50
    assert orders == [
        Order("x", SELL, 7),
        Order("a", TRIM, 20),
        Order("c", BUY, 1000),
        Order("d", BUY, 50),
    ]


def test_order_diff_tops_up_only_when_reweighting() -> None:
    prices = {"a": D(100)}
    assert order_diff(["a"], {"a": 50}, prices, D(10000), 1, D(2), reweight=True) == [
        Order("a", TOP_UP, 50)
    ]
    assert order_diff(["a"], {"a": 50}, prices, D(10000), 1, D(2), reweight=False) == []
    # a target without a price gets no order; the share is capital / top_n, not / targets
    assert order_diff(["a", "b"], {}, prices, D(10000), 4, D(2), reweight=True) == [
        Order("a", BUY, 25)
    ]


def test_a_rotation_config_has_no_playbook_or_exit_and_old_configs_keep_their_form() -> None:
    config = seed_config("momentum-rotation")
    assert config.rotation is not None
    assert config.playbooks == []
    assert config.exits == []
    assert parse_config(config.to_json()).hash() == config.hash()
    assert "rotation" not in seed_config("stage2-contraction-breakout").to_json()
    raw = config.to_json()
    with pytest.raises(ConfigError) as refused:
        parse_config({**raw, "rotation": {**raw["rotation"], "buffer_rank": 10}})
    assert refused.value.codes == {"buffer_rank_below_top_n"}
    playbook = seed_config("stage2-contraction-breakout").to_json()["playbooks"]
    with pytest.raises(ConfigError) as mixed:
        parse_config({**raw, "playbooks": playbook})
    assert mixed.value.codes == {"rotation_with_playbooks_or_exits"}
