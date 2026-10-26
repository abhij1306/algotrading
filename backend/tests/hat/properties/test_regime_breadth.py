"""The `breadth_above` regime rule: threshold, hysteresis, smoothing and a missing session, on the
real `regime_gate.breadth_above` and its config model."""

from decimal import Decimal

import pytest

from app.hat.engine import regime_gate
from app.hat.engine.strategy.config import ConfigError, Regime, parse_config
from app.hat.engine.strategy.registry import STRATEGIES
from app.hat.engine.strategy.yamlio import import_yaml

D = Decimal


def gate(breadth: list[str | None], **params: object) -> bool:
    regime = Regime(index="NIFTY 500", rule="breadth_above", params=params)
    series = [None if v is None else D(v) for v in breadth]
    return regime_gate.regime_allows_longs(regime, [D(100)], series)


def test_threshold_is_inclusive() -> None:
    assert gate(["50"]) is True
    assert gate(["49.99"]) is False
    assert gate(["60"], min_pct=60) is True
    assert gate(["59.9"], min_pct=60) is False


def test_empty_series_is_off() -> None:
    assert gate([]) is False


def test_hysteresis_holds_on_until_below_off_level() -> None:
    params = {"min_pct": 50, "off_below_pct": 40}
    assert gate(["45"], **params) is False  # never reached min_pct: stays off
    assert gate(["55", "45"], **params) is True  # on, and 45 is not below 40
    assert gate(["55", "40"], **params) is True  # at the off level is still on
    assert gate(["55", "39.9"], **params) is False
    assert gate(["55", "39.9", "45"], **params) is False  # back on only at min_pct
    assert gate(["55", "39.9", "50"], **params) is True


def test_no_hysteresis_by_default() -> None:
    assert gate(["55", "49.9"]) is False


def test_smoothing_compares_the_mean() -> None:
    assert gate(["40", "60", "50"], smooth_days=3) is True  # mean 50
    assert gate(["40", "60", "49.9"], smooth_days=3) is False
    assert gate(["60", "60"], smooth_days=3) is False  # window short: off


def test_missing_session_turns_it_off() -> None:
    assert gate(["60", None]) is False
    assert gate(["60", None, "60"]) is True
    # a gap inside the smoothing window keeps it off until the window is whole again
    assert gate(["60", None, "60", "60"], smooth_days=3) is False
    assert gate(["60", None, "60", "60", "60"], smooth_days=3) is True
    # the gap also resets the hysteresis: back on only at min_pct
    assert gate(["60", None, "45"], off_below_pct=40) is False


def test_walk_reads_only_the_last_breadth_sessions() -> None:
    """The state depends on the last `BREADTH_SESSIONS` values only, so live and the backtest
    agree however much history each passes."""
    band = ["45"] * (regime_gate.BREADTH_SESSIONS - 1)
    assert gate(["55", *band], off_below_pct=40) is True
    assert gate(["55", "45", *band], off_below_pct=40) is False


def test_config_rejects_off_level_above_min_pct() -> None:
    with pytest.raises(ValueError, match="off_below_above_min_pct"):
        Regime(index="NIFTY 500", rule="breadth_above", params={"min_pct": 50, "off_below_pct": 55})


def test_config_round_trip_and_needs_breadth() -> None:
    raw = import_yaml(STRATEGIES["stage2-contraction-breakout"].config_text).to_json()
    raw["regime"] = {"index": "NIFTY 500", "rule": "breadth_above", "params": {"min_pct": 55}}
    config = parse_config(raw)
    assert config.regime.params == {"min_pct": 55, "smooth_days": 1}
    assert parse_config(config.to_json()) == config
    assert regime_gate.needs_breadth(config.regime)
    raw["regime"]["params"] = {"min_pct": 101}
    with pytest.raises(ConfigError):
        parse_config(raw)
