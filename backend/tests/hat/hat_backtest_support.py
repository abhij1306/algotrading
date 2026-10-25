"""Small builders for the backtest fill and cost properties: instruments, sessions, a
permissive config and spec."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.hat.backtest.spec import RunConventions, RunSpec
from app.hat.costs import (
    BacktestFills,
    load_cost_model,
    parse_cost_model,
)
from app.hat.engine.strategy.config import StrategyConfig, parse_config
from app.hat.risk.policy import PolicyVersion, seed_policy

__all__ = [
    "BOT",
    "FIRST_DAY",
    "INDEX",
    "VERSION",
    "WARM_UP",
    "config",
    "instrument",
    "policy_version",
    "sessions",
    "spec",
    "zero_fills",
]

D = Decimal
FIRST_DAY = date(2025, 1, 6)  # a Monday
INDEX = "NIFTY 500"
BOT = "stage2-contraction-breakout"  # a strategy key the seed policy has a p_max for
VERSION = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
POLICY = uuid.UUID("00000000-0000-7000-8000-0000000000b2")
ZERO = D(0)

_EXITS: list[dict[str, Any]] = [
    {"rule": "hard_stop"},
    {"rule": "move_stop_to_breakeven", "at_r": 1},
    {"rule": "partial_at_r", "at_r": 2, "sell_fraction": 0.5},
    {"rule": "pct_gain_target", "pct": 20, "sell_fraction": 1.0},
    {"rule": "close_below_sma", "sma": 5, "sell_fraction": 1.0},
    {"rule": "time_stop", "days": 8, "min_r": 0.5},
]


def instrument(n: int) -> str:
    return str(uuid.UUID(int=0x1000 + n))


def sessions(count: int, start: date = FIRST_DAY) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def config(
    *,
    stop: Mapping[str, Any] | None = None,
    exits: Sequence[Mapping[str, Any]] | None = None,
    chase: float = 5.0,
    regime_sma: int = 3,
    adtr: float | None = None,
) -> StrategyConfig:
    """A one-playbook config whose pivot is a short consolidation's high; `adtr` adds an
    `adtr_filter` of that multiple before the max chase."""
    filters: list[dict[str, Any]] = [{"type": "max_chase", "params": {"pct": chase}}]
    if adtr is not None:
        filters.insert(0, {"type": "adtr_filter", "params": {"max_multiple": adtr}})
    raw: dict[str, Any] = {
        "bot": BOT,
        "universe": [{"type": "series_in", "params": {"series": ["EQ"]}}],
        "regime": {
            "index": INDEX,
            "rule": "close_above_sma_and_rising",
            "params": {"sma": regime_sma, "rising_days": 1},
        },
        "setup": {
            "threshold": 0.0,
            "criteria": [
                {
                    "type": "consolidation",
                    "mode": "score",
                    "weight": 1,
                    "params": {"days": [3, 5], "max_range_pct": 30, "higher_lows": False},
                }
            ],
        },
        "trigger": [{"type": "price_breaks_level", "params": {"level": "consolidation_high"}}],
        "entry_filters": filters,
        "stop": dict(stop or {"rule": "pct_below_entry", "pct": 5}),
        "exits": copy.deepcopy(list(exits or _EXITS)),
    }
    return parse_config(raw)


def policy_version() -> PolicyVersion:
    return PolicyVersion(
        version_id=POLICY,
        version=1,
        effective_from=datetime(2020, 1, 1, tzinfo=UTC),
        superseded_at=None,
        policy=seed_policy(),
    )


# adv20 (the liquidity cap's input) needs 20 sessions, so a run starts after them
WARM_UP = 21


def zero_fills() -> BacktestFills:
    """No slippage and no charges: fills at the proxy prices exactly."""
    model = parse_cost_model({"version": "zero", "gst_rate": "0", "components": []})
    return BacktestFills(model, ZERO)


def spec(
    cfg: StrategyConfig,
    days: Sequence[date],
    *,
    slippage: Decimal = ZERO,
    start: int = WARM_UP,
    fills: BacktestFills | None = None,
    policy: PolicyVersion | None = None,
    conventions: RunConventions | None = None,
    for_promotion: bool = False,
) -> RunSpec:
    return RunSpec(
        config=cfg,
        strategy_version_id=VERSION,
        policy=policy or policy_version(),
        start=days[min(start, len(days) - 1)],
        end=days[-1],
        fills=fills or BacktestFills(load_cost_model(), slippage),
        for_promotion=for_promotion,
        conventions=conventions or RunConventions(),
    )
