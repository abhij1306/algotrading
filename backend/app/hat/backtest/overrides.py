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
rule to the stack. A name no item has, or one more than one item has without `#n`, is refused,
as is a key the config model does not know (a typo'd field or criterion parameter).
A value that is not JSON is a string. The overridden config is parsed afresh (`parse_config`),
so an override can only make a config the model accepts, risk keys refused.
The policy overrides are the risk per trade (`risk_pct`), the open-position cap (the account's
`max_open_positions` and the exposure ladder's top level become N, and its lower levels are capped
at N: the ladder still starts low and steps up), the per-industry cap
(`max_positions_per_industry`), and the flat ladder (`flat_ladder`): every ladder level holds the
run's open-position cap at risk multiplier 1, so the run trades at one size from its first day.
The policy's other limits stay as written.

A whole candidate policy can replace the version's for a run (`policy`, read from a YAML or JSON
file by `read_policy_file` and parsed with `parse_policy`, refused if invalid), and single fields
can be set by a dotted path into the policy document (`policy_sets`, written `path=value` and
parsed by `parse_policy_set`: `health_bands.half_dd=0.12`, `kill.bot_drawdown_pct=0.15`,
`p_max.momentum-breakout-ep=0.30`; a path must name an existing field). They apply in that order,
before the other policy overrides. A decimal is kept as its exact text (the policy refuses floats).
The run records the candidate document with its digest. This is research only: the live policy
changes only by a new version under its gates (docs/invariants.md, "Policy changes").

