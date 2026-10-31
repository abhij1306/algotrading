"""The drawdown experiments' engine options at their seams: industry-relative momentum and the
wider book in weak markets (`engine.rotation`), the rule universe `TOP_TRADED`
(`backtest.traded_universe`), and the run outputs' skip rows and engine commit. Each rotation key
is set through a run override and `parse_config`, and left out of the canonical form at its
default, so stored configs keep their hashes."""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pytest

from app.hat.backtest import traded_universe as tu
from app.hat.backtest.loop import _Row, skip_row
from app.hat.backtest.overrides import Overrides, parse_set
from app.hat.backtest.runs import _universe_label, commit_of, top_traded_version
from app.hat.engine import rotation
from app.hat.engine.strategy.config import ConfigError, Rotation, parse_config
from app.hat.engine.strategy.versions import seed_config

WIDE = '{"top_n": 30, "checks": [{"rule": "breadth_above", "min_pct": 40}]}'


def test_the_new_keys_are_left_out_at_their_defaults() -> None:
    dumped = Rotation().model_dump(mode="json")
    assert "industry_relative" not in dumped
    assert "wide_book" not in dumped
    found = Rotation(
        industry_relative=True,
        wide_book={"top_n": 30, "checks": [{"rule": "breadth_above", "min_pct": 40}]},
    ).model_dump(mode="json")
    assert found["industry_relative"] is True
    assert found["wide_book"]["top_n"] == 30
    assert found["wide_book"]["when"] == "any_off"


@pytest.mark.parametrize("text", ["rotation.industry_relative=true", f"rotation.wide_book={WIDE}"])
def test_each_new_key_is_set_through_a_run_override(text: str) -> None:
    # the path a backtest takes: `--set` on the stored config, parsed afresh by `parse_config`
    # (its risk-key scan first), stored and parsed again under the same hash
    base = seed_config("momentum-rotation")
    found = Overrides(sets=(parse_set(text),)).apply_config(base)
    key = parse_set(text)[0].split(".")[1]
    assert key in found.to_json()["rotation"]
    assert found.hash() != base.hash()
    assert parse_config(found.to_json()).hash() == found.hash()
    assert parse_config(base.to_json()).hash() == base.hash()


@pytest.mark.parametrize(
    ("wide", "code"),
    [
        ({"top_n": 20, "checks": [{"rule": "breadth_above"}]}, "wide_book_not_wider"),
        ({"top_n": 200, "checks": [{"rule": "breadth_above"}]}, "wide_book_buffer_too_deep"),
    ],
)
def test_a_wide_book_that_is_not_wider_or_too_deep_is_refused(
    wide: dict[str, object], code: str
) -> None:
    with pytest.raises(ValueError, match=code):
        Rotation.model_validate({"top_n": 20, "buffer_rank": 120, "wide_book": wide})
    raw = seed_config("momentum-rotation").to_json()
    raw["rotation"] = {**raw["rotation"], "top_n": 20, "buffer_rank": 120, "wide_book": wide}
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_industry_relative_subtracts_each_industrys_median_and_keeps_unknown_raw() -> None:
    names = ["a1", "a2", "a3", "b1", "u1", "n1"]
    values = np.array([3.0, 1.0, 2.0, 5.0, 4.0, np.nan])
    sector = {"a1": "A", "a2": "A", "a3": "A", "b1": "B", "u1": "unknown", "n1": "A"}
    found = rotation.industry_relative(names, values, sector.__getitem__)
    # A's scored median is 2 (n1 is unscored, so not counted); B is alone: 0; unknown keeps 4
    assert found[:5].tolist() == [1.0, -1.0, 0.0, 0.0, 4.0]
    assert math.isnan(found[5])
    assert values.tolist()[:5] == [3.0, 1.0, 2.0, 5.0, 4.0]  # the input is not changed


def test_ranking_is_industry_relative_only_when_set_and_needs_the_classification() -> None:
    small = {"short_sessions": 5, "long_sessions": 8, "vol_sessions": 20, "min_bars": 5,
             "skip_sessions": 2, "top_n": 2, "buffer_rank": 2, "score": "r6"}  # fmt: skip
    rows = rotation.window_rows(Rotation(**small))
    growth = [1.30, 1.20, 1.05]  # hot sector names x and y, a cold sector's z
    closes = np.column_stack([np.geomspace(100, 100 * g, rows) for g in growth])
    names = ["x", "y", "z"]
    sector = {"x": "hot", "y": "hot", "z": "cold"}.__getitem__
    plain = rotation.ranking(names, closes, Rotation(**small))
    assert [n for n, _ in plain] == ["x", "y", "z"]
    rules = Rotation(**small, industry_relative=True)
    relative = rotation.ranking(names, closes, rules, industry=sector)
    # z leads its own industry (0), x sits above its industry's median, y below it
    assert relative[0][0] == "x"
    assert dict(relative)["z"] == pytest.approx(0.0)
    assert dict(relative)["y"] < 0
    with pytest.raises(ValueError, match="rotation_industry_unknown"):
        rotation.ranking(names, closes, rules)


def test_a_wide_book_widens_top_n_and_the_buffer_in_proportion_only_when_it_fires() -> None:
    rules = Rotation.model_validate(
        {"top_n": 20, "buffer_rank": 45, "wide_book": {"top_n": 30, "checks": [
            {"rule": "breadth_above", "min_pct": 40}]}}
    )  # fmt: skip
    wide = rotation.widened(rules, True)
    assert (wide.top_n, wide.buffer_rank) == (30, 68)  # 45 x 30 / 20 = 67.5, rounded up
    assert rotation.widened(rules, False) is rules
    assert rotation.widened(Rotation(), True) == Rotation()
    # equal weights on the wider count: more, smaller positions of the same capital
    orders = rotation.order_diff(
        ["a", "b"], {}, {"a": Decimal(10), "b": Decimal(10)}, Decimal(3000), wide.top_n,
        Decimal(0), reweight=True,
    )  # fmt: skip
    assert [o.qty for o in orders] == [10, 10]


