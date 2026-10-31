"""The rotation's two optional drawdown rules at their seams: the industry cap in `select` and
the mid-period scale-down (`scale_down_fires`, `scale_down_trims`, `capital_share`,
`rotation_live.scaled_in_period`), and their config keys left out of the canonical form when
unset."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.hat.engine.rotation import (
    TRIM,
    UNKNOWN_INDUSTRY,
    Order,
    capital_share,
    scale_down_fires,
    scale_down_trims,
    select,
    selection_depth,
    targets,
)
from app.hat.engine.rotation_live import scaled_in_period
from app.hat.engine.snapshot import chain_members
from app.hat.engine.strategy.config import ConfigError, Rotation, ScaleDown, parse_config
from app.hat.engine.strategy.versions import seed_config

D = Decimal
SECTOR = {"a": "bank", "b": "bank", "c": "bank", "d": "it", "e": UNKNOWN_INDUSTRY, "f": "it"}
SMA3 = {"rule": "close_above_sma_and_rising", "sma": 3, "rising_days": 0}
BREADTH40 = {"rule": "breadth_above", "min_pct": 40.0}


def _scale(when: str = "any_off", *checks: dict[str, object]) -> ScaleDown:
    return ScaleDown.model_validate(
        {"checks": list(checks) or [SMA3], "when": when, "keep_fraction": 0.5}
    )


def test_the_industry_cap_passes_over_a_full_industry_for_the_next_name() -> None:
    ranking = ["a", "b", "c", "d", "e", "f"]
    # two banks at most: c is passed over for d
    found = select(ranking, set(), 4, 6, lambda n: True, max_per_industry=2,
                   industry=SECTOR.__getitem__)  # fmt: skip
    assert found == ["a", "b", "d", "e"]
    # no cap without a classification or a count: the selection is the plain one
    assert select(ranking, set(), 4, 6, lambda n: True) == ["a", "b", "c", "d"]


def test_a_capped_holding_is_sold_and_a_kept_name_counts_but_always_stays() -> None:
    ranking = ["a", "b", "c", "d"]
    # held b and c rank within the buffer, but a (not held) cannot take a bank's place over them:
    # holdings come first, so b stays, c is over the cap of 1 and leaves, a too
    found = select(ranking, {"b", "c"}, 3, 4, lambda n: True, max_per_industry=1,
                   industry=SECTOR.__getitem__)  # fmt: skip
    assert found == ["b", "d"]
    # a kept (factor-pending) bank stays and fills the industry
    kept = select(ranking, {"c"}, 3, 4, lambda n: True, kept=["c"], max_per_industry=1,
                  industry=SECTOR.__getitem__)  # fmt: skip
    assert kept == ["c", "d"]


def test_an_unknown_industry_is_never_capped() -> None:
    industry = {"x": UNKNOWN_INDUSTRY, "y": UNKNOWN_INDUSTRY, "z": UNKNOWN_INDUSTRY}
    found = select(["x", "y", "z"], set(), 3, 3, lambda n: True, max_per_industry=1,
                   industry=industry.__getitem__)  # fmt: skip
    assert found == ["x", "y", "z"]


def test_a_capped_rule_needs_the_classification() -> None:
    rules = Rotation(top_n=2, buffer_rank=2, max_per_industry=1, min_bars=5, long_sessions=5,
                     short_sessions=5, skip_sessions=1, vol_sessions=20)  # fmt: skip
    ranks = (("a", 2.0), ("b", 1.0), ("d", 0.5))
    with pytest.raises(ValueError, match="rotation_industry_unknown"):
        targets(ranks, set(), rules, gate_on=True, can_buy=lambda n: True)
    found = targets(ranks, set(), rules, gate_on=True, can_buy=lambda n: True,
                    industry=SECTOR.__getitem__)  # fmt: skip
    assert found == (("a", "d"), False)


def test_the_scale_down_fires_on_any_or_every_check_off() -> None:
    falling = [D(100), D(100), D(100), D(90)]  # the close under its 3-session SMA
    rising = [D(100), D(100), D(100), D(110)]
    wide = [D(50)] * 3  # breadth 50%: the breadth check is on
    narrow = [D(30)] * 3
    assert scale_down_fires(_scale(), falling, wide)
    assert not scale_down_fires(_scale(), rising, wide)
    both = ("any_off", SMA3, BREADTH40)
    assert scale_down_fires(_scale(*both), rising, narrow)
    assert not scale_down_fires(_scale(*both), rising, wide)
    every = ("all_off", SMA3, BREADTH40)
    assert not scale_down_fires(_scale(*every), falling, wide)
    assert scale_down_fires(_scale(*every), falling, narrow)
    # a series too short for the check reads it off (fail closed): the scale-down fires
    assert scale_down_fires(_scale(), [D(100)], wide)


def test_the_trims_keep_the_fraction_of_each_holding_rounded_up() -> None:
    found = scale_down_trims({"b": 7, "a": 10, "c": 1, "z": 0}, D("0.5"))
    # a sells 5 of 10; b sells 3 of 7 (keeps 4); c would sell 0 of 1: no order
    assert found == [Order("a", TRIM, 5), Order("b", TRIM, 3)]
    assert scale_down_trims({"a": 10}, D("0.7")) == [Order("a", TRIM, 3)]


def test_a_rebalance_sizes_on_the_fraction_only_when_set_and_fired() -> None:
    plain = Rotation()
    scaled = Rotation(scale_down=_scale())
    assert capital_share(plain, True) == 1
    assert capital_share(scaled, False) == 1
    assert capital_share(scaled, True) == D("0.5")


def test_the_book_stays_scaled_down_until_a_rebalance_that_does_not_fire() -> None:
    jan, feb_3, feb, mar_4 = (
        date(2024, 1, 31),
        date(2024, 2, 5),
        date(2024, 2, 29),
        date(2024, 3, 4),
    )
    assert not scaled_in_period(None, None)
    assert not scaled_in_period((jan, False), None)
    assert scaled_in_period((jan, True), None)  # the rebalance itself sized down
    assert scaled_in_period((jan, False), feb_3)  # trimmed mid-period
    assert not scaled_in_period((feb, False), feb_3)  # the next rebalance restored it
    assert scaled_in_period((feb, False), mar_4)
    assert scaled_in_period(None, feb_3)


def test_the_keys_are_left_out_unset_and_the_scale_down_needs_reweight() -> None:
    config = seed_config("momentum-rotation")
    raw = config.to_json()
    assert "max_per_industry" not in raw["rotation"]
    assert "scale_down" not in raw["rotation"]
    on = parse_config(
        {
            **raw,
            "rotation": {
                **raw["rotation"],
                "max_per_industry": 4,
                "scale_down": {"checks": [SMA3], "keep_fraction": 0.5},
            },
        }
    )
    assert on.rotation is not None
    assert on.to_json()["rotation"]["max_per_industry"] == 4
    assert on.to_json()["rotation"]["scale_down"]["when"] == "any_off"
    assert on.hash() != config.hash()
    assert parse_config(on.to_json()).hash() == on.hash()
    with pytest.raises(ConfigError) as refused:
        parse_config(
            {
                **raw,
                "rotation": {
                    **raw["rotation"],
                    "reweight": False,
                    "scale_down": {"checks": [SMA3], "keep_fraction": 0.5},
                },
            }
        )
    assert refused.value.codes == {"scale_down_needs_reweight"}
    with pytest.raises(ConfigError):
        parse_config(
            {**raw, "rotation": {**raw["rotation"], "scale_down": {"checks": [SMA3],
                                                                   "keep_fraction": 1.0}}}
        )  # fmt: skip


def test_bucketed_unknowns_count_as_one_industry() -> None:
    industry = {"x": UNKNOWN_INDUSTRY, "y": UNKNOWN_INDUSTRY, "d": "it"}
    found = select(["x", "y", "d"], set(), 3, 3, lambda n: True, max_per_industry=1,
                   industry=industry.__getitem__, bucket_unknown=True)  # fmt: skip
    assert found == ["x", "d"]
    rules = Rotation(max_per_industry=1, unknown_industry="bucket", top_n=3, buffer_rank=3,
                     min_bars=5, long_sessions=5, short_sessions=5, skip_sessions=1)  # fmt: skip
    ranks = (("x", 3.0), ("y", 2.0), ("d", 1.0))
    picked = targets(ranks, set(), rules, gate_on=True, can_buy=lambda n: True,
                     industry=industry.__getitem__)  # fmt: skip
    assert picked == (("x", "d"), False)


def test_the_selection_depth_reaches_past_the_names_the_cap_skips() -> None:
    plain = Rotation(top_n=2, buffer_rank=2, min_bars=5, long_sessions=5, short_sessions=5,
                     skip_sessions=1)  # fmt: skip
    capped = plain.model_copy(update={"max_per_industry": 1})
    bucket = capped.model_copy(update={"unknown_industry": "bucket"})
    ranking = ["a", "b", "c", "d", "e", "f"]  # banks a b c, it d f, unknown e
    assert selection_depth(ranking, plain, SECTOR.__getitem__) == 4
    assert selection_depth(ranking, capped) == 4  # no classification: the uncapped rule
    # admitted: a (bank), d (it), e (unknown, free), then f is a second it: 4 needs the whole list
    assert selection_depth(ranking, capped, SECTOR.__getitem__) == 6
    assert selection_depth(["a", "d", "e", "x"], capped, {**SECTOR, "x": "fmcg"}.__getitem__) == 4
    # bucketed, the unknown counts once like an industry
    found = selection_depth(["a", "e", "d", "y", "z"], bucket,
                            {**SECTOR, "y": UNKNOWN_INDUSTRY, "z": "fmcg"}.__getitem__)  # fmt: skip
    assert found == 5


def test_a_chain_is_every_instrument_its_links_join_either_way() -> None:
    links = [("old", "mid"), ("mid", "new"), ("p", "q")]
    assert chain_members(links, "new") == {"old", "mid", "new"}
    assert chain_members(links, "old") == {"old", "mid", "new"}
    assert chain_members(links, "q") == {"p", "q"}
    assert chain_members(links, "alone") == {"alone"}


def test_unknown_industry_is_left_out_at_its_default_and_needs_a_cap() -> None:
    raw = seed_config("momentum-rotation").to_json()
    assert "unknown_industry" not in raw["rotation"]
    on = parse_config(
        {
            **raw,
            "rotation": {**raw["rotation"], "max_per_industry": 3, "unknown_industry": "bucket"},
        }
    )
    assert on.to_json()["rotation"]["unknown_industry"] == "bucket"
    with pytest.raises(ConfigError) as refused:
        parse_config({**raw, "rotation": {**raw["rotation"], "unknown_industry": "bucket"}})
    assert refused.value.codes == {"unknown_industry_without_cap"}
