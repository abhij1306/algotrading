"""A portfolio file: one backtest defined as configuration (`python -m app.hat.backtest
--portfolio FILE`). It names the strategies a run trades on one account and their weights and
playbooks, the universe, the window, the risk policy's overrides, the run's conventions and,
optionally, the idle-cash ETF overlay; `parse_portfolio` reads it into the `RunRequest` the
command line would build. A leaf over `runs`: it reads no database and no file but the policy
file a `policy` key names (`read_policy`).

    portfolio: s1-s2-etf              # the run's label (its `variant`)
    universe: NIFTY 500               # a research universe, by name or id
    window: {from: 2016-01-01, to: 2026-10-23}
    start_capital: 1000000            # optional, as the CLI's flags: slippage_bps, cost_model,
    slippage_bps: 20                  #   cooldown_sessions, risk_pct, max_positions,
    risk_pct: 0.005                   #   max_per_industry, flat_ladder, policy, policy_set
    conventions: {entry_bars: bars_5m, regime_mult: "1", slot_order: time}
    strategies:                       # the first is the run's own; each strategy once
      - version: <strategy version id>
        weight: 0.5                   # of the account; required with more than one strategy
        playbooks: [default]          # the playbooks kept (default: all)
        set: ["exits[2].at_r=3"]      # config overrides (`overrides.parse_set`)
      - version: <another strategy's version id>
        weight: 0.5
    overlay: {sleeve: regime}         # the ETF overlay (`overlay.parse_overlay`); off without
    split: in_sample                  # or hold_out with `validates: <run id>`

Every key but `portfolio`, `universe`, `window` and `strategies` is optional, with the command
line's default. An unknown key, a missing one or a value of the wrong shape is refused
`backtest_portfolio_bad_file`; a refused override or overlay value keeps its own code.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from ..costs import DEFAULT_COST_MODEL, DEFAULT_SLIPPAGE_BPS
from ..money import exact
from ..risk.policy import RiskPolicy
from .overlay import OverlayError, parse_overlay
from .overrides import OverrideError, Overrides, parse_policy_set, parse_set
from .runs import IN_SAMPLE, CoSleeveRequest, Labels, RunRequest
from .spec import BacktestError, RunConventions

__all__ = ["PORTFOLIO_KEYS", "STRATEGY_KEYS", "parse_portfolio"]

BAD = "backtest_portfolio_bad_file"
PORTFOLIO_KEYS = frozenset(
    {
        "portfolio",
        "universe",
        "window",
        "start_capital",
        "slippage_bps",
        "cost_model",
        "cooldown_sessions",
        "risk_pct",
        "max_positions",
        "max_per_industry",
        "flat_ladder",
        "policy",
        "policy_set",
        "conventions",
        "strategies",
        "overlay",
        "split",
        "validates",
    }
)
_REQUIRED = frozenset({"portfolio", "universe", "window", "strategies"})
STRATEGY_KEYS = frozenset({"version", "weight", "playbooks", "set"})
# the conventions block's keys onto `RunConventions`' fields
_CONVENTIONS = {
    "entry_fill": "entry_fill_rule",
    "slot_order": "slot_order",
    "ambiguous": "ambiguous",
    "suspension_sessions": "suspension_sessions",
    "regime_mult": "regime_mult",
    "entry_bars": "entry_bars",
    "volume_pace": "volume_pace",
}


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise BacktestError(BAD)
    number = exact(str(value))
    if number is None:
        raise BacktestError(BAD)
    return number


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BacktestError(BAD)
    return value


def _day(value: object) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise BacktestError(BAD) from None


def _texts(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise BacktestError(BAD)
    return value


def _sets(value: object, parse: Callable[[str], tuple[str, Any]]) -> tuple[tuple[str, Any], ...]:
    try:
        return tuple(parse(text) for text in _texts(value))
    except OverrideError as exc:
        raise BacktestError(exc.code) from None


def _strategy(item: object) -> tuple[uuid.UUID, Decimal | None, Overrides]:
    """One `strategies` item: its version, weight (None when not given) and config overrides."""
    if not isinstance(item, Mapping) or "version" not in item or not set(item) <= STRATEGY_KEYS:
        raise BacktestError(BAD)
    try:
        version = uuid.UUID(str(item["version"]))
    except ValueError:
        raise BacktestError(BAD) from None
    weight = None if "weight" not in item else _decimal(item["weight"])
    playbooks = None if "playbooks" not in item else tuple(_texts(item["playbooks"]))
    if playbooks == ():
        raise BacktestError(BAD)
    sets = _sets(item.get("set", []), parse_set)
    return version, weight, Overrides(sets=sets, playbooks=playbooks)


def parse_portfolio(
    doc: object, read_policy: Callable[[str], RiskPolicy], args: tuple[str, ...] = ()
) -> RunRequest:
    """The run a portfolio file (its parsed YAML or JSON document) defines, as a research
    `RunRequest` (the module docstring); `args` is the command line, recorded with it."""
    if not isinstance(doc, Mapping) or not _REQUIRED <= set(doc) <= PORTFOLIO_KEYS:
        raise BacktestError(BAD)
    name, universe, window = doc["portfolio"], doc["universe"], doc["window"]
    if not (isinstance(name, str) and name and isinstance(universe, str) and universe):
        raise BacktestError(BAD)
    if not isinstance(window, Mapping) or set(window) != {"from", "to"}:
        raise BacktestError(BAD)
    strategies = doc["strategies"]
    if not isinstance(strategies, list) or not strategies:
        raise BacktestError(BAD)
    legs = [_strategy(item) for item in strategies]
    if len({version for version, _, _ in legs}) != len(legs):
        raise BacktestError("backtest_portfolio_duplicate_strategy")
    if len(legs) > 1 and any(weight is None for _, weight, _ in legs):
        raise BacktestError(BAD)  # shared capital needs each strategy's share of it
    (version, weight, own), others = legs[0], legs[1:]
    policy = None
    if "policy" in doc:
        if not isinstance(doc["policy"], str) or not doc["policy"]:
            raise BacktestError(BAD)
        try:
            policy = read_policy(doc["policy"])
        except OverrideError as exc:
            raise BacktestError(exc.code) from None
    flat = doc.get("flat_ladder", False)
    if not isinstance(flat, bool):
        raise BacktestError(BAD)
    overrides = Overrides(
        sets=own.sets,
        playbooks=own.playbooks,
        risk_pct=None if "risk_pct" not in doc else _decimal(doc["risk_pct"]),
        max_positions=None if "max_positions" not in doc else _count(doc["max_positions"]),
        max_per_industry=(
            None if "max_per_industry" not in doc else _count(doc["max_per_industry"])
        ),
        flat_ladder=flat,
        policy=policy,
        policy_sets=_sets(doc.get("policy_set", []), parse_policy_set),
    )
    given = doc.get("conventions", {})
    if not isinstance(given, Mapping) or not set(given) <= set(_CONVENTIONS):
        raise BacktestError(BAD)
    fields: dict[str, Any] = {
        _CONVENTIONS[k]: (_count(v) if k == "suspension_sessions" else str(v))
        for k, v in given.items()
    }
    if "start_capital" in doc:
        fields["start_capital"] = _decimal(doc["start_capital"])
    conventions = RunConventions(sleeve_weight=weight, overrides=overrides, **fields)
    overlay = None
    if "overlay" in doc:
        try:
            overlay = parse_overlay(doc["overlay"])
        except OverlayError as exc:
            raise BacktestError(exc.code) from None
    split = doc.get("split", IN_SAMPLE)
    validates = doc.get("validates")
    if not isinstance(split, str) or not (validates is None or isinstance(validates, str)):
        raise BacktestError(BAD)
    return RunRequest(
        start=_day(window["from"]),
        end=_day(window["to"]),
        universe=universe,
        slippage_bps=(
            DEFAULT_SLIPPAGE_BPS if "slippage_bps" not in doc else _decimal(doc["slippage_bps"])
        ),
        cost_model=str(doc.get("cost_model", DEFAULT_COST_MODEL)),
        version_id=version,
        cooldown_sessions=(
            None if "cooldown_sessions" not in doc else _count(doc["cooldown_sessions"])
        ),
        conventions=conventions,
        labels=Labels(variant=name, split=split, validates=validates),
        args=args,
        co_sleeves=tuple(CoSleeveRequest(v, w or Decimal(0), o) for v, w, o in others),
        overlay=overlay,
    )