def test_the_wide_book_fires_on_the_breadth_rule_of_the_rebalance_close() -> None:
    rules = Rotation.model_validate({"wide_book": {"top_n": 30, "checks": [
        {"rule": "breadth_above", "min_pct": 40}]}})  # fmt: skip
    rule = rules.wide_book
    assert rule is not None
    closes = [Decimal(100)] * 5
    weak = [Decimal(50), Decimal(45), Decimal(35)]
    strong = [Decimal(30), Decimal(45), Decimal(55)]
    assert rotation.wide_fires(rule, closes, weak) is True
    assert rotation.wide_fires(rule, closes, strong) is False


# --- the rule universe --------------------------------------------------------------------------


def test_the_rule_universe_is_named_by_its_count_and_its_version_carries_it() -> None:
    assert tu.count_of_name("TOP_TRADED") == 500
    assert tu.count_of_name("TOP_TRADED_300") == 300
    assert tu.count_of_name("NSE_STOCKS") is None
    assert tu.count_of_name("nifty500") is None
    with pytest.raises(ValueError, match="backtest_top_traded_count"):
        tu.count_of_name("TOP_TRADED_0")
    assert tu.count_of(tu.version_id(500)) == 500
    assert tu.count_of(uuid.uuid4()) is None
    version = top_traded_version(250)
    assert version.id == tu.version_id(250)
    assert _universe_label(version, "ignored") == "TOP_TRADED_250"


def _sessions(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(1)
    return out


def test_reform_nights_are_the_last_sessions_of_march_and_september() -> None:
    days = _sessions(date(2015, 1, 1), date(2016, 12, 31))
    found = tu.reform_nights(days, date(2015, 6, 1), date(2016, 6, 30))
    assert found == [date(2015, 3, 31), date(2015, 9, 30), date(2016, 3, 31)]
    assert tu.reform_nights(days, date(2015, 2, 1), date(2015, 6, 1)) == []  # none before
    assert tu.reform_nights(days, date(2015, 3, 31), date(2015, 4, 1)) == [date(2015, 3, 31)]


def test_top_names_rank_the_zero_filled_median_and_need_a_recent_bar() -> None:
    days = _sessions(date(2020, 1, 1), date(2020, 1, 31))[:10]
    full = dict.fromkeys(days, 10.0)
    values = {
        "steady": full,
        "spiky": {d: (100.0 if n < 4 else 1.0) for n, d in enumerate(days)},  # median 1
        "new": dict.fromkeys(days[6:], 50.0),  # 4 of 10 sessions: median 0
        "gone": dict.fromkeys(days[:7], 99.0),  # no bar in the last 3 sessions
        "tie": full,
    }
    assert tu.top_names(values, days, 3, recent=3) == ["steady", "tie", "spiky"]
    assert tu.top_names(values, days, 10, recent=3) == ["steady", "tie", "spiky", "new"]
    assert tu.by_name([("x", days[0], None), ("x", days[1], 5.0)]) == {
        "x": {days[0]: 0.0, days[1]: 5.0}
    }
    with pytest.raises(ValueError, match="backtest_top_traded_history_short"):
        tu.window_of(days, days[3], sessions=5)
    assert tu.window_of(days, days[4], sessions=5) == days[:5]


def test_reform_lists_become_spans_from_the_session_after_each_night() -> None:
    days = _sessions(date(2015, 3, 1), date(2015, 10, 31))
    march, september = date(2015, 3, 31), date(2015, 9, 30)
    spans = tu.intervals([(march, ["a", "b"]), (september, ["b", "c"])], days)
    assert spans == [
        ("a", date(2015, 4, 1), date(2015, 10, 1)),
        ("b", date(2015, 4, 1), None),
        ("c", date(2015, 10, 1), None),
    ]


# --- run outputs --------------------------------------------------------------------------------


def test_a_skipped_trigger_leaves_a_row_with_its_reason() -> None:
    row = _Row("default", uuid.UUID(int=7), date(2024, 5, 2))
    found = skip_row("vcp-swing", row, "limit_not_reached", lambda name, day: f"S{day.day}")
    assert found == {
        "date": date(2024, 5, 2),
        "strategy": "vcp-swing",
        "playbook": "default",
        "instrument_id": str(uuid.UUID(int=7)),
        "symbol": "S2",
        "reason": "limit_not_reached",
    }


SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("checkout", "head", "dirty", "marker", "expected"),
    [
        (True, SHA, "", None, SHA),
        (True, SHA, " M file.py", None, f"{SHA}-dirty"),
        (True, SHA, None, SHA, None),  # git status failed: no commit
        (False, "f" * 40, "", f"{SHA}\n", SHA),  # an exported tree inside another checkout
        (False, None, None, "$Format:%H$\n", None),  # a copied tree: unknown, not wrong
        (False, None, None, None, None),
    ],
)
def test_the_engine_commit_is_the_checkouts_or_the_exported_trees(
    checkout: bool, head: str | None, dirty: str | None, marker: str | None, expected: str | None
) -> None:
    assert commit_of(checkout=checkout, head=head, dirty=dirty, marker=marker) == expected
