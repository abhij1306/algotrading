"""Per-run overrides of a strategy version's config and of the risk policy: a research variant
without a new draft version. A leaf of the backtest package: `spec` records them, `runs` applies
them, and it imports neither.

A config override is a path into the canonical config (`StrategyConfig.to_json`) and a JSON value,
written `path=value` (`parse_set`): `exits[2].at_r=3`, `exits[trailing_swing_low].lookback_days=20`,
`playbooks[default].setup.criteria[vcp].params.min_depth_pct=3`,
`playbooks[default].entry_filters[max_chase].params.pct=2` (a list item by its index, or by its
`rule`, `name` or criterion `type` when exactly one item has it; `rs_rank#1` is the second item
of that type in list order, the setup's criteria in their `order`),
`playbooks[default].stop.max_pct=12`, and `exits[+]={"rule": "max_hold", "days": 60}` appends a
rule to the stack. A name no item has, or one more than one item has without `#n`, is refused.
A value that is not JSON is a string. The overridden config is parsed afresh (`parse_config`),
so an override can only make a config the model accepts, risk keys refused.
The policy overrides are the risk per trade (`risk_pct`) and the open-position cap: the
account's `max_open_positions` and the exposure ladder's top level become N, and its lower levels
are capped at N (the ladder still starts low and steps up).

A run's overrides are recorded in its conventions and hashed into its `config_digest`, and the
run stays a trial of its family (HAT-BT-030), so a sweep of variants raises the family's DSR trial
count and its PBO variants. A run flagged for promotion takes none (`RunConventions`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from ..engine.strategy.config import ConfigError, StrategyConfig, parse_config
from ..errors import HatError
from ..risk.policy import LEVELS, PolicyError, PolicyVersion, parse_policy

__all__ = ["APPEND", "OverrideError", "Overrides", "parse_set", "set_path"]

APPEND = "+"
IDENTITY_KEYS = frozenset({"bot", "market"})
_SEGMENT = re.compile(r"([a-z_][a-z0-9_]*)(?:\[([a-z0-9_+-]+(?:#\d{1,2})?)\])?", re.ASCII)
_INDEX = re.compile(r"\d{1,3}", re.ASCII)


class OverrideError(HatError):
    """An override was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _segments(path: str) -> list[tuple[str, str | None]]:
    """The path's (key, selector) segments; `OverrideError` for a malformed path."""
    parts = path.split(".")
    found = []
    for n, part in enumerate(parts):
        match = _SEGMENT.fullmatch(part)
        if match is None or (match.group(2) == APPEND and n != len(parts) - 1):
            raise OverrideError("backtest_bad_override_path")
        found.append((match.group(1), match.group(2)))
    return found


def parse_set(text: str) -> tuple[str, Any]:
    """`path=value` as (path, value): the value parsed as JSON, else kept as a string."""
    path, sep, raw = text.partition("=")
    path = path.strip()
    if not sep or not raw.strip():
        raise OverrideError("backtest_bad_override_path")
    _segments(path)
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw.strip()
    return path, value


def _pick(items: list[Any], selector: str) -> int:
    """The index of the list item `selector` names: its index, the one item whose `rule`, `name`
    or `type` it is, or with `#n` the n-th (from 0) of the items it names."""
    if _INDEX.fullmatch(selector):
        index = int(selector)
        if index < len(items):
            return index
        raise OverrideError("backtest_override_no_item")
    key, nth, place = selector.partition("#")
    hits = [
        n
        for n, item in enumerate(items)
        if isinstance(item, dict) and key in (item.get("rule"), item.get("name"), item.get("type"))
    ]
    if nth:
        if int(place) < len(hits):
            return hits[int(place)]
        raise OverrideError("backtest_override_no_item")
    if len(hits) != 1:
        raise OverrideError(
            "backtest_override_no_item" if not hits else "backtest_override_ambiguous"
        )
    return hits[0]


def set_path(doc: dict[str, Any], path: str, value: object) -> None:
    """Set `path` (see the module docstring) of the JSON document `doc` to `value`, in place."""
    node: Any = doc
    segments = _segments(path)
    for n, (key, selector) in enumerate(segments):
        last = n == len(segments) - 1
        if not isinstance(node, dict):
            raise OverrideError("backtest_bad_override_path")
        if selector is None:
            if last:
                node[key] = value
                return
            if key not in node:
                raise OverrideError("backtest_bad_override_path")
            node = node[key]
            continue
        items = node.get(key)
        if not isinstance(items, list):
            raise OverrideError("backtest_bad_override_path")
        if selector == APPEND:
            items.append(value)
            return
        index = _pick(items, selector)
        if last:
            items[index] = value
            return
        node = items[index]


@dataclass(frozen=True)
class Overrides:
    """A run's overrides: config `sets` applied in order, and the policy's risk per trade and
    open-position cap (None: the policy's)."""

    sets: tuple[tuple[str, Any], ...] = ()
    risk_pct: Decimal | None = None
    max_positions: int | None = None

    @property
    def empty(self) -> bool:
        return not self.sets and self.risk_pct is None and self.max_positions is None

    def touches_identity(self) -> bool:
        """Whether a set names the config's `bot` or `market`: a run's sleeve, sizing and family
        are the version's, so an override may not change them."""
        return any(_segments(path)[0][0] in IDENTITY_KEYS for path, _ in self.sets)

    def apply_config(self, config: StrategyConfig) -> StrategyConfig:
        """`config` with the sets applied, parsed afresh; `OverrideError` for a path that names
        nothing and `backtest_override_invalid` for a config the model refuses."""
        if not self.sets:
            return config
        doc = config.to_json()
        for path, value in self.sets:
            set_path(doc, path, value)
        try:
            return parse_config(doc)
        except ConfigError:
            raise OverrideError("backtest_override_invalid") from None

    def apply_policy(self, version: PolicyVersion) -> PolicyVersion:
        """`version` with its policy's `risk_pct` and open-position cap replaced (the module
        docstring), parsed afresh (`backtest_override_invalid` for a value the policy schema
        refuses); the version id is unchanged, and the run records the overrides."""
        if self.risk_pct is None and self.max_positions is None:
            return version
        doc = version.policy.to_json()
        if self.risk_pct is not None:
            doc["risk_pct"] = str(self.risk_pct)
        if self.max_positions is not None:
            doc["max_open_positions"] = self.max_positions
            ladder = doc["exposure_ladder"]
            for level in LEVELS:  # the top level is the cap, and none above it
                cap = self.max_positions if level == LEVELS[-1] else None
                row = ladder[level]
                row["max_positions"] = cap or min(row["max_positions"], self.max_positions)
        try:
            changed = parse_policy(doc)
        except PolicyError:
            raise OverrideError("backtest_override_invalid") from None
        return replace(version, policy=changed)

    def record(self) -> dict[str, Any]:
        """The overrides as the run's conventions record them (only those set)."""
        out: dict[str, Any] = {}
        if self.sets:
            out["set"] = [{"path": path, "value": value} for path, value in self.sets]
        if self.risk_pct is not None:
            out["risk_pct"] = format(self.risk_pct.normalize(), "f")
        if self.max_positions is not None:
            out["max_positions"] = self.max_positions
        return out
