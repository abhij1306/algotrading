"""Generated strategy configs for the config properties: valid raw configs in the seeds' YAML
shape (stage and order implied by position, or explicit), and ways to break them."""

from __future__ import annotations

from typing import Any

from hypothesis import strategies as st

from app.hat.governance import Mode

NUM = st.floats(0.5, 90, allow_nan=False, allow_infinity=False)
POS = st.sampled_from([0.5, 1.0, 2.5, 4.0, 10.0, 30.0, 50.0])
DAYS = st.integers(2, 60)
SMAS = st.sampled_from([10, 20, 50, 150, 200])
SIDE = st.sampled_from(["close", 10, 20, 50, 150, 200])


@st.composite
def _pair(draw: st.DrawFn, low: int, high: int) -> list[int]:
    a, b = sorted((draw(st.integers(low, high)), draw(st.integers(low, high))))
    return [a, b]


@st.composite
def _sides(draw: st.DrawFn) -> dict[str, Any]:
    a, b = draw(SIDE), draw(SIDE)
    if a == b:
        b = "close" if a != "close" else 50
    return {"a": a, "b": b}


@st.composite
def _rs(draw: st.DrawFn) -> dict[str, Any]:
    low = draw(st.integers(0, 90))
    return {
        "min_percentile": draw(st.integers(0, 100)),
        "score_from": low,
        "score_full": draw(st.integers(low + 1, 100)),
    }


PARAMS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "series_in": st.fixed_dictionaries(
        {
            "series": st.lists(
                st.sampled_from(["EQ", "BE", "SM"]), min_size=1, max_size=3, unique=True
            )
        }
    ),
    "min_turnover": st.fixed_dictionaries({"days": st.integers(1, 60), "inr_cr": POS}),
    "min_price": st.fixed_dictionaries({"inr": st.sampled_from([0.0, 20.0, 50.0, 100.5])}),
    "price_band_allowed": st.fixed_dictionaries(
        {
            "allowed": st.lists(
                st.sampled_from([2, 5, 10, 20, "none"]), min_size=1, max_size=5, unique=True
            )
        }
    ),
    "adr_min": st.fixed_dictionaries({"days": st.integers(1, 60), "pct": POS}),
    "sma_above": _sides(),
    "sma_rising": st.fixed_dictionaries({"sma": SMAS, "lookback_days": st.integers(1, 60)}),
    "pct_from_52w_low": st.fixed_dictionaries({"min_pct": NUM}),
    "pct_from_52w_high": st.fixed_dictionaries({"max_pct_below": NUM}),
    "rs_rank": _rs(),
    "momentum_rank": st.fixed_dictionaries(
        {
            "windows": st.lists(
                st.sampled_from([21, 63, 126, 252]), min_size=1, max_size=4, unique=True
            ),
            "top_pct": POS,
            "match": st.sampled_from(["any", "all"]),
        }
    ),
    "vcp": st.fixed_dictionaries(
        {
            "contractions": _pair(2, 6),
            "depth_ratio_max": st.sampled_from([0.5, 0.75, 1.0]),
            "final_depth_max_pct": POS,
            "lookback_days": st.integers(15, 252),
            "dryup_vs_avg50": st.sampled_from([0.3, 0.7, 2.0]),
        },
        optional={
            "max_high_spread_pct": st.none() | POS,
            "min_first_depth_pct": st.none() | POS,
            "min_contraction_bars": st.none() | st.integers(2, 60),
            "pivot_source": st.sampled_from(["vcp", "base_high"]),
        },
    ),
    "consolidation": st.fixed_dictionaries(
        {"days": _pair(2, 60), "max_range_pct": POS, "higher_lows": st.booleans()}
    ),
    "ma_surf": st.fixed_dictionaries(
        {
            "mas": st.lists(SMAS, min_size=1, max_size=3, unique=True),
            "max_distance_pct": POS,
        }
    ),
    "prior_move": st.fixed_dictionaries({"min_pct": POS, "within_days": _pair(2, 252)}),
    "earnings_growth": st.fixed_dictionaries(
        {"eps_yoy": NUM, "sales_yoy": NUM, "accelerating": st.booleans()}
    ),
    "gap_up": st.fixed_dictionaries({"min_pct": POS}),
    "volume_pace": st.fixed_dictionaries({"vs_avg50": POS}),
    "early_volume": st.fixed_dictionaries({"vs_adv20": POS}),
    "catalyst_attached": st.fixed_dictionaries(
        {
            "within_hours": st.integers(1, 168),
            "required_in": st.lists(st.sampled_from(tuple(Mode)), max_size=3, unique=True),
        }
    ),
    "price_breaks_level": st.fixed_dictionaries(
        {
            "level": st.sampled_from(
                ["pivot", "opening_range_high", "max(consolidation_high, opening_range_high)"]
            ),
            "bar": st.just("5m"),
        }
    ),
    "close_vs_pivot": st.fixed_dictionaries({"max_above_pct": st.sampled_from([0.0, 1.0, 2.5])}),
    "neglect": st.fixed_dictionaries(
        {"max_return_63d_pct": NUM}, optional={"max_range_126d_pct": st.none() | POS}
    ),
    "adtr_filter": st.fixed_dictionaries({"max_multiple": POS}),
    "max_chase": st.fixed_dictionaries({"pct": POS}),
}
UNIVERSE_TYPES = ["series_in", "min_turnover", "min_price", "price_band_allowed", "adr_min"]
SETUP_TYPES = [
    "sma_above", "sma_rising", "pct_from_52w_low", "pct_from_52w_high", "rs_rank",
    "momentum_rank", "vcp", "consolidation", "ma_surf", "prior_move", "earnings_growth",
    "neglect",
]  # fmt: skip
TRIGGER_TYPES = [
    "gap_up", "volume_pace", "early_volume", "catalyst_attached", "price_breaks_level", "neglect",
]  # fmt: skip
FILTER_TYPES = ["adtr_filter", "max_chase"]


