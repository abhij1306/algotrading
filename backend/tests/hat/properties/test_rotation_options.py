"""The rotation's optional rules at their seams (`engine.rotation`): the relative liquidity floor,
the riskadj skip, inverse-beta weights, the staggered rebalance, the refusal near a price band,
and the backtest's size-aware impact (`costs.impact_bps`); each key left out of the canonical
form at its default, so stored configs keep their hashes."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from app.hat.backtest.data import band_on
from app.hat.backtest.report import FillRow
from app.hat.backtest.run_audit import check_fills, fill_impacts
from app.hat.backtest.spec import IMPACT_RULE, BacktestError, RunConventions
from app.hat.costs import MAX_IMPACT_BPS, impact_bps
from app.hat.engine import rotation
from app.hat.engine.rotation_live import near_limit_price
from app.hat.engine.strategy.config import (
    InverseBeta,
    Liquidity,
    Rotation,
    Tranches,
)
from app.hat.engine.trigger import DailyInputs

D = Decimal
SMALL = {"short_sessions": 5, "long_sessions": 8, "skip_sessions": 2, "vol_sessions": 20,
         "min_bars": 5, "top_n": 2, "buffer_rank": 2}  # fmt: skip


def test_the_new_keys_are_left_out_at_their_defaults() -> None:
    dumped = Rotation().model_dump(mode="json")
    for key in ("liquidity", "riskadj_skip", "inverse_beta", "tranches", "refuse_near_limit"):
        assert key not in dumped
    set_ = Rotation(
        liquidity={"entry_pct": 60, "hold_pct": 80},
        riskadj_skip=True,
        inverse_beta={},
        tranches={"count": 4, "offset_sessions": 3},
        refuse_near_limit=True,
    ).model_dump(mode="json")
    assert set_["liquidity"] == {"entry_pct": 60.0, "hold_pct": 80.0, "days": 20}
    assert set_["inverse_beta"] == {"sessions": 252, "floor": 0.5}
    assert set_["riskadj_skip"] is True
    assert set_["refuse_near_limit"] is True


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"liquidity": {"entry_pct": 70, "hold_pct": 50}}, "hold_pct_below_entry_pct"),
        ({"riskadj_skip": True, "score": "r12_1"}, "riskadj_skip_needs_riskadj"),
        ({"riskadj_skip": True, "skip_sessions": 126}, "riskadj_skip_needs_riskadj"),
        ({"tranches": {"count": 4, "offset_sessions": 5}}, "tranches_beyond_period"),
        (
            {"tranches": {"count": 2, "offset_sessions": 3}, "rebalance": "weekly"},
            "tranches_beyond_period",
        ),
    ],
)
def test_the_new_keys_refuse_what_cannot_run(fields: dict[str, object], code: str) -> None:
    with pytest.raises(ValueError, match=code):
        Rotation.model_validate(fields)


def test_riskadj_skip_ends_both_returns_before_the_close_but_not_the_volatility() -> None:
    rows = rotation.window_rows(Rotation(**SMALL))
    first = np.linspace(100, 130, rows) * (1 + 0.01 * np.sin(np.arange(rows)))
    closes = first.reshape(-1, 1)
    short0, long0 = rotation.riskadj_parts(closes, Rotation(**SMALL))
    short1, long1 = rotation.riskadj_parts(closes, Rotation(**SMALL, riskadj_skip=True))
    # with the skip both returns end `skip_sessions` before the close (the lookbacks still
    # counted from it), over the same volatility, which ends on the close either way
    assert short1[0] / short0[0] == pytest.approx(
        (first[-3] / first[-6] - 1) / (first[-1] / first[-6] - 1)
    )
    assert long1[0] / long0[0] == pytest.approx(
        (first[-3] / first[-9] - 1) / (first[-1] / first[-9] - 1)
    )


def test_median_traded_values_need_a_bar_on_the_night_and_every_day() -> None:
    days = [date(2024, 1, 1) + timedelta(n) for n in range(6)]
    rows = []
    for n, day in enumerate(days):
        rows.append(("a", day, 10.0, 100.0, float(n + 1)))  # traded value 1..6
        rows.append(("b", day, 10.0, 100.0 * (n + 1), None))  # close x volume 1000..6000
        if n < 5:
            rows.append(("c", day, 10.0, 100.0, 5.0))  # no bar on the night
    frame = pl.DataFrame(
        rows,
        schema=["instrument_id", "date", "close", "volume", "traded_value"],
        orient="row",
    )
    found = rotation.median_traded_values(frame, days[-1], 4)
    assert found == {"a": 4.5, "b": 4500.0}
    assert rotation.median_traded_values(frame, days[-1], 7) == {}  # too few bars


def test_liquidity_tiers_round_up_and_the_hold_tier_holds_the_entry_tier() -> None:
    values = {f"n{k}": float(k) for k in range(10)}
    entry, hold = rotation.liquidity_tiers(values, Liquidity(entry_pct=25, hold_pct=55))
    assert entry == {"n9", "n8", "n7"}  # ceil(2.5)
    assert hold == entry | {"n6", "n5", "n4"}  # ceil(5.5)
    tie = rotation.liquidity_tiers({"b": 1.0, "a": 1.0}, Liquidity(entry_pct=50, hold_pct=50))
    assert tie == ({"a"}, {"a"})


def test_a_hold_only_name_is_kept_while_held_and_never_bought() -> None:
    rules = Rotation(**{**SMALL, "buffer_rank": 3})
    ranks = (("x", 3.0), ("y", 2.0), ("z", 1.0))
    held_target, _ = rotation.targets(
        ranks, {"y"}, rules, gate_on=True, can_buy=lambda n: True, hold_only={"x", "y"}
    )
    assert held_target == ("y", "z")  # y kept in the buffer; x passed over for z
    fresh, _ = rotation.targets(
        ranks, set(), rules, gate_on=True, can_buy=lambda n: True, hold_only={"x", "y"}
    )
    assert fresh == ("z",)


def test_betas_floor_and_fall_back_to_the_market() -> None:
    rng = np.random.default_rng(7)
    market = np.cumprod(1 + rng.normal(0, 0.01, 61)) * 100
    moves = market[1:] / market[:-1] - 1
    double = np.concatenate([[50.0], 50 * np.cumprod(1 + 2 * moves)])
    flat = np.concatenate([[50.0], 50 * np.cumprod(1 + 0.1 * moves)])
    sparse = np.full(61, np.nan)
    sparse[:10] = 10.0
    closes = np.column_stack([double, flat, sparse])
    found = rotation.betas(closes, market, InverseBeta(sessions=60, floor=0.5))
    assert found[0] == pytest.approx(2.0)
    assert found[1] == 0.5  # 0.1 floored
    assert found[2] == 1.0  # too few returns: the market's
    named = rotation.betas_by_name(["a", "b", "c"], found)
    assert named == {"a": D("2.0000"), "b": D("0.5000"), "c": D("1.0000")}


def test_inverse_beta_weights_keep_equal_weights_share_and_drive_the_order_diff() -> None:
    weights = rotation.target_weights(["a", "b"], {"a": D(2), "b": D("0.5")}, 4)
    assert sum(weights.values()) == D("0.5")  # two of four slots, as equal weights
    assert weights["b"] == 4 * weights["a"]
    full = rotation.target_weights(["a", "b", "c"], {"a": D(1)}, 3)
    assert full == {name: D(1) / 3 for name in "abc"}  # no beta: 1
    prices = {"a": D(10), "b": D(10)}
    orders = rotation.order_diff(["a", "b"], {}, prices, D(1000), 4, D(0), reweight=True,
                                 weights=weights)  # fmt: skip
    assert [(o.instrument_id, o.qty) for o in orders] == [("a", 10), ("b", 40)]
    equal = rotation.order_diff(["a", "b"], {}, prices, D(1000), 4, D(0), reweight=True)
    assert [(o.instrument_id, o.qty) for o in equal] == [("a", 25), ("b", 25)]


def test_tranches_decide_at_their_offsets_after_each_period_close() -> None:
    days = [date(2024, 1, 29), date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1),
            date(2024, 2, 2), date(2024, 2, 5), date(2024, 2, 6), date(2024, 2, 7)]  # fmt: skip
    since = rotation.sessions_since_close(days, "month_end")
    assert since == [None, None, 0, 1, 2, 3, 4, 5]
    rule = Tranches(count=3, offset_sessions=2)
    assert [rotation.tranche_due(s, rule) for s in since] == [
        None, None, 0, None, 1, None, 2, None,
    ]  # fmt: skip


def _daily(band: str | None, *, known: bool = True, prev: str | None = "100") -> DailyInputs:
    return DailyInputs(
        prev_close=None if prev is None else D(prev),
        tick=D("0.05"),
        band_known=known,
        band_pct=None if band is None else D(band),
    )


def test_near_limit_reads_only_the_stored_band_and_the_previous_close() -> None:
    # the rule's inputs are the previous close, the tick and the band in force on the trade
    # date: nothing of the session itself, so the backtest refuses only what live could
    five = _daily("5")
    assert rotation.near_limit("buy", D("104.95"), five)  # one tick under 105.00
    assert rotation.near_limit("buy", D("105.00"), five)
    assert not rotation.near_limit("buy", D("104.90"), five)
    assert rotation.near_limit("sell", D("95.05"), five)
    assert not rotation.near_limit("sell", D("95.10"), five)
    assert not rotation.near_limit("sell", D("104.95"), five)  # a sale is held only at the lower
    # an F&O name (a stored row without a band) and an unknown previous close: never refused
    assert not rotation.near_limit("buy", D("200"), _daily(None))
    assert not rotation.near_limit("buy", D("105"), _daily("5", prev=None))


def test_near_limit_without_a_stored_band_checks_every_nse_band() -> None:
    unknown = _daily(None, known=False)
    for upper in ("102.00", "105.00", "110.00", "120.00"):
        assert rotation.near_limit("buy", D(upper), unknown)
        assert rotation.near_limit("buy", D(upper) - D("0.05"), unknown)
    assert not rotation.near_limit("buy", D("103.50"), unknown)  # between two candidates
    assert rotation.near_limit("sell", D("98.00"), unknown)
    assert not rotation.near_limit("sell", D("97.00"), unknown)


def test_the_stored_band_is_read_point_in_time() -> None:
    rows = [(date(2024, 1, 1), D(20)), (date(2024, 6, 1), D(5)), (date(2025, 1, 1), None)]
    assert band_on(rows, date(2023, 12, 31)) == (False, None)
    assert band_on(rows, date(2024, 5, 31)) == (True, D(20))
    assert band_on(rows, date(2024, 6, 1)) == (True, D(5))
    assert band_on(rows, date(2025, 3, 1)) == (True, None)


def test_live_refuses_a_price_near_the_band_on_the_order_side_only() -> None:
    def price(name: str) -> tuple[Decimal | None, str | None]:
        return {"up": (D("104.95"), None), "down": (D("95.05"), None)}.get(name, (None, "x"))

    def limits(name: str) -> DailyInputs:
        return _daily("5")

    buy = near_limit_price(price, limits, "buy")
    sell = near_limit_price(price, limits, "sell")
    assert buy("up") == (None, rotation.NEAR_UPPER_LIMIT)
    assert sell("up") == (D("104.95"), None)
    assert sell("down") == (None, rotation.NEAR_LOWER_LIMIT)
    assert buy("down") == (D("95.05"), None)
    assert buy("none") == (None, "x")
    assert near_limit_price(price, None, "buy") is price


def test_a_hold_only_holding_is_never_topped_up_or_bought_only_trimmed() -> None:
    prices = {"h": D(10), "n": D(10)}
    # h is held below its target, n not held: neither is bought under `no_buy`
    found = rotation.order_diff(["h", "n"], {"h": 10}, prices, D(1000), 2, D(0), reweight=True,
                                no_buy={"h", "n"})  # fmt: skip
    assert found == []
    plain = rotation.order_diff(["h", "n"], {"h": 10}, prices, D(1000), 2, D(0), reweight=True)
    assert [(o.instrument_id, o.action) for o in plain] == [("h", rotation.TOP_UP), ("n", "buy")]
    # above its target it is still trimmed
    trimmed = rotation.order_diff(["h"], {"h": 80}, prices, D(1000), 2, D(0), reweight=True,
                                  no_buy={"h"})  # fmt: skip
    assert [(o.instrument_id, o.action, o.qty) for o in trimmed] == [("h", rotation.TRIM, 30)]
    # the plan carries the names through `orders`
    chosen = rotation.Plan(date(2024, 1, 31), (("h", 1.0),), ("h",), hold_only=frozenset({"h"}))
    rules = Rotation(top_n=2, buffer_rank=2)
    assert rotation.orders(chosen, {"h": 10}, prices, D(1000), rules) == []


def test_the_selection_depth_reaches_past_hold_only_names() -> None:
    rules = Rotation(top_n=2, buffer_rank=2)
    ranking = ["a", "b", "c", "d", "e", "f", "g"]
    assert rotation.selection_depth(ranking, rules) == 4
    assert rotation.selection_depth(ranking, rules, hold_only={"a", "c"}) == 6


def test_only_a_rotation_fill_with_impact_is_widened_by_its_own_impact() -> None:
    day = date(2024, 1, 2)
    fills = [
        FillRow(day, "r", "entry", "buy", 10, D("101.50"), D(1), D(200)),
        FillRow(day, "s", "entry", "buy", 10, D("101.50"), D(1)),  # a setup fill: no impact
    ]
    impacts = fill_impacts(fills)
    assert impacts == {("r", day, "buy", D("101.50")): D(200)}
    bars = {("r", day): (D(99), D(100)), ("s", day): (D(99), D(100))}
    trades = {
        "1": [{"instrument_id": "r", "entry_date": "2024-01-02", "entry_price": "101.50",
               "exit_date": "2024-01-02", "exit_price": "99.5"}],
        "2": [{"instrument_id": "s", "entry_date": "2024-01-02", "entry_price": "101.50",
               "exit_date": "2024-01-02", "exit_price": "99.5"}],
    }  # fmt: skip
    found = check_fills(trades, bars, D(20), impacts)
    # 101.50 is 1.5% over the high: inside 20 + 200 bps for r, outside 20 bps for s
    outside = [item["trade_id"] for f in found["findings"] for item in f["items"]]
    assert outside == ["2"]
    assert found["summary"]["impact_fills_widened"] == 1


def test_impact_grows_with_the_square_root_of_the_order_share_and_is_capped() -> None:
    assert impact_bps(D(100), D(1_00_000), D(1_00_00_000)) == D("10.00")  # 1% of the day
    assert impact_bps(D(100), D(4_00_000), D(1_00_00_000)) == D("20.00")
    assert impact_bps(D(100), D(10**12), D(1)) == MAX_IMPACT_BPS
    assert impact_bps(D(100), D(1000), None) == 0
    assert impact_bps(D(100), D(1000), D(0)) == 0


def test_the_impact_convention_is_recorded_only_when_set() -> None:
    class _Schedule:
        def to_json(self, *_: object) -> dict[str, str]:
            return {}

    class _Fills:
        slippage_bps = D(20)
        schedule = _Schedule()

    window = (date(2020, 1, 1), date(2020, 12, 31))
    plain = RunConventions(entry_bars="daily").record(_Fills(), window, D(1), {})  # type: ignore[arg-type]
    assert "impact" not in plain
    found = RunConventions(entry_bars="daily", impact_bps=D(50)).record(
        _Fills(),  # type: ignore[arg-type]
        window,
        D(1),
        {},
    )
    assert found["impact"] == {"rule": IMPACT_RULE, "coef_bps": "50", "max_bps": "1000"}
    with pytest.raises(BacktestError, match="backtest_bad_impact"):
        RunConventions(impact_bps=D(0))