A run's overrides are recorded in its `config.overrides` and `config.json`, and its resolved
parameters' hash is a trial of its strategy (`trials`), so a sweep of distinct variants raises
the DSR trial count. A run flagged for promotion takes none (`RunConventions`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..engine.strategy.config import ConfigError, StrategyConfig, parse_config
from ..errors import HatError
from ..risk import policy_file
from ..risk.policy import LEVELS, PolicyError, PolicyVersion, RiskPolicy, parse_policy

__all__ = [
    "APPEND",
    "OverrideError",
    "Overrides",
    "parse_policy_set",
    "parse_set",
    "policy_digest",
    "policy_text",
    "read_policy_file",
    "set_path",
]

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


def set_path(doc: dict[str, Any], path: str, value: object) -> str:
    """Set `path` (see the module docstring) of the JSON document `doc` to `value`, in place;
    the path it set, each list selector resolved to its index (`exits[time_stop].enabled` and
    `exits[3].enabled` name one field), an append as the new item's index."""
    node: Any = doc
    segments = _segments(path)
    done: list[str] = []
    for n, (key, selector) in enumerate(segments):
        last = n == len(segments) - 1
        if not isinstance(node, dict):
            raise OverrideError("backtest_bad_override_path")
        if selector is None:
            done.append(key)
            if last:
                node[key] = value
                return ".".join(done)
            if key not in node:
                raise OverrideError("backtest_bad_override_path")
            node = node[key]
            continue
        items = node.get(key)
        if not isinstance(items, list):
            raise OverrideError("backtest_bad_override_path")
        if selector == APPEND:
            items.append(value)
            return ".".join([*done, f"{key}[{len(items) - 1}]"])
        index = _pick(items, selector)
        done.append(f"{key}[{index}]")
        if last:
            items[index] = value
            return ".".join(done)
        node = items[index]
    raise OverrideError("backtest_bad_override_path")  # pragma: no cover - a path has a segment


def policy_text(text: str) -> RiskPolicy:
    """A whole policy document (YAML or JSON, `policy_file.policy_text`) as a typed policy;
    `backtest_policy_invalid` for text that does not parse or a document the policy refuses."""
    try:
        return policy_file.policy_text(text)
    except PolicyError:
        raise OverrideError("backtest_policy_invalid") from None


def read_policy_file(path: str | Path) -> RiskPolicy:
    """The policy in the file at `path` (`policy_text`); `backtest_policy_unreadable` when it
    cannot be read."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise OverrideError("backtest_policy_unreadable") from None
    return policy_text(text)


def policy_digest(policy: RiskPolicy) -> str:
    """The sha256 of the policy's canonical document (`to_json`, keys sorted)."""
    text = json.dumps(policy.to_json(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def parse_policy_set(text: str) -> tuple[str, Any]:
    """`path=value` for one policy field: a dotted path of keys (`kill.bot_drawdown_pct`) and
    the value as JSON, a decimal kept as its exact text, anything else not JSON as a string."""
    path, sep, raw = text.partition("=")
    path, raw = path.strip(), raw.strip()
    if not sep or not raw or not all(path.split(".")):
        raise OverrideError("backtest_bad_override_path")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if isinstance(value, float):
        value = raw
    return path, value


def _set_policy_field(doc: dict[str, Any], path: str, value: object) -> None:
    """Set the existing field at the dotted `path` of the policy document `doc`, in place."""
    *parents, leaf = path.split(".")
    node: Any = doc
    for key in parents:
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict) or leaf not in node:
        raise OverrideError("backtest_bad_override_path")
    node[leaf] = value


@dataclass(frozen=True)
class Overrides:
    """A run's overrides: config `sets` applied in order, a whole candidate `policy` and the
    `policy_sets` on it, and the policy's risk per trade, open-position and per-industry caps
    (None: the policy's) and the flat ladder."""

    sets: tuple[tuple[str, Any], ...] = ()
    risk_pct: Decimal | None = None
    max_positions: int | None = None
    max_per_industry: int | None = None
    flat_ladder: bool = False
    policy: RiskPolicy | None = None
    policy_sets: tuple[tuple[str, Any], ...] = ()

    @property
    def empty(self) -> bool:
        return not self.sets and not self.touches_policy

    @property
    def touches_policy(self) -> bool:
        return (
            self.policy is not None
            or bool(self.policy_sets)
            or self.risk_pct is not None
            or self.max_positions is not None
            or self.max_per_industry is not None
            or self.flat_ladder
        )

    def touches_identity(self) -> bool:
        """Whether a set names the config's `bot` or `market`: a run's sleeve, sizing and family
        are the version's, so an override may not change them."""
        return any(_segments(path)[0][0] in IDENTITY_KEYS for path, _ in self.sets)

    def apply_config(self, config: StrategyConfig) -> StrategyConfig:
        """`config` with the sets applied, parsed afresh; `backtest_bad_override_path` (or the
        selector's code) for a path that names nothing, a key the model does not know among
        them, and `backtest_override_invalid` for a value the model refuses."""
        if not self.sets:
            return config
        doc = config.to_json()
        for path, value in self.sets:
            set_path(doc, path, value)
        try:
            return parse_config(doc)
        except ConfigError as exc:
            unknown = "extra_forbidden" in exc.codes
            code = "backtest_bad_override_path" if unknown else "backtest_override_invalid"
            raise OverrideError(code) from None

    def apply_policy(self, version: PolicyVersion) -> PolicyVersion:
        """`version` with its policy replaced by the candidate, its fields set, and its
        `risk_pct`, caps and ladder replaced (the module docstring), parsed afresh
        (`backtest_override_invalid` for a value the policy schema refuses); the version id is
        unchanged, and the run records the overrides."""
        if not self.touches_policy:
            return version
        doc = (version.policy if self.policy is None else self.policy).to_json()
        for path, value in self.policy_sets:
            _set_policy_field(doc, path, value)
        if self.risk_pct is not None:
            doc["risk_pct"] = str(self.risk_pct)
        if self.max_positions is not None:
            doc["max_open_positions"] = self.max_positions
            ladder = doc["exposure_ladder"]
            for level in LEVELS:  # the top level is the cap, and none above it
                cap = self.max_positions if level == LEVELS[-1] else None
                row = ladder[level]
                row["max_positions"] = cap or min(row["max_positions"], self.max_positions)
        if self.max_per_industry is not None:
            doc["max_positions_per_industry"] = self.max_per_industry
        if self.flat_ladder:
            for level in LEVELS:
                doc["exposure_ladder"][level] = {
                    "max_positions": doc["max_open_positions"],
                    "risk_mult": "1",
                }
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
        if self.policy is not None:
            out["policy"] = {
                "digest": policy_digest(self.policy),
                "document": self.policy.to_json(),
            }
        if self.policy_sets:
            out["policy_set"] = [{"path": path, "value": value} for path, value in self.policy_sets]
        if self.risk_pct is not None:
            out["risk_pct"] = format(self.risk_pct.normalize(), "f")
        if self.max_positions is not None:
            out["max_positions"] = self.max_positions
        if self.max_per_industry is not None:
            out["max_per_industry"] = self.max_per_industry
        if self.flat_ladder:
            out["flat_ladder"] = True
        return out