@st.composite
def instance_list(
    draw: st.DrawFn, types: list[str], stage: str, *, scored: bool, max_size: int = 4
) -> list[dict[str, Any]]:
    """Instances of `types` for the section `stage`; either all in the short form (stage and
    order implied) or all with explicit stage and distinct orders in a shuffled list."""
    count = draw(st.integers(0, max_size))
    explicit = draw(st.booleans())
    orders = draw(st.permutations(range(count + 3)).map(lambda p: list(p)[:count]))
    out = []
    for index in range(count):
        code = draw(st.sampled_from(types))
        item: dict[str, Any] = {"type": code, "params": draw(PARAMS[code])}
        if scored and draw(st.booleans()):
            item.update(mode="score", weight=draw(st.sampled_from([0.5, 1, 2, 3])))
        if draw(st.booleans()):
            item["enabled"] = draw(st.booleans())
        if explicit:
            item["stage"] = stage
            item["order"] = orders[index]
        out.append(item)
    return out


STOPS = st.one_of(
    st.fixed_dictionaries(
        {"rule": st.just("below_final_contraction_low"), "buffer_pct": POS, "max_pct": POS},
        optional={"min_pct": st.sampled_from([0.0, 0.25])},
    ),
    st.fixed_dictionaries({"rule": st.just("low_of_day"), "buffer_ticks": st.integers(0, 5)}),
    st.fixed_dictionaries(
        {"rule": st.just("pct_below_entry"), "pct": st.sampled_from([1.0, 5.5, 20.0])}
    ),
    st.fixed_dictionaries(
        {"rule": st.just("atr_multiple"), "days": st.integers(1, 30), "multiple": POS}
    ),
    st.fixed_dictionaries({"rule": st.just("swing_low"), "lookback_days": st.integers(1, 60)}),
)
FRACTION = st.sampled_from([0.25, 0.33, 0.5, 1.0])
EXITS = st.one_of(  # beside the one enabled hard_stop every config has
    st.fixed_dictionaries({"rule": st.just("move_stop_to_breakeven"), "at_r": POS}),
    st.just({"rule": "move_stop_to_breakeven", "after": "partial"}),
    st.fixed_dictionaries(
        {"rule": st.just("partial_at_r"), "at_r": POS, "sell_fraction": FRACTION}
    ),
    st.fixed_dictionaries(
        {
            "rule": st.just("partial_after_days"),
            "days": st.integers(1, 30),
            "sell_fraction": FRACTION,
            "only_if_profit": st.booleans(),
        }
    ),
    st.fixed_dictionaries(
        {
            "rule": st.just("close_below_sma"),
            "sma": SMAS,
            "sell_fraction": FRACTION,
            "when": st.sampled_from(["15:25", "09:30", "00:00"]),
            "enabled": st.booleans(),
        }
    ),
    st.fixed_dictionaries({"rule": st.just("time_stop"), "days": st.integers(1, 30), "min_r": POS}),
)
REGIMES = st.one_of(
    st.fixed_dictionaries(
        {
            "index": st.sampled_from(["NIFTY 500", "NIFTY SMALLCAP 250"]),
            "rule": st.just("close_above_sma_and_rising"),
            "params": st.fixed_dictionaries({"sma": SMAS, "rising_days": st.integers(1, 30)}),
        }
    ),
    st.fixed_dictionaries(
        {
            "index": st.sampled_from(["NIFTY 500", "NIFTY SMALLCAP 250"]),
            "rule": st.just("sma_fast_above_slow_both_rising"),
            "params": st.fixed_dictionaries(
                {"fast": st.just(10), "slow": st.just(20)},
                optional={"rising_days": st.integers(1, 30)},
            ),
        }
    ),
)


@st.composite
def playbook_body(draw: st.DrawFn) -> dict[str, Any]:
    setup: dict[str, Any] = {
        "criteria": draw(instance_list(SETUP_TYPES, "setup", scored=True, max_size=5))
    }
    if draw(st.booleans()):
        setup["threshold"] = draw(st.sampled_from([0.0, 0.3, 0.6, 1.0]))
    return {
        "setup": setup,
        "trigger": draw(instance_list(TRIGGER_TYPES, "trigger", scored=False, max_size=3)),
        "entry_filters": draw(
            instance_list(FILTER_TYPES, "entry_filter", scored=False, max_size=2)
        ),
        "stop": draw(STOPS),
    }


@st.composite
def raw_configs(draw: st.DrawFn) -> dict[str, Any]:
    """A valid config in the seeds' YAML shape."""
    raw: dict[str, Any] = {
        "bot": draw(st.sampled_from(["alpha", "stage2-x", "b_1"])),
        "universe": draw(instance_list(UNIVERSE_TYPES, "universe", scored=False)),
        "regime": draw(REGIMES),
        "exits": [{"rule": "hard_stop"}, *draw(st.lists(EXITS, max_size=3))],
    }
    if draw(st.booleans()):
        raw["version"] = draw(st.integers(1, 9))
    if draw(st.booleans()):
        raw["market"] = "NSE"
    if draw(st.booleans()):
        raw.update(draw(playbook_body()))
    else:
        names = draw(
            st.lists(
                st.sampled_from(["breakout", "ep", "pb3"]), min_size=1, max_size=3, unique=True
            )
        )
        raw["playbooks"] = [{"name": n, **draw(playbook_body())} for n in names]
    return raw


# --- breaking a config ------------------------------------------------------------------------
